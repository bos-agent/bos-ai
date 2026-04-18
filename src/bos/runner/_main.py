"""Standalone actor + channel process — launched by ``bos start``.

Usage (internal, via proc.start_background)::

    python -m bos.runner._main --workspace /path/to/workspace
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from typing import TextIO

logger = logging.getLogger(__name__)


class _TeeStream:
    """Mirror writes to the original stream and a persistent log file."""

    def __init__(self, primary: TextIO, mirror: TextIO) -> None:
        self._primary = primary
        self._mirror = mirror

    def write(self, data: str) -> int:
        written = self._primary.write(data)
        self._mirror.write(data)
        return written

    def flush(self) -> None:
        self._primary.flush()
        self._mirror.flush()

    def __getattr__(self, name: str):
        return getattr(self._primary, name)


def main() -> None:
    parser = argparse.ArgumentParser(description="BOS agent actor process")
    parser.add_argument("--workspace", default=".", help="Path to workspace directory")
    args = parser.parse_args()

    # Bootstrap workspace
    from bos.config import Workspace
    from bos.runner.proc import RunDir, write_state
    from bos.runner.runner import start as runner_start

    ws = Workspace(args.workspace)
    ws.bootstrap_platform()

    rd = RunDir(ws.bos_dir)
    rd.ensure()
    runtime_kind = os.environ.get("BOS_RUNTIME", "process")
    mirrored_log: TextIO | None = None

    if runtime_kind == "docker":
        mirrored_log = rd.log_file.open("a", encoding="utf-8")
        sys.stdout = _TeeStream(sys.stdout, mirrored_log)
        sys.stderr = _TeeStream(sys.stderr, mirrored_log)

    # Configure logging to include timestamps
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task: asyncio.Task | None = None

    def _on_sigterm(*_) -> None:
        logger.info("SIGTERM received — shutting down")
        if main_task and not main_task.done():
            loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGTERM, _on_sigterm)

    async def _run() -> None:
        runtime_kind = os.environ.get("BOS_RUNTIME", "process")
        container_id = None
        container_name = None
        if runtime_kind == "docker":
            container_id = os.environ.get("BOS_CONTAINER_ID") or os.environ.get("HOSTNAME")
            container_name = os.environ.get("BOS_CONTAINER_NAME")
        write_state(
            rd,
            runtime=runtime_kind,
            pid=os.getpid(),
            container_id=container_id,
            container_name=container_name,
            started_at=datetime.now(timezone.utc).isoformat(),
            last_active=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("Actor process started (PID %d, workspace=%s)", os.getpid(), ws.workspace)
        try:
            await runner_start(ws)
        except asyncio.CancelledError:
            logger.info("Actor cancelled — exiting cleanly")
        finally:
            rd.pid_file.unlink(missing_ok=True)
            rd.state_file.unlink(missing_ok=True)
            logger.info("Actor process stopped")

    main_task = loop.create_task(_run())
    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        if main_task and not main_task.done():
            main_task.cancel()
            loop.run_until_complete(main_task)
    finally:
        loop.close()
        if mirrored_log is not None:
            mirrored_log.close()


if __name__ == "__main__":
    main()
