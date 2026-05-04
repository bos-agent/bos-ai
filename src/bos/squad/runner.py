from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bos.config import Workspace

logger = logging.getLogger(__name__)


def _parse_actors_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    main = config.get("main", {})
    if not isinstance(main, dict):
        return {}
    actors = main.get("actors", {})
    if not isinstance(actors, dict):
        return {}
    return {str(k): dict(v) for k, v in actors.items() if isinstance(v, dict)}


async def start_squad(workspace: Workspace) -> None:
    from bos.core import AgentActor, Channel, _create_extension_instance, ep_channel
    from bos.core.chat_state import ChatState
    from bos.squad.actor import SquadActor
    from bos.squad.registry import ActorRegistry

    actors_cfg = _parse_actors_config(workspace.config)
    channels_cfg = workspace.resolve_channels(
        runtime_kind=os.environ.get("BOS_RUNTIME", "process")
    )

    if not actors_cfg:
        agent_name = workspace.get_main_agent_name()
        actor_address = workspace.get_main_agent_address()
        logger.info(
            "No [main.actors] configured; starting single actor agent=%r",
            agent_name,
        )
        async with workspace.harness() as harness:
            chat_state = ChatState(workspace.bos_dir)
            agent = harness.create_agent(agent_name)
            actor = AgentActor(
                agent, harness.mail_route.bind(actor_address), chat_state=chat_state
            )
            channels = _create_channels(channels_cfg, ep_channel, Channel)
            await _run_actor_and_channels(actor, channels, harness)
        return

    actor_names = list(actors_cfg.keys())
    logger.info(
        "Starting squad with %d actor(s): %s",
        len(actor_names),
        ", ".join(actor_names),
    )

    async with workspace.harness() as harness:
        chat_state = ChatState(workspace.bos_dir)
        registry = ActorRegistry()
        actors: list[SquadActor] = []

        for routing_name, cfg in actors_cfg.items():
            agent_name = cfg.get("agent", routing_name)
            address = f"agent@{routing_name}"
            mailbox = harness.mail_route.bind(address)
            is_default = routing_name == "main"

            agent = _build_squad_agent(harness, agent_name, workspace.config)
            actor = SquadActor(
                agent, mailbox, chat_state=chat_state, actor_name=routing_name
            )
            actors.append(actor)
            registry.register(routing_name, mailbox, is_default=is_default)

        channels = _create_channels(channels_cfg, ep_channel, Channel, registry=registry)

        async with asyncio.TaskGroup() as tg:
            for actor in actors:
                tg.create_task(actor.run(), name=f"actor:{actor.actor_name}")
            for ch, address in channels:
                tg.create_task(
                    ch.run(harness.mail_route.bind(address)),
                    name=f"channel:{address}",
                )


def _create_channels(
    channels_cfg, ep_channel, Channel, registry=None
) -> list[tuple[Channel, str]]:
    from bos.core import _create_extension_instance

    channels: list[tuple[Channel, str]] = []
    for cfg in channels_cfg:
        ext_cfg = cfg.extension_config()
        if registry is not None:
            ext_cfg["actor_registry"] = registry
        ch = _create_extension_instance(ep_channel, Channel, ext_cfg)
        if ch is None:
            logger.warning("Could not create channel from config: %r", cfg)
            continue
        channels.append((ch, cfg.bind_address))
    return channels


async def _run_actor_and_channels(
    actor, channels, harness
) -> None:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(actor.run(), name="actor")
        for ch, address in channels:
            tg.create_task(
                ch.run(harness.mail_route.bind(address)),
                name=f"channel:{address}",
            )


def _build_squad_agent(harness, agent_name: str, config: dict[str, Any]):
    from bos.squad.actor import SquadAgent

    agents = config.get("platform", {}).get("agents", [])
    agent_spec: dict[str, Any] = {}
    for a in agents:
        if isinstance(a, dict) and a.get("name") == agent_name:
            agent_spec = {k: v for k, v in a.items() if k != "name"}
            break

    defaults = config.get("platform", {}).get("agent_defaults", {})
    if isinstance(defaults, dict):
        for k, v in defaults.items():
            agent_spec.setdefault(k, v)

    return SquadAgent(
        name=agent_name,
        llm=harness.llm,
        message_store=harness.message_store,
        memory=harness.memory,
        consolidator=harness.consolidator,
        skills_loader=harness.skills_loader,
        interceptor=harness.interceptor,
        tool_configs=harness._tools_cfg,
        **agent_spec,
    )
