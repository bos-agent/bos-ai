from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .actor_resolver import ActorDescriptor, ActorResolver
from .channel_context import ChannelRuntimeContext
from .channel_manager import ChannelManager
from .chat_coordinator import ChatCoordinator
from .http import create_gateway_app, require_gateway_api_key
from .state import GatewayRunDir, write_gateway_state

if TYPE_CHECKING:
    from bos.config import Workspace
    from bos.core import AgentHarness


class Gateway:
    def __init__(self, *, workspace: Workspace, harness: AgentHarness) -> None:
        self.workspace = workspace
        self.harness = harness
        self.config = workspace.resolve_gateway_config()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.actual_port = self.config.port
        self.actual_host = self.config.host
        actors = workspace.resolve_gateway_actors()
        self.actor_resolver = ActorResolver(
            {
                name: ActorDescriptor(
                    name=name,
                    address=actor.address,
                    display_name=actor.display_name,
                    agent_kind=actor.agent,
                    is_default=name == workspace.resolve_default_actor(),
                )
                for name, actor in actors.items()
            },
            default_actor=workspace.resolve_default_actor(),
            mention_prefix=workspace.config.runtime.actor_resolver.mention_prefix if workspace.config.runtime else "@",
        )
        if harness.chat_store is None or harness.mail_route is None:
            raise RuntimeError("Gateway requires an active AgentHarness with chat_store and mail_route services.")
        self.chat_coordinator = ChatCoordinator(harness.chat_store)
        self.channel_manager = ChannelManager(
            runtime=ChannelRuntimeContext(
                actor_resolver=self.actor_resolver,
                chat_coordinator=self.chat_coordinator,
                mail_route=harness.mail_route,
                state_changed=self._write_state,
            )
        )
        self.channel_manager.create_persistent(workspace.resolve_gateway_channels())

    def status_snapshot(self) -> dict[str, Any]:
        gateway = {
            "host": self.actual_host,
            "port": self.actual_port,
            "base_url": f"http://{self.actual_host}:{self.actual_port}",
            "auth": {"type": "api_key", "configured": bool(os.environ.get(self.config.api_key_env))},
        }
        actors = {
            name: {
                "agent": actor.agent,
                "display_name": actor.display_name,
                "status": "configured",
                "address": actor.address,
                "active_turns": 0,
                "restart_count": 0,
            }
            for name, actor in self.workspace.resolve_gateway_actors().items()
        }
        channels = self.channel_manager.status_payload()
        return {
            "runtime": self.workspace.get_runtime_config().kind,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "gateway": gateway,
            "actors": actors,
            "channels": channels,
            "active_turns": {},
        }

    def build_app(self) -> web.Application:
        api_key = require_gateway_api_key(self.config)
        return create_gateway_app(config=self.config, api_key=api_key, status_provider=self.status_snapshot)

    async def _write_state(self) -> None:
        write_gateway_state(GatewayRunDir(self.workspace.bos_dir), self.status_snapshot())

    async def run(self) -> None:
        app = self.build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        sockets = getattr(site, "_server", None).sockets if getattr(site, "_server", None) else None
        if sockets:
            self.actual_port = sockets[0].getsockname()[1]
        self.actual_host = self.config.host
        await self.channel_manager.start_all()
        write_gateway_state(GatewayRunDir(self.workspace.bos_dir), self.status_snapshot())
        try:
            import asyncio

            await asyncio.Event().wait()
        finally:
            await self.channel_manager.stop_all()
            write_gateway_state(GatewayRunDir(self.workspace.bos_dir), self.status_snapshot() | {"status": "stopped"})
            await runner.cleanup()
