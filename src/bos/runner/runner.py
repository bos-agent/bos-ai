"""runner.start() — orchestrates harness, actor(s), and channels in-process.

Intentionally NOT in core.py so that core stays focused on framework primitives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bos.config import Workspace

logger = logging.getLogger(__name__)


async def start(workspace: Workspace) -> None:
    """Launch harness, agent actor(s), and all configured channels in-process.

    Reads from ``config.toml``::

        [main]
        agent = "main"

        [[main.channels]]
        name    = "HttpChannel"
        address = "http"
        host    = "127.0.0.1"
        port    = 8080

    When multiple channels are configured a ``BroadcastChannel`` multiplexes
    them behind a single sender address so the actor stays channel-agnostic.

    Blocks until all tasks complete (i.e. until cancelled via SIGTERM or
    ``asyncio.CancelledError``).
    """
    from bos.core import AgentActor, Channel, _create_extension_instance, ep_channel
    from bos.extensions.channels.broadcast import BroadcastChannel

    agent_name: str = workspace.get_setting("main.agent") or "_default"
    channels_cfg: list[dict] = workspace.config.get("main", {}).get("channels", [{"name": "HttpChannel"}])

    logger.info("Starting harness for agent=%r with %d channel(s)", agent_name, len(channels_cfg))

    async with workspace.harness() as harness:
        agent = harness.create_agent(agent_name)
        actor_address = f"agent@{agent_name}"
        actor = AgentActor(actor_address, agent, harness.mailbox)
        broadcast_address = workspace.get_setting("main.broadcast_address")
        target_address = broadcast_address or actor_address

        channels: list[tuple[Channel, str]] = []
        for cfg in channels_cfg:
            ch = _create_extension_instance(ep_channel, Channel, {"target_address": target_address} | cfg)
            if ch is None:
                logger.warning("Could not create channel from config: %r", cfg)
                continue
            channels.append((ch, f"channel@{cfg.get('address', 'http')}"))

        async def _actor_and_channels() -> None:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(actor.run(), name="actor")
                if broadcast_address:
                    member_addresses = [addr for _, addr in channels]
                    bc = BroadcastChannel(member_addresses, actor_address)
                    tg.create_task(bc.run(harness.mailbox, broadcast_address), name="broadcast")
                for ch, address in channels:
                    tg.create_task(
                        ch.run(harness.mailbox, address),
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
                    info: dict = {"address": address}
                    if hasattr(ch, "actual_host"):
                        info["name"] = type(ch).__name__
                        info["host"] = ch.actual_host
                        info["port"] = ch.actual_port
                    channel_info.append(info)
                write_state(rd, channels=channel_info)
        except Exception as exc:
            logger.debug("Could not update agent.state with channel info: %s", exc)

        await task
