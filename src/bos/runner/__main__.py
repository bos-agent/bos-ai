"""Standalone gateway process — launched by ``boscli gateway start``.

Usage (internal, via proc.start_background)::

    python -m bos.runner --config /path/to/.bos/config.toml

This process is a *driver*, not a second gateway implementation: it builds a
``GatewayMount`` and hands it to ``runner.serve``, the same pair an embedded
host uses (BEP 17 §2.1.6). Everything singleton-related — taking the lock,
standing by when another process holds it, watching that ownership holds — is
the mount's, not this module's.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="BOS gateway process")
    parser.add_argument("--config", default=None, help="Path to BOS config file")
    args = parser.parse_args()

    # Bootstrap workspace
    from bos.config import Workspace, resolve_config_source
    from bos.gateway.state import GatewayRunDir
    from bos.runner.mount import GatewayMount
    from bos.runner.runner import serve, shielded

    def _workspace_factory() -> Workspace:
        """Build a Workspace from this process's arguments.

        The mount is handed the callable rather than the object because a
        restart must re-read configuration. ``resolve_agents`` /
        ``bootstrap_platform`` are deliberately *not* called here: the mount
        runs them once it holds the singleton lock, so an instance that loses
        the race imports no extension modules and writes no ``os.environ``.
        """
        if args.config:
            config_path, bos_dir, config = resolve_config_source(args.config)
            return Workspace(".", bos_dir, config, config_file=config_path)
        return Workspace.from_discovery(".")

    ws = _workspace_factory()
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

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task: asyncio.Task | None = None
    mount: Any = None

    def _on_sigterm(*_) -> None:
        # Resolved now, not captured at bring-up: a hot restart replaces the
        # Gateway behind the mount, and a handler holding the old object would
        # aim request_shutdown() at something nothing is listening to — the
        # first signal would do nothing and only the second, forceful one would
        # stop the process.
        gateway = mount.gateway if mount is not None else None
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

    async def _run() -> None:
        nonlocal mount
        logger.info("Gateway process started (PID %d, workspace=%s)", os.getpid(), ws.workspace)
        mount = GatewayMount(_workspace_factory, runtime_label="process")
        # Only the winner of the lock writes gateway.pid, so only the winner may
        # remove it. Every other path through the finally below — standby,
        # demotion, a cancel during bring-up — would otherwise delete a file
        # that belongs to whichever process actually holds the lock, which is
        # the clobber proc.start_background refuses to risk.
        wrote_pid = False
        # mount.start() is *inside* the try: until it returns there is no
        # gateway, so every SIGTERM takes _on_sigterm's escalation branch and
        # cancels this task mid-bring-up — with the lock taken and possibly the
        # actors and channels already up. That cancel has to land on a teardown,
        # not escape as a traceback.
        try:
            await mount.start()
            if mount.state != "live":
                # Another live gateway holds the flock for this run dir — however
                # this one was launched (a duplicate `gateway start`, an `ask`
                # auto-start racing an existing gateway). Exit rather than become
                # a second poller, leaving gateway.pid as the live process wrote
                # it: `stop` and `restart` must still find that process.
                logger.error("Another BOS gateway already running for %s — exiting.", ws.bos_dir)
                return
            rd.pid_file.write_text(str(os.getpid()), encoding="utf-8")
            wrote_pid = True
            await serve(mount)
        except asyncio.CancelledError:
            logger.info("Gateway cancelled — exiting cleanly")
        finally:
            # A no-op after serve(), which stops the mount itself; the work is
            # for the bring-up and standby paths that never reached it. Shielded
            # because the second SIGTERM of an escalating stop lands right here.
            await shielded(mount.stop(graceful=False))
            if wrote_pid:
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
