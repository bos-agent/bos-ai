"""Standalone gateway process — launched by ``boscli gateway start``.

Usage (internal, via proc.start_background)::

    python -m bos.runner --config /path/to/bos.toml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
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
    parser = argparse.ArgumentParser(description="BOS gateway process")
    parser.add_argument("--config", default=None, help="Path to BOS config file")
    args = parser.parse_args()

    # Bootstrap workspace
    from bos.config import Workspace, resolve_config_source
    from bos.gateway.state import GatewayRunDir
    from bos.runner import start

    if args.config:
        config_path, bos_dir, config = resolve_config_source(args.config)
        ws = Workspace(".", bos_dir, config, config_file=config_path)
    else:
        ws = Workspace.from_discovery(".")
    ws.resolve_agents()
    ws.bootstrap_platform()

    rd = GatewayRunDir(ws.bos_dir)
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
        logger.info("Gateway process started (PID %d, workspace=%s)", os.getpid(), ws.workspace)
        rd.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        try:
            await start(ws)
        except asyncio.CancelledError:
            logger.info("Gateway cancelled — exiting cleanly")
        finally:
            rd.pid_file.unlink(missing_ok=True)
            logger.info("Gateway process stopped")

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
