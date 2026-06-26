"""SubagentPlugin — subagent delegation via AskSubagent."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from bos.core._utils import _pick_collection, _xml_attr
from bos.core.contract import (
    AgentPlugin,
    AgentRunner,
    PluginServices,
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
        return {"enabled": [], "disabled": [], "task_template": None}

    async def setup(self, services: PluginServices) -> None:
        if services.agent_runner is None:
            raise RuntimeError("SubagentPlugin requires services.agent_runner")
        self._runner: AgentRunner = services.agent_runner

    def validate_config(self, config: Mapping[str, Any]) -> None:
        enabled = config.get("enabled")
        if enabled is not None and not isinstance(enabled, (str, list)):
            raise TypeError("SubagentPlugin: 'enabled' must be a list, '*', or None")
        if isinstance(enabled, str) and enabled != "*":
            raise ValueError("SubagentPlugin: 'enabled' string value must be '*'")
        if isinstance(enabled, list) and not all(isinstance(item, str) for item in enabled):
            raise TypeError("SubagentPlugin: 'enabled' list entries must be strings")
        disabled = config.get("disabled")
        if disabled is not None and not isinstance(disabled, list):
            raise TypeError("SubagentPlugin: 'disabled' must be a list or None")
        if isinstance(disabled, list) and not all(isinstance(item, str) for item in disabled):
            raise TypeError("SubagentPlugin: 'disabled' list entries must be strings")

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        enabled = _normalize_enabled(config.get("enabled", []))
        disabled = config.get("disabled", [])
        if not isinstance(disabled, list):
            disabled = []
        return SubagentAgentPlugin(self._runner, enabled, disabled, task_template=config.get("task_template"))

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


def _normalize_enabled(value: Any) -> list[str] | None:
    if value == "*":
        return None
    if isinstance(value, list):
        if "*" in value:
            return None
        return list(value)
    return []


class SubagentAgentPlugin:
    def __init__(
        self,
        runner: AgentRunner,
        enabled: list[str] | None,
        disabled: list[str],
        *,
        task_template: str | None = None,
    ) -> None:
        self._runner = runner
        self._enabled = enabled
        self._disabled = disabled
        self._task_template = task_template

    @property
    def name(self) -> str:
        return "SubagentPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        if not self._available_subagents():
            return

        runner = self._runner
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

            if role not in self._available_subagents():
                return f"Error: Agent '{role}' is not an enabled subagent."
            if not AgentRegistry.has_registered(role):
                return f"Error: Agent '{role}' not found."
            if not task:
                return "Error: AskSubagent requires a non-empty task."
            if context is None:
                return "Error: AskSubagent requires a ToolContext."

            if task_template:
                task = _safe_format(task_template, role=role, task=task, message=task)

            result = await runner.run(task, kind=role, parent=context.parent)
            return result.output

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:
        available = self._available_subagents()
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
            f'<agent role="{_xml_attr(name)}">{escape(desc or "")}</agent>' for name, desc in available.items()
        )
        available_subagents += "\n</available_subagents>"
        sections.append(available_subagents)
        return "\n\n".join(sections)

    def _available_subagents(self) -> dict[str, str]:
        from bos.core import AgentRegistry

        available = dict(AgentRegistry.describe())
        available.pop("_default", None)
        return _pick_collection(available, self._enabled, self._disabled)

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        return []
