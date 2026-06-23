"""LarkChannel — Lark/Feishu bot bridge over the lark-oapi WebSocket long connection.

Lark bots can receive events two ways: an HTTP event-subscription webhook (needs a
public URL plus challenge/encryption/signature handling) or a WebSocket "long
connection" opened by the official ``lark-oapi`` SDK. This channel uses the long
connection — the direct analog of the Telegram channel's long polling: a self-hosted
agent needs no inbound public endpoint and no webhook crypto.

Integration note: ``lark_oapi.ws.Client.start()`` is blocking and drives its own
module-global asyncio loop (it ends on ``run_until_complete`` of a sleep-forever
coroutine). It therefore cannot share the actor's event loop. We run it on a dedicated
daemon thread with its own loop (rebinding the SDK module global so the client uses it),
and the SDK's synchronous ``im.message.receive_v1`` callback hands inbound events to the
actor loop via ``call_soon_threadsafe``. Outbound sends use the synchronous HTTP client
wrapped in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from bos.core import BaseChannel, MailBox, ep_channel
from bos.gateway import ChannelConversationRef, ChannelRuntimeContext
from bos.protocol import Envelope, MessageType

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read an int tunable from an env var of the same name, falling back to default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using default %d", name, raw, default)
        return default


# Lark text messages accept large payloads, but very long single messages render poorly
# and risk per-message limits, so split like the Telegram channel does.
LARK_MESSAGE_LIMIT = _env_int("LARK_MESSAGE_LIMIT", 4000)
# Bound the set of recently-seen event ids used to drop SDK redeliveries.
LARK_DEDUP_CACHE_SIZE = _env_int("LARK_DEDUP_CACHE_SIZE", 4096)
# Final replies longer than this (UTF-8 bytes) are sent as a file attachment instead
# of inline text, so long answers don't flood the chat as message chunks (mirrors the
# Telegram channel's document threshold).
LARK_ATTACHMENT_THRESHOLD = _env_int("LARK_ATTACHMENT_THRESHOLD", 1024)


@dataclass(frozen=True)
class LarkSettings:
    app_id: str | None = None
    app_id_env: str | None = None
    app_secret: str | None = None
    app_secret_env: str | None = None
    allowed_chat_ids: Iterable[str] | None = None
    default_chat_id: str | None = None
    log_level: str = "INFO"
    # Open-platform domain. The lark-oapi SDK defaults to Feishu (open.feishu.cn);
    # international Lark apps live on open.larksuite.com and otherwise fail to connect
    # with "Incorrect domain name". Accepts "lark"/"larksuite", "feishu", or a full URL.
    domain: str | None = None
    # Emoji reacted onto the user's message when their message is accepted for handling,
    # acknowledging receipt (in place of streaming intermediate turn events). The value is
    # a Lark emoji_type key; empty disables the acknowledgement. Requires the
    # im:message.reactions:write_only scope.
    ack_reaction: str | None = "OnIt"


def _conversation_id_for_lark_chat(lark_chat_id: str) -> str:
    return f"lark_chat:{lark_chat_id}"


def _strip_mentions(text: str, mentions: list[dict[str, Any]] | None) -> str:
    """Drop the ``@_user_N`` placeholder tokens Lark injects for @-mentions.

    In group chats the bot is usually @-mentioned, and the message text carries an
    opaque key (e.g. ``@_user_1``) for each mention rather than the display name. We
    strip them so the agent sees the user's actual words, not the placeholders.
    """
    for mention in mentions or []:
        key = mention.get("key") if isinstance(mention, dict) else None
        if key:
            text = text.replace(key, "")
    return text.strip()


def _split_message(text: str, limit: int = LARK_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return [part for part in parts if part] or [text[:limit]]


def _extract_inbound_message(event: dict[str, Any]) -> dict[str, Any] | None:
    """Build the inbound payload from a flattened ``im.message.receive_v1`` event.

    Returns ``None`` for anything this channel can't turn into agent text (non-text
    message types, empty bodies). ``event`` is the plain-dict shape produced by
    :meth:`LarkChannel._event_to_dict`, so this stays unit-testable without the SDK.
    """
    lark_chat_id = event.get("chat_id")
    if not lark_chat_id:
        return None
    if event.get("message_type") != "text":
        return None

    raw = event.get("content")
    if not isinstance(raw, str):
        return None
    try:
        text = json.loads(raw).get("text", "")
    except (ValueError, AttributeError):
        return None
    if not isinstance(text, str):
        return None

    text = _strip_mentions(text, event.get("mentions"))
    if not text:
        return None

    return {
        "lark_chat_id": str(lark_chat_id),
        "channel_conversation_id": _conversation_id_for_lark_chat(lark_chat_id),
        "text": text,
        "content_type": MessageType.COMMAND if text.startswith("/") else MessageType.MESSAGE,
    }


def _unsupported_message_chat_id(event: dict[str, Any]) -> str | None:
    """Return the chat id of an inbound message this channel can't read, else None.

    Any non-text message type (post, image, audio, file, sticker, …) is something we
    received but can't process, so the sender gets a nudge instead of silence.
    """
    lark_chat_id = event.get("chat_id")
    message_type = event.get("message_type")
    if not lark_chat_id or not message_type or message_type == "text":
        return None
    return str(lark_chat_id)


def _assistant_text(message: dict[str, Any]) -> str | None:
    """Extract user-facing assistant text from a hydrated chat message, if any."""
    if message.get("is_summary"):
        return None
    llm = message.get("llm_message")
    if not isinstance(llm, dict) or llm.get("role") != "assistant":
        return None
    content = llm.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return "".join(parts).strip() or None
    return None


def _import_lark():
    try:
        import lark_oapi as lark
    except ImportError as exc:  # pragma: no cover - exercised only without the optional dep
        raise RuntimeError(
            "LarkChannel requires the 'lark-oapi' package. Install it with: uv pip install 'bos-ai[lark]'"
        ) from exc
    return lark


def _resolve_domain(lark: Any, value: str) -> str:
    """Map a configured domain (alias or URL) to a concrete open-platform base URL.

    Empty value → the SDK's Feishu default. ``lark``/``larksuite`` → larksuite.com,
    ``feishu`` → feishu.cn; anything else is treated as a full URL.
    """
    if not value:
        return lark.FEISHU_DOMAIN
    alias = value.lower()
    if alias in ("lark", "larksuite"):
        return lark.LARK_DOMAIN
    if alias == "feishu":
        return lark.FEISHU_DOMAIN
    return value


@ep_channel(name="LarkChannel")
class LarkChannel(BaseChannel[LarkSettings]):
    """Lark/Feishu bot channel using the lark-oapi WebSocket long connection."""

    SettingsType = LarkSettings

    def __init__(
        self,
        *,
        channel_id: str,
        target_actor: str,
        settings: LarkSettings,
        runtime: ChannelRuntimeContext,
        display_name: str | None = None,
    ) -> None:
        super().__init__(
            channel_id=channel_id,
            target_actor=target_actor,
            display_name=display_name,
            settings=settings,
            runtime=runtime,
        )
        self._app_id = (settings.app_id or _env(settings.app_id_env) or "").strip()
        self._app_secret = (settings.app_secret or _env(settings.app_secret_env) or "").strip()
        self._log_level = (settings.log_level or "INFO").strip().upper()
        self._domain = (settings.domain or "").strip()
        self._ack_reaction = (settings.ack_reaction or "").strip()
        self._allowed_chat_ids = {str(v) for v in (settings.allowed_chat_ids or [])}
        self._default_chat_id = str(settings.default_chat_id or "").strip()

        self._client: Any = None
        self._ws_client: Any = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._conversation_to_lark_chat: dict[str, str] = {}
        self._chat_to_lark_chat: dict[str, str] = {}
        # bos chat_id → (message_id, reaction_id) for the acknowledgement reaction, so it
        # can be removed once the agent's final reply for that chat is delivered.
        self._chat_to_ack_reaction: dict[str, tuple[str, str]] = {}
        self._seen_event_ids: deque[str] = deque(maxlen=LARK_DEDUP_CACHE_SIZE)
        self._seen_event_set: set[str] = set()

    @property
    def identity_key(self) -> str | None:
        return f"lark:app:{self._app_id}" if self._app_id else None

    async def run(self, mailbox: MailBox) -> None:
        if not self._app_id or not self._app_secret:
            raise ValueError(
                "Lark app_id and app_secret are required; set settings.app_id(_env) and settings.app_secret(_env)."
            )

        lark = _import_lark()
        domain = _resolve_domain(lark, self._domain)
        self._client = lark.Client.builder().app_id(self._app_id).app_secret(self._app_secret).domain(domain).build()
        self._main_loop = asyncio.get_running_loop()
        self._ws_thread = threading.Thread(target=self._run_ws, name=f"lark:ws:{self.channel_id}", daemon=True)
        self._ws_thread.start()
        logger.info("LarkChannel WebSocket started for channel_id=%r", self.channel_id)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._consume_inbound(mailbox), name="lark:recv")
                tg.create_task(self._forward_replies(mailbox), name="lark:send")
        except* asyncio.CancelledError:
            logger.info("LarkChannel stopped")
            raise
        finally:
            self._stop_ws()

    async def aclose(self) -> None:
        self._stop_ws()

    # ── WebSocket long connection (runs on its own thread + loop) ──────────────

    def _run_ws(self) -> None:
        """Open and serve the lark-oapi WS long connection on a dedicated loop.

        ``ws.Client`` drives a module-global event loop via blocking ``start()``; we
        give this thread a fresh loop and rebind that global so the client uses it,
        keeping the SDK entirely off the actor's loop.
        """
        lark = _import_lark()
        import lark_oapi.ws.client as ws_client_mod

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client_mod.loop = loop
        self._ws_loop = loop

        level = getattr(lark.LogLevel, self._log_level, lark.LogLevel.INFO)
        handler = (
            lark.EventDispatcherHandler
            .builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_event)
            .build()
        )
        domain = _resolve_domain(lark, self._domain)
        client = lark.ws.Client(self._app_id, self._app_secret, event_handler=handler, log_level=level, domain=domain)
        self._ws_client = client
        try:
            client.start()
        except BaseException as exc:  # noqa: BLE001 - shutdown cancels the SDK loop; never crash the thread
            logger.debug("LarkChannel WebSocket loop ended: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                if not loop.is_closed():
                    loop.close()

    def _stop_ws(self) -> None:
        loop = self._ws_loop
        if loop is not None and not loop.is_closed():
            client = self._ws_client
            try:
                loop.call_soon_threadsafe(lambda: loop.create_task(self._ws_shutdown(client, loop)))
            except RuntimeError:
                pass
        thread = self._ws_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            # Let the WS loop unwind before we return, so the SDK's background
            # coroutines don't get torn down by interpreter shutdown mid-flight.
            thread.join(timeout=5)

    @staticmethod
    async def _ws_shutdown(client: Any, loop: asyncio.AbstractEventLoop) -> None:
        """Gracefully stop the lark-oapi WS loop (runs on the WS thread's loop).

        Reaching into the SDK internals is deliberate: ``ws.Client`` exposes no
        public graceful stop, and bluntly stopping the loop leaves its background
        recv/ping coroutines mid-await on a half-open SSL socket, which spews
        "Event loop is closed" / "Bad file descriptor" tracebacks on exit. We
        disable auto-reconnect, close the socket, then cancel the SDK loops.
        """
        if client is not None:
            with contextlib.suppress(Exception):
                client._auto_reconnect = False
            with contextlib.suppress(Exception):
                await client._disconnect()
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        loop.stop()

    def _on_message_event(self, data: Any) -> None:
        """SDK callback (runs on the WS thread) — marshal and hand off to the actor loop."""
        try:
            inbound = self._event_to_dict(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lark event parse error: %s", exc)
            return
        main_loop = self._main_loop
        if main_loop is None or main_loop.is_closed():
            return
        main_loop.call_soon_threadsafe(self._inbound.put_nowait, inbound)

    @staticmethod
    def _event_to_dict(data: Any) -> dict[str, Any]:
        """Flatten a ``P2ImMessageReceiveV1`` object into the plain dict our parsers use."""
        header = getattr(data, "header", None)
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        mentions = [
            {"key": getattr(m, "key", None), "name": getattr(m, "name", None)}
            for m in (getattr(message, "mentions", None) or [])
        ]
        return {
            "event_id": getattr(header, "event_id", None),
            "sender_type": getattr(sender, "sender_type", None),
            "sender_open_id": getattr(sender_id, "open_id", None),
            "message_id": getattr(message, "message_id", None),
            "chat_id": getattr(message, "chat_id", None),
            "chat_type": getattr(message, "chat_type", None),
            "message_type": getattr(message, "message_type", None),
            "content": getattr(message, "content", None),
            "mentions": mentions,
        }

    # ── Inbound (runs on the actor loop) ───────────────────────────────────────

    async def _consume_inbound(self, mailbox: MailBox) -> None:
        while True:
            event = await self._inbound.get()
            try:
                await self._handle_inbound(mailbox, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lark inbound handling error: %s", exc)

    async def _handle_inbound(self, mailbox: MailBox, event: dict[str, Any]) -> None:
        logger.debug(
            "Lark inbound event: id=%s chat_id=%s chat_type=%s message_type=%s",
            event.get("event_id"),
            event.get("chat_id"),
            event.get("chat_type"),
            event.get("message_type"),
        )
        if self._is_duplicate(event.get("event_id")):
            return

        inbound = _extract_inbound_message(event)
        if inbound is None:
            unsupported_chat = _unsupported_message_chat_id(event)
            if unsupported_chat is not None and (
                not self._allowed_chat_ids or unsupported_chat in self._allowed_chat_ids
            ):
                await self._notify_unsupported(unsupported_chat)
            return

        lark_chat_id = inbound["lark_chat_id"]
        if self._allowed_chat_ids and lark_chat_id not in self._allowed_chat_ids:
            logger.info("Ignoring Lark message from unauthorized chat_id=%s", lark_chat_id)
            return

        ref = ChannelConversationRef(self.channel_id, inbound["channel_conversation_id"])
        if inbound["content_type"] == MessageType.COMMAND:
            # Control plane: handle slash-commands off the gateway, never as
            # envelopes through the agent actor (BEP 13 / OPEN-D).
            await self._handle_command(inbound["text"], ref, lark_chat_id)
            return
        chat_id = self._runtime.chat_coordinator.get_cursor(ref)
        if chat_id is None:
            chat_id = self._runtime.chat_coordinator.new_chat(ref)
        observed_revision = self._runtime.chat_coordinator.observed_revision(chat_id=chat_id, ref=ref)
        if observed_revision is None:
            observed_revision = 0
            self._runtime.chat_coordinator.set_cursor(ref, chat_id, observed_revision=observed_revision)

        preflight = await self._runtime.chat_coordinator.prepare_send(
            chat_id=chat_id,
            ref=ref,
            base_revision=observed_revision,
            content_type=inbound["content_type"],
        )
        if preflight.stale:
            # The channel cursor fell behind (the agent committed messages this channel
            # never delivered). A Lark client can't "refresh", so do it for them: push
            # the missed replies, resync the cursor, and retry the send.
            observed_revision = await self._catch_up(lark_chat_id, chat_id, ref, preflight)
            preflight = await self._runtime.chat_coordinator.prepare_send(
                chat_id=chat_id,
                ref=ref,
                base_revision=observed_revision,
                content_type=inbound["content_type"],
            )
        if not preflight.ok:
            await self._send_preflight_rejection(lark_chat_id, preflight)
            return

        self._conversation_to_lark_chat[ref.channel_conversation_id] = lark_chat_id
        self._chat_to_lark_chat[chat_id] = lark_chat_id
        metadata = {
            "base_revision": observed_revision,
            "channel": {
                "channel_id": self.channel_id,
                "channel_conversation_id": ref.channel_conversation_id,
            },
        }
        content = inbound["text"]
        target_address = f"agent@{self.target_actor}"
        if inbound["content_type"] == MessageType.MESSAGE:
            try:
                route = self._runtime.actor_resolver.resolve(
                    content,
                    default_actor=self.target_actor,
                    metadata=metadata,
                )
            except Exception as exc:
                await self._deliver_text(lark_chat_id, str(exc))
                return
            target_address = route.target_address
            content = route.content
            metadata = route.metadata
        await mailbox.send(
            target_address,
            content,
            content_type=inbound["content_type"],
            chat_id=chat_id,
            metadata=metadata,
        )
        # Acknowledge receipt by reacting to the user's message — they have asked us not
        # to stream intermediate turn events, so this is the only "we're on it" signal.
        # The reaction is removed once the final reply for this chat is delivered.
        await self._react_ack(chat_id, event.get("message_id"))

    def _is_duplicate(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        if event_id in self._seen_event_set:
            return True
        if len(self._seen_event_ids) == self._seen_event_ids.maxlen:
            self._seen_event_set.discard(self._seen_event_ids[0])
        self._seen_event_ids.append(event_id)
        self._seen_event_set.add(event_id)
        return False

    async def _catch_up(self, lark_chat_id: str, chat_id: str, ref: ChannelConversationRef, preflight) -> int:
        """Deliver missed replies and resync the cursor to the current revision.

        Returns the revision the cursor advanced to, so the caller can retry the send
        with a matching base_revision.
        """
        for message in preflight.missing_messages or []:
            text = _assistant_text(message)
            if text:
                await self._deliver_text(lark_chat_id, text)
        self._runtime.chat_coordinator.mark_observed(chat_id=chat_id, ref=ref, revision=preflight.current_revision)
        return preflight.current_revision

    async def _notify_unsupported(self, lark_chat_id: str) -> None:
        await self._deliver_text(
            lark_chat_id,
            "I can only read text messages right now — I can't process images, files, audio, "
            "or other attachments yet. Please send your message as text.",
        )

    async def _send_preflight_rejection(self, lark_chat_id: str, preflight) -> None:
        if preflight.stale:
            text = (
                f"This chat has new messages. Please retry after refreshing to revision {preflight.current_revision}."
            )
        else:
            text = "A response is already in progress for this chat."
        await self._deliver_text(lark_chat_id, text)

    # ── Outbound (runs on the actor loop) ──────────────────────────────────────

    async def _forward_replies(self, mailbox: MailBox) -> None:
        while True:
            env = await mailbox.receive()
            lark_chat_id = self._resolve_lark_chat_id(env)
            if lark_chat_id is None:
                logger.warning("Dropping Lark reply without chat mapping (chat_id=%r)", env.chat_id)
                continue

            # Intermediate turn-event status streaming is not supported on Lark yet
            # (text-message editing is rate-limit-prone); deliver final replies only.
            if env.content_type == MessageType.TURN_EVENT:
                continue

            content = str(env.content)
            if env.chat_id:
                revision = await self._runtime.chat_coordinator.current_revision(env.chat_id)
                if (ref := self._ref_from_env(env)) is not None:
                    self._runtime.chat_coordinator.mark_observed(chat_id=env.chat_id, ref=ref, revision=revision)

            # The reply is the end of handling this turn — drop the receipt reaction.
            await self._clear_ack_reaction(env.chat_id)
            await self._deliver_text(lark_chat_id, content)

    async def _handle_command(self, command: str, ref: ChannelConversationRef, lark_chat_id: str) -> None:
        """Run a slash-command through the gateway control plane and send the
        result back to Lark. No COMMAND envelope, no actor mailbox."""
        handler = self._runtime.command_handler
        if handler is None:
            await self._deliver_text(lark_chat_id, "Commands are unavailable.")
            return
        result = await handler.run(ref, command, target_actor=self.target_actor)
        if result.ok and result.name in ("new", "resume") and result.chat_id:
            # Re-map this Lark chat to the newly selected internal chat
            # (the coordinator cursor was already moved by the handler).
            self._conversation_to_lark_chat[ref.channel_conversation_id] = lark_chat_id
            for old_chat, mapped in list(self._chat_to_lark_chat.items()):
                if mapped == lark_chat_id and old_chat != result.chat_id:
                    self._chat_to_lark_chat.pop(old_chat, None)
            self._chat_to_lark_chat[result.chat_id] = lark_chat_id
        body: dict[str, Any] = {"name": result.name, "ok": result.ok}
        if result.result is not None:
            body["result"] = result.result
        if result.error is not None:
            body["error"] = result.error
            body.setdefault("result", result.error)
        if result.chat_id is not None:
            body["chat_id"] = result.chat_id
        await self._deliver_text(lark_chat_id, json.dumps(body, default=str))

    async def _deliver_text(self, lark_chat_id: str, text: str) -> None:
        # Long replies go out as a file attachment instead of flooding the chat as
        # message chunks; fall back to inline text if the upload/send fails.
        if len(text.encode("utf-8")) > LARK_ATTACHMENT_THRESHOLD and await self._deliver_document(lark_chat_id, text):
            return
        for part in _split_message(text):
            try:
                await asyncio.to_thread(self._send_text_sync, lark_chat_id, part)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lark send failed for chat_id=%s: %s", lark_chat_id, exc)
                break

    async def _deliver_document(self, lark_chat_id: str, text: str) -> bool:
        """Upload a long reply as a file attachment. Returns True on success."""
        try:
            await asyncio.to_thread(self._send_file_sync, lark_chat_id, text, "response.md")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lark file send failed for chat_id=%s: %s", lark_chat_id, exc)
            return False

    async def _react_ack(self, chat_id: str, message_id: str | None) -> None:
        """React to the user's message to acknowledge receipt (best-effort)."""
        if not message_id or not self._ack_reaction:
            return
        try:
            reaction_id = await asyncio.to_thread(self._add_reaction_sync, message_id, self._ack_reaction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lark reaction failed for message_id=%s: %s", message_id, exc)
            return
        if reaction_id:
            self._chat_to_ack_reaction[chat_id] = (message_id, reaction_id)

    async def _clear_ack_reaction(self, chat_id: str | None) -> None:
        """Remove the receipt reaction once the turn is answered (best-effort)."""
        if not chat_id:
            return
        entry = self._chat_to_ack_reaction.pop(chat_id, None)
        if entry is None:
            return
        message_id, reaction_id = entry
        try:
            await asyncio.to_thread(self._delete_reaction_sync, message_id, reaction_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lark reaction delete failed for message_id=%s: %s", message_id, exc)

    def _send_text_sync(self, lark_chat_id: str, text: str) -> None:
        """Send one text message via the synchronous lark-oapi HTTP client."""
        _import_lark()  # surface a friendly error if the optional dep is missing
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest
            .builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody
                .builder()
                .receive_id(lark_chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.warning(
                "Lark message.create failed for chat_id=%s: code=%s msg=%s",
                lark_chat_id,
                getattr(response, "code", "?"),
                getattr(response, "msg", "?"),
            )

    def _send_file_sync(self, lark_chat_id: str, content: str, filename: str) -> None:
        """Upload text as a file and send it as a Lark file message (raises on failure)."""
        _import_lark()
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        file_request = (
            CreateFileRequest
            .builder()
            .request_body(
                CreateFileRequestBody
                .builder()
                .file_type("stream")
                .file_name(filename)
                .file(io.BytesIO(content.encode("utf-8")))
                .build()
            )
            .build()
        )
        file_response = self._client.im.v1.file.create(file_request)
        if not file_response.success() or file_response.data is None:
            raise RuntimeError(
                f"file upload failed: code={getattr(file_response, 'code', '?')} "
                f"msg={getattr(file_response, 'msg', '?')}"
            )

        request = (
            CreateMessageRequest
            .builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody
                .builder()
                .receive_id(lark_chat_id)
                .msg_type("file")
                .content(json.dumps({"file_key": file_response.data.file_key}))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f"file message send failed: code={getattr(response, 'code', '?')} msg={getattr(response, 'msg', '?')}"
            )

    def _add_reaction_sync(self, message_id: str, emoji: str) -> str | None:
        """Add an emoji reaction to a message; return its reaction_id (or None on failure)."""
        _import_lark()
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        request = (
            CreateMessageReactionRequest
            .builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody
                .builder()
                .reaction_type(Emoji.builder().emoji_type(emoji).build())
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message_reaction.create(request)
        if not response.success():
            logger.warning(
                "Lark reaction create failed for message_id=%s emoji=%s: code=%s msg=%s",
                message_id,
                emoji,
                getattr(response, "code", "?"),
                getattr(response, "msg", "?"),
            )
            return None
        return getattr(response.data, "reaction_id", None)

    def _delete_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """Remove a reaction via the synchronous lark-oapi HTTP client."""
        _import_lark()
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        request = DeleteMessageReactionRequest.builder().message_id(message_id).reaction_id(reaction_id).build()
        response = self._client.im.v1.message_reaction.delete(request)
        if not response.success():
            logger.warning(
                "Lark reaction delete failed for message_id=%s reaction_id=%s: code=%s msg=%s",
                message_id,
                reaction_id,
                getattr(response, "code", "?"),
                getattr(response, "msg", "?"),
            )

    def _resolve_lark_chat_id(self, env: Envelope) -> str | None:
        if (ref := self._ref_from_env(env)) is not None:
            mapped = self._conversation_to_lark_chat.get(ref.channel_conversation_id)
            if mapped:
                return mapped
        if env.chat_id and env.chat_id in self._chat_to_lark_chat:
            return self._chat_to_lark_chat[env.chat_id]
        return self._default_chat_id or None

    def _ref_from_env(self, env: Envelope) -> ChannelConversationRef | None:
        channel = env.metadata.get("channel")
        if not isinstance(channel, dict):
            return None
        channel_id = channel.get("channel_id")
        conversation_id = channel.get("channel_conversation_id")
        if channel_id != self.channel_id or not isinstance(conversation_id, str):
            return None
        return ChannelConversationRef(channel_id=self.channel_id, channel_conversation_id=conversation_id)


def _env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None
