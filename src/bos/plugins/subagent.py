"""SubagentPlugin — subagent delegation via AskSubagent."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from bos.core._utils import _allowed, _pick_collection, _xml_attr
from bos.core.contract import (
    AgentPlugin,
    PluginServices,
    SubagentRuntime,
    TurnInterceptor,
    ep_plugin,
)
from bos.core.registry import ToolRegistry

if TYPE_CHECKING:
    from bos.core.agent import TurnContext
    from bos.core.contract import ToolContext

logger = logging.getLogger(__name__)


@ep_plugin(name="SubagentPlugin")
class SubagentHarnessPlugin:
    @property
    def name(self) -> str:
        return "SubagentPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {"allow": None, "exclude": [], "task_template": None}

    async def setup(self, services: PluginServices) -> None:
        self._runtime: SubagentRuntime = services.subagents

    def validate_config(self, config: Mapping[str, Any]) -> None:
        allow = config.get("allow")
        if allow is not None and not isinstance(allow, (str, list)):
            raise TypeError("SubagentPlugin: 'allow' must be a string, list, or None")
        exclude = config.get("exclude")
        if exclude is not None and not isinstance(exclude, list):
            raise TypeError("SubagentPlugin: 'exclude' must be a list or None")

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        allow = config.get("allow")
        exclude = config.get("exclude", [])
        if allow is None:
            allow = []  # nothing allowed
        elif isinstance(allow, str) and allow == "*":
            allow = None  # None means all allowed in _allowed / _pick_collection
        return SubagentAgentPlugin(self._runtime, allow, exclude, task_template=config.get("task_template"))

    async def teardown(self) -> None:
        pass


_SUBAGENT_TOOL_USAGE = {
    "AskSubagent": """Delegate a task to an allowed named subagent and return its response.

Use when a configured subagent is a better fit than doing the work locally: broad codebase
exploration, independent research, planning, implementation review, or isolated subtasks that
would otherwise flood the main context. Do not delegate the immediate blocking next step if the
main agent should do it directly.

Guidelines:
- Write task like a brief to a capable teammate with no conversation history.
- Include the goal, why it matters, what you already know or ruled out, relevant files/lines,
  constraints, whether edits are allowed, and the output you need.
- For lookups, hand over the exact command or target when known; for investigations, hand over
  the question rather than over-prescribed steps.
- Do not ask the subagent to both discover and decide your main implementation. Synthesize the
  result yourself, then make or request the concrete change.
- Verify or integrate the result before treating the parent task as complete.""",
}

_SUBAGENT_PROMPT_SECTION = """<subagent_workflow>
Use subagents for broad exploration, independent research, planning, review, or isolated side work.

- Use direct tools for known files, specific symbols, and immediate blocking next steps.
- Delegate bounded side tasks that can run independently or protect the main context from noisy output.
- Do not duplicate work between the main agent and a subagent.
- Brief subagents with self-contained task context; they do not know the full conversation.
- Include goal, known facts, relevant files or lines, constraints, edit permission, and desired output.
- Treat subagent results as input to your own synthesis; verify important claims before completion.
</subagent_workflow>"""


class SubagentAgentPlugin:
    def __init__(
        self,
        runtime: SubagentRuntime,
        allow: list[str] | str | None,
        exclude: list[str],
        *,
        task_template: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._allow = allow
        self._exclude = exclude
        self._task_template = task_template

    @property
    def name(self) -> str:
        return "SubagentPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        runtime = self._runtime
        allow = self._allow
        exclude = self._exclude
        task_template = self._task_template

        @registry(
            name="AskSubagent",
            description="Delegate a task to a named subagent and return its response.",
            usage=_SUBAGENT_TOOL_USAGE["AskSubagent"],
            parallel_safe=True,
            parameters={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "The role (kind) of the subagent to delegate to, case sensitive.",
                    },
                    "task": {"type": "string", "description": "Self-contained task brief to send to the subagent."},
                },
                "required": ["role", "task"],
            },
        )
        async def ask_subagent(
            role: str,
            task: str | None = None,
            context: ToolContext | None = None,
        ) -> str:
            from bos.core import AgentRegistry
            from bos.core._utils import _safe_format

            if not _allowed(role, allow, exclude):
                return f"Error: Agent '{role}' is not an allowed subagent."
            if not AgentRegistry.has_registered(role):
                return f"Error: Agent '{role}' not found."
            if not task:
                return "Error: AskSubagent requires a non-empty task."
            if context is None:
                return "Error: AskSubagent requires a ToolContext."

            if task_template:
                task = _safe_format(task_template, role=role, task=task, message=task)

            return await runtime.ask(role, task, parent=context)

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:
        from bos.core import AgentRegistry

        available = dict(AgentRegistry.describe())
        available.pop("_default", None)

        available = _pick_collection(available, self._allow, self._exclude)
        if not available:
            return None
        sections = [_SUBAGENT_PROMPT_SECTION]
        try:
            limit = int(os.environ.get("BOS_CAPABILITY_LIMIT", 50))
        except Exception:
            limit = 50
        if len(available) > limit:
            logger.warning(
                "Rendering only the first %d subagents in the system prompt; %d are available.",
                limit,
                len(available),
            )
            available = dict(list(available.items())[:limit])
        available_subagents = "<available_subagents>\n"
        available_subagents += "\n".join(
            f'<agent role="{_xml_attr(name)}">{escape(desc or "")}</agent>'
            for name, desc in available.items()
        )
        available_subagents += "\n</available_subagents>"
        sections.append(available_subagents)
        return "\n\n".join(sections)

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        return []
