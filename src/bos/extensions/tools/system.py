import asyncio
import contextlib
import io
import os
import signal
import traceback
from typing import Any

from bos.core import ep_tool

_REPL_GLOBALS = {}


def _subprocess_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
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


@ep_tool(
    name="Bash",
    description="Execute a shell command in the current workspace.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The bash command to execute."},
            "cwd": {"type": "string", "description": "Optional working directory.", "default": "."},
            "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 60},
        },
        "required": ["command"],
    },
    usage="""Execute shell commands for shell-native work.

Use for tests, package commands, git inspection, build tools, and commands not covered by a dedicated tool.
Prefer ReadFile, WriteFile, EditFile, GlobSearch, and GrepSearch for file operations.

Guidelines:
- Keep commands bounded and scoped to the relevant workspace.
- Follow repository instructions for command runners and toolchains.
- Avoid destructive filesystem or git operations unless the user explicitly requested them.
- If a command fails, inspect the error and change approach instead of repeating blindly.
""",
)
async def tool_bash(command: str, cwd: str = ".", timeout: int = 60) -> str:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **_subprocess_kwargs(),
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
            await _terminate_process(proc)
        return f"Error: Command timed out after {timeout} seconds."
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process(proc)
        raise
    except Exception as e:
        return f"Error executing bash: {e}"


@ep_tool(
    name="PowerShell",
    description="Execute a PowerShell command (primarily for Windows).",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The PowerShell command to execute."},
            "cwd": {"type": "string", "description": "Optional working directory.", "default": "."},
            "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 60},
        },
        "required": ["command"],
    },
)
async def tool_powershell(command: str, cwd: str = ".", timeout: int = 60) -> str:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "pwsh",
            "-NonInteractive",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **_subprocess_kwargs(),
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
            await _terminate_process(proc)
        return f"Error: Command timed out after {timeout} seconds."
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process(proc)
        raise
    except FileNotFoundError:
        return "Error: pwsh (PowerShell) not found on system."
    except Exception as e:
        return f"Error executing PowerShell: {e}"


@ep_tool(
    name="Repl",
    description="Execute Python code in a persistent REPL environment.",
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code snippet to execute."},
        },
        "required": ["code"],
    },
)
async def tool_repl(code: str) -> str:
    return await asyncio.to_thread(_sync_tool_repl, code)


def _sync_tool_repl(code: str) -> str:
    global _REPL_GLOBALS
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                comp = compile(code, "<repl>", "eval")
                res = eval(comp, _REPL_GLOBALS)
                if res is not None:
                    print(repr(res))
            except SyntaxError:
                comp = compile(code, "<repl>", "exec")
                exec(comp, _REPL_GLOBALS)
    except Exception:
        traceback.print_exc(file=stderr)

    out = stdout.getvalue()
    err = stderr.getvalue()
    result = out
    if err:
        if result:
            result += "\n"
        result += err
    return result.strip() or "(Execution succeeded with no output)"


@ep_tool(
    name="Sleep",
    description="Wait for a specified duration.",
    parameters={
        "type": "object",
        "properties": {
            "duration": {"type": "integer", "description": "Wait duration in seconds."},
        },
        "required": ["duration"],
    },
)
async def tool_sleep(duration: int) -> str:
    await asyncio.sleep(duration)
    return f"Slept for {duration} seconds."
