from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType, web

from bos.core import BaseChannel, MailBox
from bos.protocol import WS_TAKEOVER_CLOSE_CODE, WS_TAKEOVER_CLOSE_REASON, Envelope, MessageContent, MessageType

from ..core.actor_resolver import ActorResolutionError
from ..core.channel_context import ChannelRuntimeContext
from ..core.chat_coordinator import ChannelConversationRef
from ..core.command_handler import CommandResult


class WSChannel(BaseChannel[dict[str, Any]]):
    """Dynamic one-WebSocket channel managed by the gateway."""

    def __init__(
        self,
        *,
        channel_id: str,
        target_actor: str,
        display_name: str | None = None,
        settings: dict[str, Any] | None = None,
        runtime: ChannelRuntimeContext,
        websocket: web.WebSocketResponse,
        chat_id: str,
        channel_conversation_id: str = "default",
    ) -> None:
        super().__init__(
            channel_id=channel_id,
            target_actor=target_actor,
            display_name=display_name,
            settings=settings or {},
            runtime=runtime,
        )
        self._ws = websocket
        self._chat_id = chat_id
        self._conversation_id = channel_conversation_id
        self._closed_by_takeover = False

    @property
    def ref(self) -> ChannelConversationRef:
        return ChannelConversationRef(self.channel_id, self._conversation_id)

    @property
    def chat_id(self) -> str:
        return self._chat_id

    async def close_for_takeover(self) -> None:
        self._closed_by_takeover = True
        await self._ws.close(code=WS_TAKEOVER_CLOSE_CODE, message=WS_TAKEOVER_CLOSE_REASON.encode())

    async def run(self, mailbox: MailBox) -> None:
        observed_revision = self._runtime.chat_coordinator.observed_revision(chat_id=self._chat_id, ref=self.ref)
        if observed_revision is None:
            observed_revision = 0
            self._runtime.chat_coordinator.set_cursor(self.ref, self._chat_id, observed_revision=observed_revision)
        current_revision = await self._runtime.chat_coordinator.current_revision(self._chat_id)
        # Full transcript, not just unobserved messages: clients replace their
        # viewport with the session ack payload on connect and reconnect.
        missing_messages = await self._runtime.chat_coordinator.hydrate(chat_id=self._chat_id)
        active_turn = self._runtime.chat_coordinator.active_turn_status(self._chat_id)
        await self._send_session_ack(mailbox, current_revision, observed_revision, missing_messages, active_turn)
        self._runtime.chat_coordinator.mark_observed(
            chat_id=self._chat_id,
            ref=self.ref,
            revision=current_revision,
        )

        inbound = asyncio.create_task(self._recv_ws_loop(mailbox), name=f"bos-ws-in:{self.channel_id}")
        outbound = asyncio.create_task(self._send_mail_loop(mailbox), name=f"bos-ws-out:{self.channel_id}")
        try:
            done, pending = await asyncio.wait({inbound, outbound}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            if not self._closed_by_takeover:
                await self._ws.close()
            inbound.cancel()
            outbound.cancel()
            await asyncio.gather(inbound, outbound, return_exceptions=True)
            raise

    async def _send_session_ack(
        self,
        mailbox: MailBox,
        current_revision: int,
        observed_revision: int,
        missing_messages: list[dict[str, Any]],
        active_turn: dict[str, Any] | None = None,
    ) -> None:
        await self._ws.send_json(
            _envelope_to_dict(
                Envelope(
                    sender=mailbox.address,
                    recipient=self.channel_id,
                    content="connected",
                    content_type=MessageType.SYSTEM,
                    chat_id=self._chat_id,
                    metadata={
                        "event": "session",
                        "channel_id": self.channel_id,
                        "channel_conversation_id": self._conversation_id,
                        "chat_id": self._chat_id,
                        "current_revision": current_revision,
                        "observed_revision": observed_revision,
                        "missing_messages": missing_messages,
                        "active_turn": active_turn,
                    },
                )
            )
        )

    async def _recv_ws_loop(self, mailbox: MailBox) -> None:
        async for msg in self._ws:
            if msg.type == WSMsgType.TEXT:
                data = msg.json()
                await self._handle_inbound_payload(mailbox, data)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                break

    async def _send_mail_loop(self, mailbox: MailBox) -> None:
        # Carries actor output (replies, turn events, system events) to the
        # client. Chat switching is no longer routed here — that is the control
        # plane's job, handled synchronously in _deliver_command_result.
        while not self._ws.closed:
            env = await mailbox.receive()
            out_chat_id = env.chat_id or self._chat_id
            current_revision = await self._runtime.chat_coordinator.current_revision(out_chat_id)
            payload = _envelope_to_dict(env)
            metadata = dict(payload.get("metadata") or {})
            metadata["current_revision"] = current_revision
            payload["metadata"] = metadata
            payload["chat_id"] = out_chat_id
            await self._ws.send_json(payload)
            self._runtime.chat_coordinator.mark_observed(
                chat_id=out_chat_id,
                ref=self.ref,
                revision=current_revision,
            )

    async def _handle_inbound_payload(self, mailbox: MailBox, data: dict[str, Any]) -> None:
        content_type = data.get("content_type") or MessageType.MESSAGE
        content: MessageContent = data.get("content", "")
        if str(content_type) == str(MessageType.COMMAND):
            await self._handle_command(content)
            return
        chat_id = data.get("chat_id") or self._chat_id
        metadata = dict(data.get("metadata") or {})
        base_revision = _base_revision(data, metadata)
        if base_revision is None:
            await self._send_system_event(
                {
                    "event": "missing_base_revision",
                    "chat_id": chat_id,
                    "current_revision": await self._runtime.chat_coordinator.current_revision(chat_id),
                },
                chat_id=chat_id,
            )
            return

        preflight = await self._runtime.chat_coordinator.prepare_send(
            chat_id=chat_id,
            ref=self.ref,
            base_revision=base_revision,
            content_type=content_type,
        )
        if not preflight.ok:
            await self._send_preflight_rejection(preflight)
            return

        metadata["base_revision"] = base_revision
        metadata["channel"] = {
            "channel_id": self.channel_id,
            "channel_conversation_id": self._conversation_id,
        }

        target_address = f"agent@{self.target_actor}"
        if str(content_type) == str(MessageType.MESSAGE):
            try:
                route = self._runtime.actor_resolver.resolve(
                    content,
                    default_actor=self.target_actor,
                    metadata=metadata,
                )
            except ActorResolutionError as exc:
                await self._send_system_event(exc.to_event(), chat_id=chat_id)
                return
            target_address = route.target_address
            content = route.content
            metadata = route.metadata

        await mailbox.send(
            target_address,
            content,
            content_type=content_type,
            chat_id=chat_id,
            metadata=metadata,
        )

    async def _handle_command(self, content: MessageContent) -> None:
        """Run a client slash-command through the gateway control plane and send
        the result back natively — no COMMAND envelope, no actor mailbox."""
        handler = self._runtime.command_handler
        if handler is None:
            await self._send_system_event(
                {"event": "command_unavailable", "error": "No command handler configured."},
                chat_id=self._chat_id,
            )
            return
        command = content if isinstance(content, str) else ""
        result = await handler.run(self.ref, command, target_actor=self.target_actor)
        await self._deliver_command_result(result)

    async def _deliver_command_result(self, result: CommandResult) -> None:
        body: dict[str, Any] = {"name": result.name, "ok": result.ok}
        if result.result is not None:
            body["result"] = result.result
        if result.error is not None:
            body["error"] = result.error
            body.setdefault("result", result.error)
        if result.chat_id is not None:
            body["chat_id"] = result.chat_id

        # A /new or /resume switches the channel's chat: re-render from a full
        # transcript and re-cursor (what _send_mail_loop used to do on a
        # COMMAND_RESULT envelope, now done synchronously here).
        switched = result.chat_id if (result.ok and result.name in ("new", "resume") and result.chat_id) else None
        out_chat_id = switched or self._chat_id
        current_revision = await self._runtime.chat_coordinator.current_revision(out_chat_id)
        metadata: dict[str, Any] = {
            "event": "command_result",
            "channel_id": self.channel_id,
            "current_revision": current_revision,
        }
        if switched:
            metadata["missing_messages"] = await self._runtime.chat_coordinator.hydrate(chat_id=switched)
            self._chat_id = switched
            out_chat_id = switched
        await self._ws.send_json(
            _envelope_to_dict(
                Envelope(
                    sender=f"channel@{self.channel_id}",
                    recipient=self.channel_id,
                    content=json.dumps(body, default=str),
                    content_type=MessageType.COMMAND_RESULT,
                    chat_id=out_chat_id,
                    metadata=metadata,
                )
            )
        )
        if switched:
            self._runtime.chat_coordinator.set_cursor(self.ref, out_chat_id, observed_revision=current_revision)

    async def _send_preflight_rejection(self, preflight) -> None:
        if preflight.stale:
            content = {
                "event": "stale_chat",
                "error": preflight.error,
                "chat_id": preflight.chat_id,
                "base_revision": preflight.base_revision,
                "observed_revision": preflight.observed_revision,
                "current_revision": preflight.current_revision,
                "missing_messages": preflight.missing_messages or [],
            }
        else:
            active = self._runtime.chat_coordinator.active_turn_status(preflight.chat_id)
            content = {"event": "active_turn", "chat_id": preflight.chat_id, "active_turn": active}
        await self._send_system_event(content, chat_id=preflight.chat_id)

    async def _send_system_event(self, content: dict[str, Any], *, chat_id: str) -> None:
        await self._ws.send_json(
            _envelope_to_dict(
                Envelope(
                    sender=f"channel@{self.channel_id}",
                    recipient=self.channel_id,
                    content=json.dumps(content, default=str),
                    content_type=MessageType.SYSTEM,
                    chat_id=chat_id,
                    metadata={"event": content.get("event"), "channel_id": self.channel_id, "payload": content},
                )
            )
        )


def _base_revision(data: dict[str, Any], metadata: dict[str, Any]) -> int | None:
    raw = metadata.get("base_revision", data.get("base_revision"))
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _envelope_to_dict(env: Envelope) -> dict[str, Any]:
    payload = dataclasses.asdict(env)
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, datetime):
        payload["timestamp"] = timestamp.isoformat()
    return payload
