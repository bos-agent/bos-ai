import asyncio
import shlex
import sys

import pytest

from bos.extensions.tools.system import tool_bash


async def _wait_for_path(path, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {path}")


def _signal_script(*, ready_path, terminated_path) -> str:
    return "\n".join([
        "import pathlib",
        "import signal",
        "import sys",
        "import time",
        f"ready = pathlib.Path({str(ready_path)!r})",
        f"terminated = pathlib.Path({str(terminated_path)!r})",
        "def handle_term(signum, frame):",
        "    terminated.write_text(str(signum), encoding='utf-8')",
        "    sys.exit(0)",
        "signal.signal(signal.SIGTERM, handle_term)",
        "ready.write_text('ready', encoding='utf-8')",
        "while True:",
        "    time.sleep(1)",
    ])


@pytest.mark.asyncio
async def test_bash_timeout_terminates_subprocess_group(tmp_path):
    ready_path = tmp_path / "ready"
    terminated_path = tmp_path / "terminated"
    script = _signal_script(ready_path=ready_path, terminated_path=terminated_path)
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await tool_bash(command, cwd=str(tmp_path), timeout=1)

    assert result == "Error: Command timed out after 1 seconds."
    await _wait_for_path(terminated_path)


@pytest.mark.asyncio
async def test_bash_cancellation_terminates_subprocess_group(tmp_path):
    ready_path = tmp_path / "ready"
    terminated_path = tmp_path / "terminated"
    script = _signal_script(ready_path=ready_path, terminated_path=terminated_path)
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    task = asyncio.create_task(tool_bash(command, cwd=str(tmp_path), timeout=60))
    await _wait_for_path(ready_path)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_path(terminated_path)
