from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bos.config import Workspace
    from bos.core import Channel

logger = logging.getLogger(__name__)

_ACTOR_RUNTIME_KEYS = {"agent", "display_name"}
_FORBIDDEN_ACTOR_KEYS = {"identity", "memory_scope", "role_label", "name"}


def _parse_actors_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    main = config.get("main", {})
    if not isinstance(main, dict):
        return {}
    actors = main.get("actors", {})
    if not isinstance(actors, dict):
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for key, value in actors.items():
        if not isinstance(value, dict):
            continue
        actor_name = str(key)
        cfg = dict(value)
        _validate_actor_config(actor_name, cfg)
        parsed[actor_name] = cfg
    return parsed


def _validate_actor_config(actor_name: str, cfg: dict[str, Any]) -> None:
    forbidden = sorted(_FORBIDDEN_ACTOR_KEYS.intersection(cfg))
    if forbidden:
        names = ", ".join(forbidden)
        raise ValueError(
            f"main.actors.{actor_name} uses forbidden field(s): {names}. "
            "The actor table key is the identity and memory scope; use 'agent' for the reusable agent kind."
        )


def _agent_overrides(actor_cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in actor_cfg.items() if k not in _ACTOR_RUNTIME_KEYS}


async def start_named_actors(workspace: Workspace) -> None:
    from bos.core import AgentActor, Channel, ep_channel
    from bos.core.chat_state import ChatState
    from bos.named_actors.actor import NamedActor
    from bos.named_actors.registry import ActorRegistry

    actors_cfg = _parse_actors_config(workspace.config)
    channels_cfg = workspace.resolve_channels(runtime_kind=os.environ.get("BOS_RUNTIME", "process"))

    if not actors_cfg:
        agent_kind = workspace.get_main_agent_kind()
        actor_address = workspace.get_main_agent_address()
        logger.info("No [main.actors] configured; starting single actor agent=%r", agent_kind)
        async with workspace.harness() as harness:
            chat_state = ChatState(workspace.bos_dir)
            agent = await harness.create_agent(agent_kind, agent_cfg={"agent_name": "main"})
            actor = AgentActor(agent, harness.mail_route.bind(actor_address), chat_state=chat_state)
            channels = _create_channels(channels_cfg, ep_channel, Channel)
            task = asyncio.create_task(_run_actor_and_channels(actor, channels, harness))
            await asyncio.sleep(0.2)
            _write_channel_state(workspace, channels)
            await task
        return

    actor_names = list(actors_cfg.keys())
    logger.info("Starting named actors with %d actor(s): %s", len(actor_names), ", ".join(actor_names))

    async with workspace.harness() as harness:
        chat_state = ChatState(workspace.bos_dir)
        registry = ActorRegistry()
        actors: list[NamedActor] = []

        for actor_name, cfg in actors_cfg.items():
            agent_kind = str(cfg.get("agent") or actor_name)
            display_name = cfg.get("display_name")
            display_name = str(display_name) if display_name is not None else None
            address = f"agent@{actor_name}"
            mailbox = harness.mail_route.bind(address)
            is_default = actor_name == "main"

            agent = await _build_named_agent(harness, agent_kind, actor_name, _agent_overrides(cfg))
            actor = NamedActor(
                agent,
                mailbox,
                chat_state=chat_state,
                display_name=display_name,
                agent_kind=agent_kind,
            )
            actors.append(actor)
            registry.register(
                actor_name,
                mailbox,
                is_default=is_default,
                display_name=display_name,
                agent_kind=agent_kind,
            )

        channels = _create_channels(channels_cfg, ep_channel, Channel, registry=registry)

        async def _run_named_actors() -> None:
            async with asyncio.TaskGroup() as tg:
                for actor in actors:
                    tg.create_task(actor.run(), name=f"actor:{actor._agent.name}")
                for ch, address in channels:
                    tg.create_task(ch.run(harness.mail_route.bind(address)), name=f"channel:{address}")

        task = asyncio.create_task(_run_named_actors())
        await asyncio.sleep(0.2)
        _write_channel_state(workspace, channels)
        await task


def _write_channel_state(workspace, channels) -> None:
    try:
        from bos.runner.proc import RunDir, write_state

        rd = RunDir(workspace.bos_dir)
        if rd.root.exists():
            channel_info = []
            for ch, _address in channels:
                info: dict = {"address": _address, "name": type(ch).__name__}
                if hasattr(ch, "actual_host"):
                    info["host"] = ch.actual_host
                    info["port"] = ch.actual_port
                channel_info.append(info)
            write_state(rd, channels=channel_info)
    except Exception as exc:
        logger.debug("Could not update agent.state with channel info: %s", exc)


def _create_channels(channels_cfg, ep_channel, Channel, registry=None) -> list[tuple[Channel, str]]:
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


async def _run_actor_and_channels(actor, channels, harness) -> None:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(actor.run(), name="actor")
        for ch, address in channels:
            tg.create_task(ch.run(harness.mail_route.bind(address)), name=f"channel:{address}")


async def _build_named_agent(harness, agent_kind: str, actor_name: str, actor_overrides: dict[str, Any]):
    from bos.core import AgentRegistry, _deep_merge

    agent_spec: dict[str, Any] = {"agent_name": actor_name}
    if AgentRegistry.has_registered(agent_kind):
        agent_spec.update(AgentRegistry.get_defaults(agent_kind))
    _deep_merge(agent_spec, actor_overrides)

    return await harness.create_agent(agent_kind, agent_spec)
