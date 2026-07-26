from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .actors.actor_manager import ActorManager
from .channels.channel_manager import ChannelManager
from .channels.ws_channel import WSChannel
from .config import GatewayRuntimeConfig
from .core.actor_resolver import ActorDescriptor, ActorResolver
from .core.channel_context import ChannelRuntimeContext
from .core.chat_coordinator import ChannelConversationRef, ChatCoordinator
from .core.command_handler import CommandHandler
from .http import create_gateway_app, resolve_gateway_api_key
from .state import GatewayRunDir, write_gateway_state

if TYPE_CHECKING:
    from bos.core import AgentHarness

# Settle window after the actor drain, so polling channels can pick up the last
# handoff replies before their consumers stop. Covers a few JSONL poll cycles.
_REPLY_FLUSH_SECONDS = 2.0


class Gateway:
    def __init__(self, *, runtime: GatewayRuntimeConfig, harness: AgentHarness) -> None:
        self.runtime = runtime
        self.bos_dir = runtime.bos_dir
        self.harness = harness
        self.config = runtime.gateway
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.actual_port = self.config.port
        self.actual_host = self.config.host
        self.actor_resolver = ActorResolver(
            {
                name: ActorDescriptor(
                    name=name,
                    address=actor.address,
                    display_name=actor.display_name,
                    agent_kind=actor.agent,
                    is_default=name == runtime.default_actor,
                )
                for name, actor in runtime.actors.items()
            },
            default_actor=runtime.default_actor,
            mention_prefix=runtime.mention_prefix,
            workdir=runtime.workdir,
        )
        self.default_actor = runtime.default_actor
        if harness.chat_store is None or harness.mail_route is None:
            raise RuntimeError("Gateway requires an active AgentHarness with chat_store and mail_route services.")
        self.chat_coordinator = ChatCoordinator(
            harness.chat_store, cursor_path=GatewayRunDir(self.bos_dir).cursors_file
        )
        self.actor_manager = ActorManager(
            actors=runtime.actors,
            harness=harness,
            chat_coordinator=self.chat_coordinator,
            state_changed=self._write_state,
        )
        self.command_handler = CommandHandler(
            self.chat_coordinator,
            harness.chat_store,
            self.actor_manager.retire_session,
        )
        self.channel_manager = ChannelManager(
            runtime=ChannelRuntimeContext(
                actor_resolver=self.actor_resolver,
                chat_coordinator=self.chat_coordinator,
                mail_route=harness.mail_route,
                command_handler=self.command_handler,
                state_changed=self._write_state,
                upload_dir=(Path(self.bos_dir) / self.config.upload_dir)
                if not Path(self.config.upload_dir).is_absolute()
                else Path(self.config.upload_dir),
            )
        )
        # Channel instantiation is async (ep_channel.invoke); deferred to run().
        self._persistent_channel_configs = runtime.channels
        self._shutdown = asyncio.Event()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    def request_shutdown(self) -> None:
        """Ask ``run`` to return and begin the graceful drain (§ ``run``).

        Idempotent, and safe to call from a signal handler via
        ``loop.call_soon_threadsafe``. Cancelling the task running ``run`` still
        works and remains the forceful path — it skips the drain."""
        self._shutdown.set()

    def status_snapshot(self) -> dict[str, Any]:
        gateway = {
            "host": self.actual_host,
            "port": self.actual_port,
            "base_url": f"http://{self.actual_host}:{self.actual_port}",
            "auth": {"type": "api_key", "configured": bool(os.environ.get(self.config.api_key_env))},
        }
        actors = self.actor_manager.status_payload()
        channels = self.channel_manager.status_payload()
        return {
            "runtime": "process",
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "gateway": gateway,
            "actors": actors,
            "channels": channels,
            "active_turns": self.chat_coordinator.active_turns_status(),
        }

    def build_app(self) -> web.Application:
        api_key = resolve_gateway_api_key(self.config)
        return create_gateway_app(
            config=self.config,
            api_key=api_key,
            status_provider=self.status_snapshot,
            ws_handler=self.handle_ws,
        )

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        if self.shutdown_requested:
            # Registering a channel now would race ``ChannelManager.stop_all``,
            # which has already snapshotted the tasks it will cancel — the new
            # channel would be marked stopped with its task still running, and
            # the client would get a consumer that is about to disappear.
            return web.json_response({"ok": False, "error": "shutting_down"}, status=503)
        channel_id = (request.query.get("channel_id") or "").strip()
        if not channel_id:
            return web.json_response({"ok": False, "error": "channel_id is required"}, status=400)
        takeover = request.query.get("takeover") in {"1", "true", "yes"}
        if channel_id in self.channel_manager.channels and not takeover:
            return web.json_response({"ok": False, "error": "duplicate_channel_id"}, status=409)

        existing = self.channel_manager.channels.get(channel_id)
        if existing is not None and not hasattr(existing.channel, "close_for_takeover"):
            return web.json_response({"ok": False, "error": "duplicate_channel_id"}, status=409)
        if existing is not None:
            await existing.channel.close_for_takeover()  # pyright: ignore[reportAttributeAccessIssue]

        conversation_id = (request.query.get("channel_conversation_id") or "default").strip() or "default"
        ref = ChannelConversationRef(channel_id=channel_id, channel_conversation_id=conversation_id)
        chat_id = (request.query.get("chat_id") or "").strip()
        if chat_id:
            observed_revision = self.chat_coordinator.observed_revision(chat_id=chat_id, ref=ref)
            self.chat_coordinator.set_cursor(ref, chat_id, observed_revision=observed_revision or 0)
        else:
            chat_id = self.chat_coordinator.get_cursor(ref) or self.chat_coordinator.new_chat(ref)

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        channel = WSChannel(
            channel_id=channel_id,
            target_actor=self.default_actor,
            display_name=f"WebSocket {channel_id}",
            settings={},
            runtime=self.channel_manager.runtime,
            websocket=ws,
            chat_id=chat_id,
            channel_conversation_id=conversation_id,
        )
        managed = self.channel_manager.register(
            channel,
            type_name="WSChannel",
            kind="dynamic",
            address=f"channel@{channel_id}",
            takeover=takeover,
        )
        await self.channel_manager.start_channel(managed)
        try:
            if managed.task is not None:
                await managed.task
        finally:
            await self.channel_manager.unregister(channel_id, expected=managed, cancel=False)
        return ws

    async def _write_state(self) -> None:
        write_gateway_state(GatewayRunDir(self.bos_dir), self.status_snapshot())

    async def run(self) -> None:
        await self.channel_manager.create_persistent(self._persistent_channel_configs)
        app = self.build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        server = getattr(site, "_server", None)
        sockets = server.sockets if server else None
        if sockets:
            self.actual_port = sockets[0].getsockname()[1]
        self.actual_host = self.config.host
        await self.actor_manager.start_all()
        await self.channel_manager.start_all()
        write_gateway_state(GatewayRunDir(self.bos_dir), self.status_snapshot())
        graceful = True
        try:
            await self._shutdown.wait()
        except asyncio.CancelledError:
            # Being cancelled is the forceful path — an operator escalating, or
            # the process going down now. Skip the drain rather than holding the
            # stop open for turns that were told to hurry.
            graceful = False
            raise
        finally:
            if graceful:
                # The drain is the one cancellable part of shutdown: a second,
                # escalating signal lands here and should cost the *remaining
                # grace*, not the teardown that follows. Channels stay up across
                # it — a turn closing during the drain still needs a live
                # consumer for its reply.
                with contextlib.suppress(asyncio.CancelledError):
                    await self.actor_manager.drain_all(self.config.shutdown_grace_seconds)
                    await self._flush_replies()
            # Mandatory teardown. Re-awaited through a shield so an escalating
            # signal cannot leave the listening socket, the channel sessions and
            # the on-disk state behind; every step in it is bounded, so this
            # cannot hold the stop open. An outer CancelledError that brought us
            # into this block still propagates once the block completes.
            teardown = asyncio.ensure_future(self._teardown(runner))
            while not teardown.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(teardown)

    async def _teardown(self, runner: web.AppRunner) -> None:
        """Stop everything, in the one order that does not drop replies.

        Actors stop *before* channels so a turn closing during the drain still
        has a live consumer for its reply; the old order dropped the consumer
        first and left clients hanging."""
        await self.actor_manager.stop_all()
        await self.channel_manager.stop_all()
        write_gateway_state(GatewayRunDir(self.bos_dir), self.status_snapshot() | {"status": "stopped"})
        await runner.cleanup()

    async def _flush_replies(self) -> None:
        """Give channels a moment to deliver replies produced during the drain.

        Channels poll their mailbox, so a handoff written in the last instant of
        the drain is not necessarily on the wire yet. This is a bounded
        best-effort settle, not a delivery guarantee — a channel that is slower
        than the window (or a client that has already gone) still loses the
        reply, and the handoff remains committed to chat history either way."""
        if self.config.shutdown_grace_seconds <= 0:
            return
        await asyncio.sleep(min(_REPLY_FLUSH_SECONDS, self.config.shutdown_grace_seconds))
