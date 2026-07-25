"""Standalone gateway process — launched by ``boscli gateway start``.

Usage (internal, via proc.start_background)::

    python -m bos.runner --config /path/to/.bos/config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger(__name__)

# How often the running gateway re-checks that it still owns the singleton lock.
_LOCK_WATCH_INTERVAL = 5.0


def main() -> None:
    parser = argparse.ArgumentParser(description="BOS gateway process")
    parser.add_argument("--config", default=None, help="Path to BOS config file")
    args = parser.parse_args()

    # Bootstrap workspace
    from bos.config import Workspace, resolve_config_source
    from bos.gateway.state import GatewayRunDir
    from bos.runner import start
    from bos.runner.proc import acquire_singleton_lock

    if args.config:
        config_path, bos_dir, config = resolve_config_source(args.config)
        ws = Workspace(".", bos_dir, config, config_file=config_path)
    else:
        ws = Workspace.from_discovery(".")
    ws.resolve_agents()
    ws.bootstrap_platform()

    rd = GatewayRunDir(ws.bos_dir)
    rd.ensure()

    # Configure logging to include timestamps. BOS_LOG_LEVEL overrides the default
    # so operators can raise verbosity (e.g. DEBUG) without code changes.
    _level_name = (os.environ.get("BOS_LOG_LEVEL") or "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, _level_name, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # Singleton guard: only one gateway may run per workspace run dir. The lock
    # is held for this process's lifetime and released automatically on exit or
    # crash (the OS drops the flock), so a second runner — however it was
    # launched (duplicate `gateway start`, an `ask` auto-start racing an
    # existing gateway) — refuses rather than starting a second poller.
    singleton_lock = acquire_singleton_lock(rd)
    if singleton_lock is None:
        logger.error("Another BOS gateway already running for %s — exiting.", ws.bos_dir)
        sys.exit(0)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task: asyncio.Task | None = None
    gateway: Any = None

    def _on_sigterm(*_) -> None:
        # First signal asks for a graceful stop: in-flight turns are told to
        # close with a handoff, within the configured grace. A second signal
        # (an impatient operator, or the CLI escalating) cancels outright.
        if gateway is not None and not gateway.shutdown_requested:
            logger.info("SIGTERM received — draining in-flight turns, then shutting down")
            loop.call_soon_threadsafe(gateway.request_shutdown)
            return
        logger.info("SIGTERM received — stopping now")
        if main_task and not main_task.done():
            loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGTERM, _on_sigterm)

    def _gateway_ready(instance: Any) -> None:
        nonlocal gateway
        gateway = instance

    async def _watch_singleton_lock(target: asyncio.Task | None) -> None:
        # Singleton authority must hold for the whole process lifetime, not just
        # at startup. If this gateway ever stops owning the on-disk lock (the
        # file was wiped/recreated and another instance took over), stand down
        # rather than becoming a second poller that duplicates message delivery.
        from bos.runner.proc import lock_still_owned

        if target is not None:
            while True:
                await asyncio.sleep(_LOCK_WATCH_INTERVAL)
                if not lock_still_owned(rd, singleton_lock):
                    logger.error(
                        "Lost singleton lock ownership for %s — another gateway has taken over; shutting down.",
                        ws.bos_dir,
                    )
                    target.cancel()
                    return

    async def _run() -> None:
        logger.info("Gateway process started (PID %d, workspace=%s)", os.getpid(), ws.workspace)
        rd.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        watch_task = asyncio.ensure_future(_watch_singleton_lock(asyncio.current_task()))
        try:
            await start(ws, on_ready=_gateway_ready)
        except asyncio.CancelledError:
            logger.info("Gateway cancelled — exiting cleanly")
        finally:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task
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


if __name__ == "__main__":
    main()
