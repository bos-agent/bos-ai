"""PlanPlugin — structured per-chat planning state and tools."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from bos.core.contract import (
    AgentPlugin,
    PluginServices,
    TurnInterceptor,
    ep_plugin,
)
from bos.core.registry import ToolRegistry

if TYPE_CHECKING:
    from bos.core.agent import TurnContext

_PLAN_STATUSES = {"draft", "needs_input", "approved", "in_progress", "verified", "abandoned"}
_TERMINAL_PLAN_STATUSES = {"verified", "abandoned"}


@dataclass
class _Plan:
    objective: str
    user_value: str = ""
    appetite: str = ""
    constraints: list[str] = field(default_factory=list)
    current_context: list[str] = field(default_factory=list)
    shaped_solution: str = ""
    risks: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    breakdown: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    status: str = "draft"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "objective": self.objective,
            "user_value": self.user_value,
            "appetite": self.appetite,
            "constraints": self.constraints,
            "current_context": self.current_context,
            "shaped_solution": self.shaped_solution,
            "risks": self.risks,
            "non_goals": self.non_goals,
            "breakdown": self.breakdown,
            "verification": self.verification,
            "open_questions": self.open_questions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _render_list(label: str, values: list[str]) -> list[str]:
    if not values:
        return []
    lines = [f"<{label}>"]
    lines.extend(f"- {escape(value)}" for value in values)
    lines.append(f"</{label}>")
    return lines


def _render_current_plan(plan: _Plan) -> str:
    lines = [
        '<current_plan status="{}">'.format(escape(plan.status, {'"': "&quot;"})),
        f"<objective>{escape(plan.objective)}</objective>",
    ]
    if plan.user_value:
        lines.append(f"<user_value>{escape(plan.user_value)}</user_value>")
    if plan.appetite:
        lines.append(f"<appetite>{escape(plan.appetite)}</appetite>")
    lines.extend(_render_list("constraints", plan.constraints))
    lines.extend(_render_list("current_context", plan.current_context))
    if plan.shaped_solution:
        lines.append(f"<shaped_solution>{escape(plan.shaped_solution)}</shaped_solution>")
    lines.extend(_render_list("risks", plan.risks))
    lines.extend(_render_list("non_goals", plan.non_goals))
    if plan.status != "in_progress":
        lines.extend(_render_list("breakdown", plan.breakdown))
    lines.extend(_render_list("verification", plan.verification))
    lines.extend(_render_list("open_questions", plan.open_questions))
    lines.append("</current_plan>")
    return "\n".join(lines)


_PLAN_TOOL_USAGE = {
    "PlanCreate": """Create or replace the current structured plan for this conversation.

Use for non-trivial work when alignment matters before implementation. Capture the desired
outcome, user-visible value, appetite, constraints, current evidence, shaped solution, step
breakdown, verification, non-goals, risks, and any open questions. If open questions remain, set
status to needs_input and end the turn by asking those questions in normal assistant text.""",

    "PlanUpdate": """Update the current structured plan.

Use this as planning evolves across turns: incorporate user answers, record new evidence from
read-only inspection, move status from needs_input to approved/in_progress, or refine the
breakdown and verification. List fields replace the previous list when provided; omit fields you
do not want to change.""",

    "PlanGet": """Return the current structured plan for this conversation.

Use before resuming a multi-turn plan if the visible prompt context is insufficient or when you
need the exact structured state.""",

    "PlanClear": """Clear the current structured plan for this conversation.

Use when the work is completed, abandoned, or superseded by a new unrelated objective.""",
}

_PLAN_PROMPT_SECTION = """<plan_workflow>
Use plan tools for non-trivial work, especially when the task has multiple sources, calculations, external/current data,
financial/legal/medical implications, multi-file code changes, or more than three dependent steps.

Decision rule:
- If the task is simple and one-step, do it directly without plan tools.
- If the task is complex but clear, create a plan with status in_progress and execute in the same turn.
- If the task is complex and has important ambiguity or missing constraints, create a plan with status needs_input and
  end the turn with concise questions.
- If the user asks to think first, plan, discuss, design, review the approach, or otherwise align before acting, create
  or update a plan before implementation.

