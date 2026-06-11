from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType, web

from bos.core import BaseChannel, MailBox
from bos.protocol import WS_TAKEOVER_CLOSE_CODE, WS_TAKEOVER_CLOSE_REASON, Envelope, MessageContent, MessageType

from .actor_resolver import ActorResolutionError
from .channel_context import ChannelRuntimeContext
from .chat_coordinator import ChannelConversationRef


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
        await self._send_session_ack(mailbox, current_revision, observed_revision, missing_messages)
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
        while not self._ws.closed:
            env = await mailbox.receive()
            selected_chat_id = _selected_chat_from_command_result(env)
            out_chat_id = selected_chat_id or env.chat_id or self._chat_id
            current_revision = await self._runtime.chat_coordinator.current_revision(out_chat_id)
            payload = _envelope_to_dict(env)
            metadata = dict(payload.get("metadata") or {})
            metadata["current_revision"] = current_revision
            if selected_chat_id:
                # Full transcript, not just unobserved messages: the client
                # clears its viewport on chat switch and re-renders history.
                metadata["missing_messages"] = await self._runtime.chat_coordinator.hydrate(
                    chat_id=selected_chat_id,
                )
                self._chat_id = selected_chat_id
                out_chat_id = selected_chat_id
            payload["metadata"] = metadata
            payload["chat_id"] = out_chat_id
            await self._ws.send_json(payload)
            if selected_chat_id:
                self._runtime.chat_coordinator.set_cursor(self.ref, out_chat_id, observed_revision=current_revision)
            else:
                self._runtime.chat_coordinator.mark_observed(
                    chat_id=out_chat_id,
                    ref=self.ref,
                    revision=current_revision,
                )

    async def _handle_inbound_payload(self, mailbox: MailBox, data: dict[str, Any]) -> None:
        content_type = data.get("content_type") or MessageType.MESSAGE
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
        content: MessageContent = data.get("content", "")
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


def _selected_chat_from_command_result(env: Envelope) -> str | None:
    if env.content_type != MessageType.COMMAND_RESULT:
        return None
    try:
        payload = json.loads(env.content) if isinstance(env.content, str) else env.content
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    if payload.get("name") not in {"new", "resume"}:
        return None
    chat_id = payload.get("chat_id")
    return chat_id if isinstance(chat_id, str) and chat_id.strip() else None


def _envelope_to_dict(env: Envelope) -> dict[str, Any]:
    payload = dataclasses.asdict(env)
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, datetime):
        payload["timestamp"] = timestamp.isoformat()
    return payload
