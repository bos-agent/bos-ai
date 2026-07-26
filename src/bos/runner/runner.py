"""runner.start() — bootstrap the BEP 7 gateway runtime."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from bos.config import Workspace
    from bos.gateway import Gateway

logger = logging.getLogger(__name__)


async def start(workspace: Workspace, *, on_ready: Callable[[Gateway], None] | None = None) -> None:
    """Launch the gateway runtime for a workspace.

    The runner is now only bootstrap: it opens the harness service container,
    constructs the gateway process root, and awaits ``gateway.run()``.

    ``on_ready`` is handed the constructed gateway before it runs, so the
    composition root can wire signals to ``gateway.request_shutdown()`` — a
    graceful stop that drains in-flight turns, as opposed to cancelling this
    coroutine, which stops immediately.
    """
    from bos.gateway import Gateway

    logger.info("Starting BOS gateway runtime")
    async with workspace.harness() as harness:
        gateway = Gateway(runtime=workspace.resolve_gateway_runtime(), harness=harness)
        if on_ready is not None:
            on_ready(gateway)
        await gateway.run()