How to plan:
- Use PlanCreate to start or replace a structured plan, PlanUpdate to refine lifecycle/state, PlanGet to inspect exact
  state, and PlanClear when the plan is completed, abandoned, or superseded.
- Plan from the desired outcome backwards, then shape a bounded approach.
- Clarify the user-visible objective, user value, appetite, constraints, current context, risks, and non-goals.
- Define a shaped solution, a concrete step breakdown, and verification before editing.
- If important unknowns remain, set plan status to needs_input and end the turn with concise questions.
- Do not implement while the plan is needs_input; wait for the user to answer, clarify, or clearly ask you to proceed.
- After user approval or an explicit proceed instruction, set status to in_progress and execute.
- The plan is a contract with the user; the task list is the ledger for the work. When moving to in_progress,
  materialize the plan breakdown into tasks with TaskCreate and track execution progress only with task tools.
- Do not edit the plan breakdown during execution; the task list is the single source of truth for steps.
- When verification is complete, set status to verified or clear the plan if it is no longer useful.
</plan_workflow>"""

_NEEDS_INPUT_NUDGE = """<plan_needs_input>
The current plan status is needs_input: it has blocking open questions.
- If the latest user message answers them, use PlanUpdate to record the answers, clear the answered
  questions, set an appropriate status, and continue.
- Otherwise do not start or continue implementation work, and do not call any more non-plan tools.
  End the turn now by asking the user the open questions in plain assistant text.
