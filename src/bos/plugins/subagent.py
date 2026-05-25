"""SubagentPlugin — subagent delegation via AskSubagent, AskClaudeCode, AskCodex."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from bos.core._utils import _allowed
from bos.core.contract import (
    AgentBindContext,
    AgentPlugin,
    PluginServices,
    SubagentRuntime,
    TurnInterceptor,
    ep_agent,
    ep_plugin,
)
from bos.core.registry import ToolRegistry

if TYPE_CHECKING:
    from bos.core.agent import TurnContext
    from bos.core.contract import ToolContext


@ep_plugin(name="SubagentPlugin")
class SubagentHarnessPlugin:
    @property
    def name(self) -> str:
        return "SubagentPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {"allow": "*", "exclude": []}

    async def setup(self, services: PluginServices) -> None:
        self._runtime: SubagentRuntime = services.subagents

    def validate_config(self, config: Mapping[str, Any], context: AgentBindContext) -> None:
        allow = config.get("allow")
        if allow is not None and not isinstance(allow, (str, list)):
            raise TypeError("SubagentPlugin: 'allow' must be a string, list, or None")
        exclude = config.get("exclude")
        if exclude is not None and not isinstance(exclude, list):
            raise TypeError("SubagentPlugin: 'exclude' must be a list or None")

    def bind(self, config: Mapping[str, Any], context: AgentBindContext) -> AgentPlugin:
        allow = config.get("allow")
        exclude = config.get("exclude", [])
        if isinstance(allow, str) and allow == "*":
            allow = None  # None means all allowed
        return SubagentAgentPlugin(self._runtime, allow, exclude)

    async def teardown(self) -> None:
        pass


async def _terminate_proc(proc: asyncio.subprocess.Process) -> None:
    """Best-effort terminate then kill a subprocess."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()


async def _run_cli_agent(
    binary: str,
    args: list[str],
    *,
    cwd: str = ".",
    timeout: int = 300,
) -> str:
    """Run a CLI coding-agent binary and return its combined output."""
    path = shutil.which(binary)
    if path is None:
        return f"Error: '{binary}' not found on PATH."

    posix_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        posix_kwargs["start_new_session"] = True

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **posix_kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = ""
        if stdout:
            output += stdout.decode("utf-8", errors="replace")
        if stderr:
            if output:
                output += "\n"
            output += stderr.decode("utf-8", errors="replace")
        return output.strip() or "(Execution succeeded with no output)"
    except asyncio.TimeoutError:
        if proc is not None:
            await _terminate_proc(proc)
        return f"Error: {binary} timed out after {timeout} seconds."
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_proc(proc)
        raise
    except Exception as e:
        return f"Error executing {binary}: {e}"


_SUBAGENT_TOOL_USAGE = {
    "AskSubagent": """Delegate a task to an allowed named subagent and return its response.

Use for broad codebase exploration, independent research, planning, implementation review, or
isolated subtasks that would otherwise flood the main context. Do not delegate the immediate
blocking next step if the main agent should do it directly.

Guidelines:
- Make the message self-contained: goal, context, relevant files, constraints, and expected output.
- Tell the subagent whether code changes are allowed or whether the task is read-only.
- Subagents cannot see the full conversation history. Summarize what they need to know.
- Verify subagent results before treating work as complete.""",
    "AskClaudeCode": """Run a one-shot task using the Claude Code CLI (`claude -p`).

Use when you need Claude Code to perform an isolated coding task — file edits, code generation,
explanation, or review — without maintaining an interactive session.

Guidelines:
- Provide a clear, self-contained prompt describing the task.
- Set an appropriate timeout for long-running tasks.
- The tool returns Claude Code's stdout/stderr output.""",
    "AskCodex": """Run a one-shot task using the OpenAI Codex CLI (`codex exec`).

Use when you need Codex to perform an isolated coding task — file edits, code generation,
explanation, or review — in non-interactive mode.

Guidelines:
- Provide a clear, self-contained prompt describing the task.
- Set an appropriate timeout for long-running tasks.
- The tool returns Codex's stdout/stderr output.""",
}


class SubagentAgentPlugin:
    def __init__(
        self,
        runtime: SubagentRuntime,
        allow: list[str] | str | None,
        exclude: list[str],
    ) -> None:
        self._runtime = runtime
        self._allow = allow
        self._exclude = exclude

    @property
    def name(self) -> str:
        return "SubagentPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        runtime = self._runtime
        allow = self._allow
        exclude = self._exclude

        @registry(
            name="AskSubagent",
            description="Delegate a task to a named subagent and return its response.",
            usage=_SUBAGENT_TOOL_USAGE["AskSubagent"],
            parameters={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "The role (kind) of the subagent to delegate to, case sensitive.",
                    },
                    "message": {"type": "string", "description": "Task or message to send."},
                },
                "required": ["role", "message"],
            },
        )
        async def ask_subagent(
            role: str,
            message: str | None = None,
            context: ToolContext | None = None,
        ) -> str:
            if not _allowed(role, allow, exclude):
                return f"Error: Agent '{role}' is not an allowed subagent."
            if not ep_agent.has(role):
                return f"Error: Agent '{role}' not found."
            if not message:
                return "Error: AskSubagent requires a non-empty message."
            if context is None:
                return "Error: AskSubagent requires a ToolContext."
            return await runtime.ask(role, message, parent=context)

        @registry(
            name="AskClaudeCode",
            description="Run a one-shot task using the Claude Code CLI.",
            usage=_SUBAGENT_TOOL_USAGE["AskClaudeCode"],
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The task prompt to send to Claude Code.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command.",
                        "default": ".",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds.",
                        "default": 300,
                    },
                },
                "required": ["prompt"],
            },
        )
        async def ask_claude_code(
            prompt: str, cwd: str = ".", timeout: int = 300, **_: Any
        ) -> str:
            return await _run_cli_agent("claude", ["-p", prompt], cwd=cwd, timeout=timeout)

        @registry(
            name="AskCodex",
            description="Run a one-shot task using the OpenAI Codex CLI.",
            usage=_SUBAGENT_TOOL_USAGE["AskCodex"],
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The task prompt to send to Codex.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command.",
                        "default": ".",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds.",
                        "default": 300,
                    },
                },
                "required": ["prompt"],
            },
        )
        async def ask_codex(
            prompt: str, cwd: str = ".", timeout: int = 300, **_: Any
        ) -> str:
            return await _run_cli_agent(
                "codex", ["exec", "--json", prompt], cwd=cwd, timeout=timeout,
            )

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:

        available = dict(ep_agent.describe())
        available.pop("_default", None)
        # Apply allow/exclude
        from bos.core._utils import _pick_collection

        available = _pick_collection(available, self._allow, self._exclude)
        if not available:
            return None
        try:
            limit = int(os.environ.get("BOS_CAPABILITY_LIMIT", 50))
        except Exception:
            limit = 50
        if len(available) > limit:
            available = dict(list(available.items())[:limit])
        section = "<available_subagents>\n"
        section += "\n\n".join([f"## {name}\n{desc}" for name, desc in available.items()])
        section += "\n</available_subagents>"
        return section

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        return []
