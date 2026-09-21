"""runner.serve()/start() — the one socket loop in front of a GatewayMount."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from bos.runner.mount import GatewayMount

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from bos.config import Workspace
    from bos.gateway import Gateway

logger = logging.getLogger(__name__)


class GatewayAlreadyRunningError(RuntimeError):
    """Another gateway holds the singleton lock for this workspace's run dir.

    Named rather than a bare ``RuntimeError`` so a composition root can report
    it as the ordinary refusal it is — one clean line and a non-zero exit —
    without also swallowing a real failure from inside the runtime.
    """


async def shielded(coro: Coroutine[Any, Any, None]) -> None:
    """Await *coro* to completion even if the awaiting task is cancelled.

    Teardown is mandatory: an escalating signal must not leave the listening
    socket, the channel sessions, the on-disk state or the singleton lock
    behind. Every step under this is bounded, so it cannot hold a stop open.
    """
    task = asyncio.ensure_future(coro)
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)


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
        # Two ways to stop serving. Either the gateway was asked to shut down,
        # or the watchdog demoted this mount to standby after it lost the lock.
        # A demotion has to end the serving too: the gateway captured above is
        # gone and will never signal, so waiting only on it would park here
        # forever holding the port in front of no runtime — and the successor
        # that won the lock would then die on EADDRINUSE.
        # Both waits are the mount's, not the gateway's: a hot restart replaces
        # that object while this call stays parked, and a wait bound to the
        # instance captured above would go deaf to the live gateway's shutdown.
        waiters = [
            asyncio.ensure_future(mount.wait_for_shutdown()),
            asyncio.ensure_future(mount.wait_for_demotion()),
        ]
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if waiters[1] in done:
            logger.error("Gateway lost the singleton lock — releasing the socket and stopping.")
    except asyncio.CancelledError:
        graceful = False
        raise
    finally:
        # The order matters: the drain inside mount.stop still needs live
        # channels — and therefore a live socket — to deliver the replies it
        # produces, so the socket goes last. A demotion has already torn the
        # runtime down, which makes this stop() a lock release and nothing more.
        #
        # mount.stop is deliberately *not* shielded: the drain inside it is the
        # one cancellable part of a shutdown, and an escalating second signal
        # must reach it so it costs the remaining grace rather than a fresh
        # full one. Gateway.stop() suppresses that cancel itself and shields
        # its own teardown, so what follows the drain still completes. Only the
        # socket, which no cancel may leave behind, goes under the shield.
        try:
            await mount.stop(graceful=graceful)
        finally:
            await shielded(runner.cleanup())


async def start(workspace: Workspace, *, on_ready: Callable[[Gateway], None] | None = None) -> None:
    """Launch the gateway runtime for a workspace, owning the listening socket.

    ``on_ready`` is handed the gateway before it serves, so the composition root
    can wire signals to ``gateway.request_shutdown()`` — a graceful stop that
    drains in-flight turns, as opposed to cancelling this coroutine, which stops
    immediately. Since the mount both builds and starts the gateway, ``on_ready``
    now runs *after* ``Gateway.start()`` rather than before it.

    The mount is what makes this safe to call twice for one workspace: losing
    the singleton flock raises ``GatewayAlreadyRunningError`` instead of binding
    a socket in front of a gateway that was never brought up (BEP 17 §3.4.1).
    """
    logger.info("Starting BOS gateway runtime")
    mount = GatewayMount(lambda: workspace)
    try:
        await mount.start()
        if mount.state != "live":
            holder = mount.status().get("holder_pid")
            raise GatewayAlreadyRunningError(
                f"Gateway is already running{f' (process {holder})' if holder else ''}."
            )
        gateway = mount.gateway
        assert gateway is not None
        if on_ready is not None:
            on_ready(gateway)
        await serve(mount)
    finally:
        # serve() already stopped it on every path it owns; this covers the ones
        # it never reached, so the lock is never held by a returning caller.
        await shielded(mount.stop())