</plan_needs_input>"""

_AUTO_PLAN_CONTEXT = [
    "Plan auto-triggered because the request appears complex.",
    "Before doing substantive work, update the plan with shaped solution, breakdown, verification, and open questions.",
    "If there is no blocking ambiguity, proceed after updating the plan.",
]


@ep_plugin(name="PlanPlugin")
class PlanHarnessPlugin:
    @property
    def name(self) -> str:
        return "PlanPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {"auto_trigger": True}

    async def setup(self, services: PluginServices) -> None:
        pass

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config.get("auto_trigger", True), bool):
            raise TypeError("PlanPlugin: 'auto_trigger' must be a boolean")

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        return PlanAgentPlugin(auto_trigger=config.get("auto_trigger", True))

    async def teardown(self) -> None:
        pass


class PlanAgentPlugin:
    def __init__(self, *, auto_trigger: bool = True) -> None:
        self._plans: dict[str, _Plan] = {}
        self._auto_trigger = auto_trigger

    @property
    def name(self) -> str:
        return "PlanPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        plans = self._plans

        @registry(
            name="PlanCreate",
            description="Create or replace the structured plan for the current conversation.",
            usage=_PLAN_TOOL_USAGE["PlanCreate"],
            parameters={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "description": "Desired user-visible outcome."},
                    "user_value": {"type": "string", "description": "Why this matters to the user."},
                    "appetite": {"type": "string", "description": "Appropriate effort, risk, and scope."},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "current_context": {"type": "array", "items": {"type": "string"}},
                    "shaped_solution": {"type": "string", "description": "Bounded approach to implement."},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "non_goals": {"type": "array", "items": {"type": "string"}},
                    "breakdown": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Proposed step breakdown for user review. After approval, materialize"
                            " these via TaskCreate; the task list is the source of truth during"
                            " execution."
                        ),
                    },
                    "verification": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                    "status": {
                        "type": "string",
                        "enum": sorted(_PLAN_STATUSES),
                        "description": "Plan lifecycle status.",
                    },
                },
                "required": ["objective"],
            },
        )
        async def plan_create(
            objective: str,
            user_value: str = "",
            appetite: str = "",
            constraints: list[str] | None = None,
            current_context: list[str] | None = None,
            shaped_solution: str = "",
            risks: list[str] | None = None,
            non_goals: list[str] | None = None,
            breakdown: list[str] | None = None,
            verification: list[str] | None = None,
            open_questions: list[str] | None = None,
            status: str = "draft",
            chat_id: str = "",
        ) -> dict[str, Any]:
            if status not in _PLAN_STATUSES:
                return {"error": f"Invalid status {status!r}. Expected one of: {', '.join(sorted(_PLAN_STATUSES))}."}
            if status == "draft" and open_questions:
                status = "needs_input"
            plan = _Plan(
                objective=objective,
                user_value=user_value,
                appetite=appetite,
                constraints=constraints or [],
                current_context=current_context or [],
                shaped_solution=shaped_solution,
                risks=risks or [],
                non_goals=non_goals or [],
                breakdown=breakdown or [],
                verification=verification or [],
                open_questions=open_questions or [],
                status=status,
            )
            plans[chat_id] = plan
            return {"result": "Plan created.", "plan": plan.to_payload()}

        @registry(
            name="PlanUpdate",
            description="Update fields or lifecycle status on the current structured plan.",
            usage=_PLAN_TOOL_USAGE["PlanUpdate"],
            parameters={
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "user_value": {"type": "string"},
                    "appetite": {"type": "string"},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "current_context": {"type": "array", "items": {"type": "string"}},
                    "shaped_solution": {"type": "string"},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "non_goals": {"type": "array", "items": {"type": "string"}},
                    "breakdown": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Proposed step breakdown for user review. After approval, materialize"
                            " these via TaskCreate; the task list is the source of truth during"
                            " execution."
                        ),
                    },
                    "verification": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": sorted(_PLAN_STATUSES)},
                },
                "required": [],
            },
        )
        async def plan_update(
            objective: str | None = None,
            user_value: str | None = None,
            appetite: str | None = None,
            constraints: list[str] | None = None,
            current_context: list[str] | None = None,
            shaped_solution: str | None = None,
            risks: list[str] | None = None,
            non_goals: list[str] | None = None,
            breakdown: list[str] | None = None,
            verification: list[str] | None = None,
            open_questions: list[str] | None = None,
            status: str | None = None,
            chat_id: str = "",
        ) -> dict[str, Any]:
            plan = plans.get(chat_id)
            if plan is None:
                return {"error": "No plan exists for this conversation. Use PlanCreate first."}
            if status is not None:
                if status not in _PLAN_STATUSES:
                    expected = ", ".join(sorted(_PLAN_STATUSES))
                    return {"error": f"Invalid status {status!r}. Expected one of: {expected}."}
                plan.status = status
            for key, value in {
                "objective": objective,
                "user_value": user_value,
                "appetite": appetite,
                "shaped_solution": shaped_solution,
            }.items():
                if value is not None:
                    setattr(plan, key, value)
            for key, value in {
                "constraints": constraints,
                "current_context": current_context,
                "risks": risks,
                "non_goals": non_goals,
                "breakdown": breakdown,
                "verification": verification,
                "open_questions": open_questions,
            }.items():
                if value is not None:
                    setattr(plan, key, value)
            plan.touch()
            return {"result": "Plan updated.", "plan": plan.to_payload()}

        @registry(
            name="PlanGet",
            description="Return the current structured plan for this conversation.",
            usage=_PLAN_TOOL_USAGE["PlanGet"],
            parameters={"type": "object", "properties": {}, "required": []},
        )
        async def plan_get(chat_id: str = "") -> dict[str, Any]:
            plan = plans.get(chat_id)
            if plan is None:
                return {"plan": None}
            return {"plan": plan.to_payload()}

        @registry(
            name="PlanClear",
            description="Clear the current structured plan for this conversation.",
            usage=_PLAN_TOOL_USAGE["PlanClear"],
            parameters={"type": "object", "properties": {}, "required": []},
        )
        async def plan_clear(chat_id: str = "") -> dict[str, Any]:
            removed = plans.pop(chat_id, None) is not None
            return {"result": "Plan cleared." if removed else "No plan existed."}

    async def get_system_prompt_section(self, context: TurnContext | None) -> str | None:
        return _PLAN_PROMPT_SECTION

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        interceptors: list[TurnInterceptor] = [
            PlanContextInterceptor(self._plans),
            PlanEventInterceptor(self._plans),
        ]
        if self._auto_trigger:
            interceptors.append(PlanAutoTriggerInterceptor(self._plans))
        return interceptors


class PlanContextInterceptor:
    def __init__(self, plans: dict[str, _Plan]) -> None:
        self._plans = plans

    async def intercept(self, stage: str, context: TurnContext) -> None:
        if stage != "before_llm":
            return
        plan = self._plans.get(context.chat_id)
        if plan is None or plan.status in _TERMINAL_PLAN_STATUSES:
            context.clear_ephemeral_message("plan.current_plan")
            context.clear_ephemeral_message("plan.needs_input")
            return
        context.set_ephemeral_message(
            "plan.current_plan",
            {"role": "user", "content": _render_current_plan(plan)},
        )
        if plan.status == "needs_input":
            context.set_ephemeral_message(
                "plan.needs_input",
                {"role": "user", "content": _NEEDS_INPUT_NUDGE},
            )
        else:
            context.clear_ephemeral_message("plan.needs_input")


class PlanEventInterceptor:
    """Emits plan state events so channels (e.g. the TUI) can render the current plan."""

    def __init__(self, plans: dict[str, _Plan]) -> None:
        self._plans = plans
        self._emitted: dict[str, float] = {}

    async def intercept(self, stage: str, context: TurnContext) -> None:
        if stage not in ("after_tool", "final_response"):
            return
        event_sink = getattr(context, "event_sink", None)
        if event_sink is None:
            return
        plan = self._plans.get(context.chat_id)
        if plan is None:
            if context.chat_id not in self._emitted:
                return
            del self._emitted[context.chat_id]
            payload = None
        else:
            if self._emitted.get(context.chat_id) == plan.updated_at:
                return
            self._emitted[context.chat_id] = plan.updated_at
            payload = plan.to_payload()
        from bos.protocol import TurnEvent

        await event_sink.emit(
            TurnEvent(
                event_type="plan",
                phase="update",
                chat_id=context.chat_id,
                turn_id=context.turn_id,
                agent_name=context.agent_name,
                stage=stage,
                detail="plan_state",
                content="",
                metadata={"plan": payload},
            )
        )


class PlanAutoTriggerInterceptor:
    def __init__(self, plans: dict[str, _Plan]) -> None:
        self._plans = plans

    async def intercept(self, stage: str, context: TurnContext) -> None:
        if stage != "prepare":
            return
        if not _plan_tools_available(context):
            return
        existing = self._plans.get(context.chat_id)
        if existing is not None and existing.status not in _TERMINAL_PLAN_STATUSES:
            return
        user_text = _latest_user_text(context)
        if not user_text or not _should_auto_plan(user_text):
            return
        self._plans[context.chat_id] = _Plan(
            objective=user_text,
            status="in_progress",
            current_context=list(_AUTO_PLAN_CONTEXT),
        )


def _plan_tools_available(context: TurnContext) -> bool:
    tool_names = {
        tool_def.get("function", {}).get("name")
        for tool_def in context.tool_defs
        if isinstance(tool_def.get("function"), dict)
    }
    return {"PlanCreate", "PlanUpdate"} <= tool_names


def _latest_user_text(context: TurnContext) -> str:
    for message in reversed(context.current):
        if message.llm_message.get("role") != "user":
            continue
        return _message_content_to_text(message.llm_message.get("content", ""))
    return ""


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts).strip()
    return str(content).strip()


def _should_auto_plan(text: str) -> bool:
    lowered = text.lower()
    words = lowered.split()
    score = 0

    if len(words) >= 25:
        score += 1
    if any(marker in lowered for marker in ("think first", "plan", "design", "review the approach")):
        score += 2
    if any(marker in lowered for marker in ("cross-reference", "compare", "reconcile", "matrix", "multi-step")):
        score += 2
    if any(marker in lowered for marker in ("latest", "current", "today", "external", "report", "source")):
        score += 1
    if any(marker in lowered for marker in ("calculate", "compute", "estimate", "implied", "volatility")):
        score += 1
    if any(marker in lowered for marker in ("financial", "futures", "options", "market", "cbot", "usda")):
        score += 1
    if any(marker in lowered for marker in ("refactor", "migrate", "implement", "multi-file")) and len(words) >= 12:
        score += 1
    if lowered.count(" and ") >= 2 or "," in lowered and " and " in lowered:
        score += 1

    return score >= 2
