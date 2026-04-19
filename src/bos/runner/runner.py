"""runner.start() — orchestrates harness, actor(s), and configured channels in-process.

Intentionally NOT in core.py so that core stays focused on framework primitives.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bos.config import Workspace

logger = logging.getLogger(__name__)


async def start(workspace: Workspace) -> None:
    """Launch harness, agent actor(s), and all configured channels in-process.

    Reads from ``config.toml``::

        [[main.channels]]
        name = "HttpChannel"
        bind_address = "channel@http"
        target_address = "agent@main"
        host = "127.0.0.1"
        port = 5920

    Blocks until all tasks complete (i.e. until cancelled via SIGTERM or
    ``asyncio.CancelledError``).
    """
    from bos.core import AgentActor, Channel, _create_extension_instance, ep_channel

    agent_name = workspace.get_main_agent_name()
    actor_address = workspace.get_main_agent_address()
    channels_cfg = workspace.resolve_channels(runtime_kind=os.environ.get("BOS_RUNTIME", "process"))

    logger.info("Starting harness for agent=%r with %d channel(s)", agent_name, len(channels_cfg))

    async with workspace.harness() as harness:
        agent = harness.create_agent(agent_name)
        actor = AgentActor(agent, harness.mail_route.bind(actor_address))

        channels: list[tuple[Channel, str]] = []
        for cfg in channels_cfg:
            ch = _create_extension_instance(ep_channel, Channel, cfg.extension_config())
            if ch is None:
                logger.warning("Could not create channel from config: %r", cfg)
                continue
            channels.append((ch, cfg.bind_address))

        async def _actor_and_channels() -> None:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(actor.run(), name="actor")
                for ch, address in channels:
                    tg.create_task(
                        ch.run(harness.mail_route.bind(address)),
                        name=f"channel:{address}",
                    )

        task = asyncio.create_task(_actor_and_channels())

        # Give channels a moment to bind (aiohttp TCPSite.start() is nearly instant)
        await asyncio.sleep(0.2)

        # Write channel endpoint info to agent.state
        try:
            from bos.runner.proc import RunDir, write_state

            rd = RunDir(workspace.bos_dir)
            if rd.root.exists():
                channel_info = []
                for ch, address in channels:
                    info: dict = {"address": address, "name": type(ch).__name__}
                    if hasattr(ch, "actual_host"):
                        info["host"] = ch.actual_host
                        info["port"] = ch.actual_port
                    channel_info.append(info)
                write_state(rd, channels=channel_info)
        except Exception as exc:
            logger.debug("Could not update agent.state with channel info: %s", exc)

        await task
