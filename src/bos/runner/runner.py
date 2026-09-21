"""runner.serve()/start() — the one socket loop in front of a GatewayMount."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from bos.runner.mount import GatewayMount

if TYPE_CHECKING:
    from collections.abc import Callable

    from bos.config import Workspace
    from bos.gateway import Gateway

logger = logging.getLogger(__name__)


async def serve(mount: GatewayMount) -> None:
    """Own the listening socket for a *live* mount until it shuts down.

    This is the only socket loop in BOS: ``python -m bos.runner``,
    ``boscli gateway start --foreground`` and ``start()`` all land here, so the
    standalone path is not a second implementation (BEP 17 §2.1.6). The mount
    supplies the app (built once, indirecting through it) and the gateway whose
    configured host/port we bind; the bound port is read back off the socket
    because ``port = 0`` means the configured value is not the real one.
    """
    from aiohttp import web

    gateway = mount.gateway
    if gateway is None:
        raise RuntimeError("serve() requires a live GatewayMount — there is no runtime to serve.")

    runner = web.AppRunner(mount.build_app(), access_log=None)
    graceful = True
    try:
        await runner.setup()
        site = web.TCPSite(runner, gateway.config.host, gateway.config.port)
        await site.start()
        server = getattr(site, "_server", None)
        sockets = server.sockets if server else None
        port = sockets[0].getsockname()[1] if sockets else gateway.config.port
        gateway.set_endpoint(gateway.config.host, port)
        await gateway.wait_for_shutdown()
    except asyncio.CancelledError:
        graceful = False
        raise
    finally:
        # Shielded together: an escalating third signal must not leave the
        # listening socket (runner.cleanup) behind any more than it may leave
        # the channel sessions, the on-disk state and the singleton lock behind
        # (mount.stop covers those). Every step here is bounded, so this cannot
        # hold the stop open. The order matters: the drain inside mount.stop
        # still needs live channels — and therefore a live socket — to deliver
        # the replies it produces, so the socket goes last.
        async def _finish() -> None:
            await mount.stop(graceful=graceful)
            await runner.cleanup()

        finish = asyncio.ensure_future(_finish())
        while not finish.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(finish)


async def start(workspace: Workspace, *, on_ready: Callable[[Gateway], None] | None = None) -> None:
    """Launch the gateway runtime for a workspace, owning the listening socket.

    ``on_ready`` is handed the constructed gateway before it serves, so the
    composition root can wire signals to ``gateway.request_shutdown()`` — a
    graceful stop that drains in-flight turns, as opposed to cancelling this
    coroutine, which stops immediately.

    The mount is what makes this safe to call twice for one workspace: losing
    the singleton flock raises instead of binding a socket in front of a
    gateway that was never brought up (BEP 17 §3.4.1).
    """
    logger.info("Starting BOS gateway runtime")
    mount = GatewayMount(lambda: workspace)
    try:
        await mount.start()
        if mount.state != "live":
            raise RuntimeError(f"Another BOS gateway already holds the singleton lock for {workspace.bos_dir}.")
        gateway = mount.gateway
        assert gateway is not None
        if on_ready is not None:
            on_ready(gateway)
        await serve(mount)
    finally:
        # serve() already stopped it on every path it owns; this covers the ones
        # it never reached, so the lock is never held by a returning caller.
        await mount.stop()
