"""runner.start() — bootstrap the BEP 7 gateway runtime."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bos.config import Workspace

logger = logging.getLogger(__name__)


async def start(workspace: Workspace) -> None:
    """Launch the gateway runtime for a workspace.

    The runner is now only bootstrap: it opens the harness service container,
    constructs the gateway process root, and awaits ``gateway.run()``.
    """
    from bos.gateway import Gateway

    logger.info("Starting BOS gateway runtime")
    async with workspace.harness() as harness:
        gateway = Gateway(runtime=workspace.resolve_gateway_runtime(), harness=harness)
        await gateway.run()
