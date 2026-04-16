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
from datetime import datetime

logger = logging.getLogger(__name__)


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
        write_state(
            rd,
            pid=os.getpid(),
            started_at=datetime.now().isoformat(),
            last_active=datetime.now().isoformat(),
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


if __name__ == "__main__":
    main()
