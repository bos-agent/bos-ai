import asyncio
import contextlib
import io
import traceback

from bos.core import ep_tool

_REPL_GLOBALS = {}


@ep_tool(
    name="bash",
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
)
async def tool_bash(command: str, cwd: str = ".", timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
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
        return f"Error: Command timed out after {timeout} seconds."
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
    try:
        proc = await asyncio.create_subprocess_exec(
            "pwsh",
            "-NonInteractive",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
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
        return f"Error: Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return "Error: pwsh (PowerShell) not found on system."
    except Exception as e:
        return f"Error executing PowerShell: {e}"


@ep_tool(
    name="REPL",
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
