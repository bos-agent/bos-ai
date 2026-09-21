"""runner.start() — bootstrap the BEP 7 gateway runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from bos.config import Workspace
    from bos.gateway import Gateway

logger = logging.getLogger(__name__)


async def start(workspace: Workspace, *, on_ready: Callable[[Gateway], None] | None = None) -> None:
    """Launch the gateway runtime for a workspace, owning the listening socket.

    ``on_ready`` is handed the constructed gateway before it serves, so the
    composition root can wire signals to ``gateway.request_shutdown()`` — a
    graceful stop that drains in-flight turns, as opposed to cancelling this
    coroutine, which stops immediately.
    """
    from aiohttp import web

    from bos.gateway import Gateway

    logger.info("Starting BOS gateway runtime")
    async with workspace.harness() as harness:
        gateway = Gateway(runtime=workspace.resolve_gateway_runtime(), harness=harness)
        if on_ready is not None:
            on_ready(gateway)

        # Actors and channels come up *before* the socket listens — see
        # Gateway.start's docstring for what serving early loses.
        await gateway.start()
        app = gateway.build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, gateway.config.host, gateway.config.port)
        graceful = True
        try:
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
            await gateway.stop(graceful=graceful)
            await runner.cleanup()
