"""TelegramChannel — Telegram Bot API bridge backed by a bound mailbox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, FormData

from bos.core import BaseChannel, MailBox, ep_channel
from bos.core.actor import Envelope, MessageType
from bos.core.agent import TurnEvent
from bos.gateway import ChannelConversationRef, ChannelRuntimeContext
from bos.gateway.http import store_uploaded_attachment

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


TELEGRAM_MESSAGE_LIMIT = _env_int("TELEGRAM_MESSAGE_LIMIT", 4096)
# Final replies longer than this (UTF-8 bytes) are sent as a document attachment
# instead of inline text, so long answers don't flood the chat as message chunks.
TELEGRAM_ATTACHMENT_THRESHOLD = _env_int("TELEGRAM_ATTACHMENT_THRESHOLD", 1024)
# Max UTF-8 bytes for an intermediate turn-event status preview.
TELEGRAM_STATUS_PREVIEW_LIMIT = _env_int("TELEGRAM_STATUS_PREVIEW_LIMIT", 64)


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip() + "…"


@dataclass(frozen=True)
class TelegramSettings:
    token: str | None = None
    token_env: str | None = None
    bot_id: str | None = None
    poll_timeout: int = 30
    api_base: str = "https://api.telegram.org"
    allowed_chat_ids: Iterable[int | str] | None = None
    default_chat_id: int | str | None = None
    album_debounce_seconds: float = 1.5


def _conversation_id_for_telegram_chat(telegram_chat_id: int | str) -> str:
    return f"tg_chat:{telegram_chat_id}"


def _normalize_command(text: str, bot_username: str | None = None) -> str:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return stripped
    head, sep, tail = stripped.partition(" ")
    if bot_username and "@" in head:
        cmd, _, mention = head.partition("@")
        if mention.lower() == bot_username.lower():
            head = cmd
    return head + (sep + tail if sep else "")


def _split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
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


def _largest_photo_file_id(photo: Any) -> str | None:
    if not isinstance(photo, list) or not photo:
        return None
    # Telegram returns sizes ascending; pick the last with a file_id.
    for size in reversed(photo):
        if isinstance(size, dict) and isinstance(size.get("file_id"), str):
            return size["file_id"]
    return None


# Non-photo media fields, in priority order. Each carries a single ``file_id``
# and, for most types, a ``mime_type`` (and sometimes ``file_name``). These are
# stored verbatim and handed to the agent as a path reference — never rejected.
_ATTACHMENT_KEYS = ("document", "audio", "voice", "video", "video_note", "animation")


def _extract_attachment(message: dict[str, Any]) -> dict[str, Any] | None:
    """Return a descriptor for a single non-photo attachment, if present."""
    for key in _ATTACHMENT_KEYS:
        media = message.get(key)
        if isinstance(media, dict) and isinstance(media.get("file_id"), str):
            return {
                "file_id": media["file_id"],
                "filename": media.get("file_name"),
                "mime_type": media.get("mime_type"),
            }
    return None


def _inbound_attachment_descriptors(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    """Download descriptors for a single inbound message (photos or one file)."""
    if inbound.get("file_ids"):
        # Photos carry no MIME type from Telegram; they are always JPEG.
        return [{"file_id": fid, "filename": None, "mime_type": "image/jpeg"} for fid in inbound["file_ids"]]
    attachment = inbound.get("attachment")
    return [attachment] if attachment else []


def _extract_inbound_message(update: dict[str, Any], *, bot_username: str | None = None) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    telegram_chat_id = chat.get("id")
    if telegram_chat_id is None:
        return None

    caption = message.get("text") or message.get("caption")
    caption = caption if isinstance(caption, str) else ""
    file_id = _largest_photo_file_id(message.get("photo"))
    attachment = _extract_attachment(message) if file_id is None else None

    if not caption.strip() and file_id is None and attachment is None:
        return None  # nothing we can use

    has_media = file_id is not None or attachment is not None
    normalized = _normalize_command(caption, bot_username) if caption.strip() else ""
    is_command = normalized.startswith("/") and not has_media
    return {
        "telegram_chat_id": str(telegram_chat_id),
        "channel_conversation_id": _conversation_id_for_telegram_chat(telegram_chat_id),
        "text": normalized,
        "content_type": MessageType.COMMAND if is_command else MessageType.MESSAGE,
        "file_ids": [file_id] if file_id else [],
        "attachment": attachment,
        "media_group_id": message.get("media_group_id"),
    }


# Message fields that carry user content this channel can't turn into an
# attachment (no downloadable file we'd reference). Used to tell genuine media
# apart from service updates (e.g. new_chat_members), so we only nudge the
# sender when they actually sent something we can't process. Downloadable media
# (document, audio, voice, video, video_note, animation, photo) is handled and
# is intentionally absent here.
_UNSUPPORTED_CONTENT_KEYS = (
    "sticker",
    "contact",
    "location",
    "poll",
    "dice",
)


def _unsupported_message_chat_id(update: dict[str, Any]) -> str | None:
    """Return the chat id of an inbound user message we can't read, else None.

    Distinguishes real media (voice, photo, …) from non-message/service updates
    so the channel can nudge the sender instead of dropping it in silence.
    """
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    telegram_chat_id = (message.get("chat") or {}).get("id")
    if telegram_chat_id is None:
        return None
    text = message.get("text") or message.get("caption")
    if isinstance(text, str) and text.strip():
        return None  # has usable text — handled normally
    if not any(key in message for key in _UNSUPPORTED_CONTENT_KEYS):
        return None  # service/other update, not user media
    return str(telegram_chat_id)


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


def _turn_event_label(event: TurnEvent) -> str:
    if event.parent_agent_name and event.agent_name and event.agent_name != event.parent_agent_name:
        return f"{event.parent_agent_name} -> {event.agent_name}"
    return event.agent_name or "agent"


def _render_turn_event(event: TurnEvent) -> str | None:
    label = _turn_event_label(event)

    if event.event_type == "llm" and event.detail == "thinking":
        # The per-iteration "thinking" tick carries only the iteration counter.
        # Returning None leaves the status message untouched so the last real
        # reasoning/tool content stays visible instead of flashing "[main] 3/80".
        return None

    if event.event_type == "llm" and event.detail in ("reasoning", "thinking_content"):
        preview = (event.content or "").strip().replace("\n", " ")
        if not preview:
            return None
        return f"[{label}] {_truncate_bytes(preview, TELEGRAM_STATUS_PREVIEW_LIMIT)}"

    if event.event_type == "llm" and event.detail == "tool_calls" and event.tool_calls:
        tool_names = ", ".join(tc["name"] for tc in event.tool_calls)
        return f"[{label}] using: {_truncate_bytes(tool_names, TELEGRAM_STATUS_PREVIEW_LIMIT)}"

    if event.event_type == "tool" and event.detail == "tool_result":
        preview = str(event.content or "").strip().replace("\n", " ")
        preview = _truncate_bytes(preview, TELEGRAM_STATUS_PREVIEW_LIMIT)
        return f"{label} finished {event.tool_name or 'tool'}: {preview}"

    if event.detail == "max_iteration":
        return f"{label} reached the maximum iteration limit."

    if event.detail == "shutdown":
        return f"{label} was interrupted — the agent is shutting down."

    if event.detail == "error":
        return f"{label} error: {event.content or 'unknown error'}"

    return None


@ep_channel(name="TelegramChannel")
class TelegramChannel(BaseChannel[TelegramSettings]):
    """Telegram Bot API channel using long polling."""

    SettingsType = TelegramSettings

    def __init__(
        self,
        *,
        channel_id: str,
        target_actor: str,
        settings: TelegramSettings,
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
        self._token = settings.token or (os.environ.get(settings.token_env or "") if settings.token_env else None)
        self._poll_timeout = int(settings.poll_timeout)
        self._api_base = settings.api_base.rstrip("/")
        self._allowed_chat_ids = {str(v) for v in (settings.allowed_chat_ids or [])}
        self._default_chat_id = str(settings.default_chat_id or "").strip()
        self._bot_id = str(settings.bot_id or "").strip()

        self._album_buffers: dict[str, dict[str, Any]] = {}
        self._album_debounce = settings.album_debounce_seconds

        self._session: ClientSession | None = None
        self._chat_to_telegram_chat: dict[str, str] = {}
        self._conversation_to_telegram_chat: dict[str, str] = {}
        self._chat_to_status_message_id: dict[str, int] = {}
        self._chat_to_status_text: dict[str, str] = {}
        self._offset: int = 0
        self._bot_username: str | None = None

    @property
    def identity_key(self) -> str | None:
        return f"telegram:bot:{self._bot_id}" if self._bot_id else None

    async def run(self, mailbox: MailBox) -> None:
        if not self._token:
            raise ValueError("Telegram bot token is required; set settings.token or settings.token_env.")

        async with ClientSession(base_url=f"{self._api_base}/bot{self._token}/", raise_for_status=True) as session:
            self._session = session
            self._bot_username = await self._get_bot_username()
            logger.info("TelegramChannel polling started for channel_id=%r", self.channel_id)
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._poll_updates(mailbox), name="telegram:poll")
                    tg.create_task(self._forward_replies(mailbox), name="telegram:send")
            except* asyncio.CancelledError:
                logger.info("TelegramChannel stopped")
                raise
            finally:
                for buf in self._album_buffers.values():
                    if buf.get("handle") is not None:
                        buf["handle"].cancel()
                self._album_buffers.clear()
                self._session = None

    async def aclose(self) -> None:
        pass

    async def _api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Telegram session is not initialized.")
        timeout = ClientTimeout(total=self._poll_timeout + 10)
        async with self._session.post(method, json=payload, timeout=timeout) as resp:
            data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {data}")
        return data

    async def _send_document(self, telegram_chat_id: str, content: str) -> None:
        """Upload a long reply as a Markdown document attachment.

        sendDocument needs multipart/form-data, so this bypasses _api_call
        (which posts JSON) and talks to the session directly.
        """
        if self._session is None:
            raise RuntimeError("Telegram session is not initialized.")
        form = FormData()
        form.add_field("chat_id", str(telegram_chat_id))
        form.add_field(
            "document",
            content.encode("utf-8"),
            filename="response.md",
            content_type="text/markdown",
        )
        try:
            timeout = ClientTimeout(total=self._poll_timeout + 10)
            async with self._session.post("sendDocument", data=form, timeout=timeout) as resp:
                data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API sendDocument failed: {data}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram sendDocument failed for chat_id=%s: %s", telegram_chat_id, exc)

    async def _get_bot_username(self) -> str | None:
        try:
            data = await self._api_call("getMe", {})
        except Exception as exc:
            logger.warning("Telegram getMe failed: %s", exc)
            return None
        result = data.get("result") or {}
        username = result.get("username")
        return username if isinstance(username, str) else None

    async def _poll_updates(self, mailbox: MailBox) -> None:
        while True:
            try:
                data = await self._api_call(
                    "getUpdates",
                    {
                        "timeout": self._poll_timeout,
                        "offset": self._offset,
                        "allowed_updates": ["message", "edited_message"],
                    },
                )
                for update in data.get("result", []):
                    if not isinstance(update, dict):
                        continue
                    if (update_id := update.get("update_id")) is not None:
                        self._offset = max(self._offset, int(update_id) + 1)

                    inbound = _extract_inbound_message(update, bot_username=self._bot_username)
                    if inbound is None:
                        unsupported_chat = _unsupported_message_chat_id(update)
                        if unsupported_chat is not None and (
                            not self._allowed_chat_ids or unsupported_chat in self._allowed_chat_ids
                        ):
                            await self._notify_unsupported(unsupported_chat)
                        continue
                    telegram_chat_id = inbound["telegram_chat_id"]
                    if self._allowed_chat_ids and telegram_chat_id not in self._allowed_chat_ids:
                        logger.info("Ignoring Telegram update from unauthorized chat_id=%s", telegram_chat_id)
                        continue

                    ref = ChannelConversationRef(self.channel_id, inbound["channel_conversation_id"])
                    if inbound["content_type"] == MessageType.COMMAND:
                        # Control plane: handle slash-commands off the gateway,
                        # never as envelopes through the agent actor (BEP 13 / OPEN-D).
                        await self._handle_command(inbound["text"], ref, telegram_chat_id)
                        continue
                    if inbound["content_type"] == MessageType.MESSAGE and inbound["media_group_id"]:
                        self._buffer_album_update(mailbox, inbound)
                        continue
                    descriptors = _inbound_attachment_descriptors(inbound)
                    is_single_media = (
                        inbound["content_type"] == MessageType.MESSAGE and descriptors and not inbound["media_group_id"]
                    )
                    if is_single_media:
                        try:
                            parts = await self._download_attachment_parts(descriptors)
                        except Exception as exc:
                            logger.warning("Telegram attachment download failed: %s", exc)
                            await self._api_call(
                                "sendMessage",
                                {
                                    "chat_id": telegram_chat_id,
                                    "text": "Couldn't fetch the file you sent — please try again.",
                                },
                            )
                            continue
                        await self._assemble_and_send(mailbox, inbound, parts)
                        continue
                    chat_id = self._runtime.chat_coordinator.get_cursor(ref)
                    if chat_id is None:
                        chat_id = self._runtime.chat_coordinator.new_chat(ref)
                    observed_revision = self._runtime.chat_coordinator.observed_revision(chat_id=chat_id, ref=ref)
                    if observed_revision is None:
                        observed_revision = 0
                        self._runtime.chat_coordinator.set_cursor(
                            ref,
                            chat_id,
                            observed_revision=observed_revision,
                        )
                    preflight = await self._runtime.chat_coordinator.prepare_send(
                        chat_id=chat_id,
                        ref=ref,
                        base_revision=observed_revision,
                        content_type=inbound["content_type"],
                    )
                    if preflight.stale:
                        # The channel cursor fell behind: the agent committed
                        # messages this channel never delivered. A Telegram client
                        # has no way to "refresh", so do it for them — push the
                        # missed replies, resync the cursor, and retry the send
                        # instead of dead-ending with a retry-after-refresh notice.
                        observed_revision = await self._catch_up(telegram_chat_id, chat_id, ref, preflight)
                        preflight = await self._runtime.chat_coordinator.prepare_send(
                            chat_id=chat_id,
                            ref=ref,
                            base_revision=observed_revision,
                            content_type=inbound["content_type"],
                        )
                    if not preflight.ok:
                        await self._send_preflight_rejection(telegram_chat_id, preflight)
                        continue

                    self._conversation_to_telegram_chat[ref.channel_conversation_id] = telegram_chat_id
                    self._chat_to_telegram_chat[chat_id] = telegram_chat_id
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
                            await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": str(exc)})
                            continue
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(2)

    def _buffer_album_update(self, mailbox: Any, inbound: dict[str, Any]) -> None:
        group = inbound["media_group_id"]
        buf = self._album_buffers.get(group)
        if buf is None:
            buf = {"file_ids": [], "caption": "", "base": inbound, "handle": None}
            self._album_buffers[group] = buf
        buf["file_ids"].extend(inbound["file_ids"])
        if inbound["text"] and not buf["caption"]:
            buf["caption"] = inbound["text"]
        if buf["handle"] is not None:
            buf["handle"].cancel()
        loop = asyncio.get_running_loop()

        def _schedule_flush() -> None:
            fut = asyncio.ensure_future(self._flush_album(mailbox, group))
            fut.add_done_callback(
                lambda f: (
                    f.cancelled()
                    or (f.exception() and logger.warning("Telegram album flush failed: %s", f.exception()))
                )
            )

        buf["handle"] = loop.call_later(self._album_debounce, _schedule_flush)

    async def _flush_album(self, mailbox: Any, media_group_id: str) -> None:
        buf = self._album_buffers.pop(media_group_id, None)
        if not buf:
            return
        inbound = dict(buf["base"])
        inbound["text"] = buf["caption"]
        inbound["file_ids"] = buf["file_ids"]
        try:
            parts = await self._download_attachment_parts(_inbound_attachment_descriptors(inbound))
        except Exception as exc:
            logger.warning("Telegram album download failed: %s", exc)
            await self._api_call(
                "sendMessage",
                {
                    "chat_id": inbound["telegram_chat_id"],
                    "text": "Couldn't fetch the images you sent — please try again.",
                },
            )
            return
        await self._assemble_and_send(mailbox, inbound, parts)

    async def _download_telegram_file(self, file_path: str) -> bytes:
        if self._session is None:
            raise RuntimeError("Telegram session is not initialized.")
        url = f"{self._api_base}/file/bot{self._token}/{file_path}"
        timeout = ClientTimeout(total=60)
        async with self._session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _download_attachment_parts(self, descriptors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for desc in descriptors:
            file_id = desc["file_id"]
            info = await self._api_call("getFile", {"file_id": file_id})
            file_path = (info.get("result") or {}).get("file_path")
            if not file_path:
                raise ValueError(f"Telegram getFile returned no file_path for {file_id}")
            data = await self._download_telegram_file(file_path)
            parts.append(
                store_uploaded_attachment(
                    upload_dir=self._runtime.upload_dir,
                    filename=desc.get("filename") or Path(file_path).name,
                    content_type=desc.get("mime_type"),
                    data=data,
                )
            )
        return parts

    async def _assemble_and_send(
        self, mailbox: Any, inbound: dict[str, Any], image_parts: list[dict[str, Any]]
    ) -> None:
        ref = ChannelConversationRef(self.channel_id, inbound["channel_conversation_id"])
        telegram_chat_id = inbound["telegram_chat_id"]
        chat_id = self._runtime.chat_coordinator.get_cursor(ref)
        if chat_id is None:
            chat_id = self._runtime.chat_coordinator.new_chat(ref)
        observed_revision = self._runtime.chat_coordinator.observed_revision(chat_id=chat_id, ref=ref) or 0
        self._runtime.chat_coordinator.set_cursor(ref, chat_id, observed_revision=observed_revision)
        preflight = await self._runtime.chat_coordinator.prepare_send(
            chat_id=chat_id,
            ref=ref,
            base_revision=observed_revision,
            content_type=inbound["content_type"],
        )
        if preflight.stale:
            observed_revision = await self._catch_up(telegram_chat_id, chat_id, ref, preflight)
            preflight = await self._runtime.chat_coordinator.prepare_send(
                chat_id=chat_id,
                ref=ref,
                base_revision=observed_revision,
                content_type=inbound["content_type"],
            )
        if not preflight.ok:
            await self._send_preflight_rejection(telegram_chat_id, preflight)
            return
        self._conversation_to_telegram_chat[ref.channel_conversation_id] = telegram_chat_id
        self._chat_to_telegram_chat[chat_id] = telegram_chat_id
        metadata = {
            "base_revision": observed_revision,
            "channel": {"channel_id": self.channel_id, "channel_conversation_id": ref.channel_conversation_id},
        }
        text = inbound["text"]
        target_address = f"agent@{self.target_actor}"
        if text:
            try:
                route = self._runtime.actor_resolver.resolve(text, default_actor=self.target_actor, metadata=metadata)
            except Exception as exc:
                await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": str(exc)})
                return
            target_address, text, metadata = route.target_address, route.content, route.metadata
        content: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else []) + image_parts
        await mailbox.send(
            target_address, content, content_type=MessageType.MESSAGE, chat_id=chat_id, metadata=metadata
        )

    async def _catch_up(self, telegram_chat_id: str, chat_id: str, ref: ChannelConversationRef, preflight) -> int:
        """Deliver replies the channel missed and resync its cursor to the current revision.

        Returns the revision the cursor was advanced to, so the caller can retry the
        send with a matching base_revision.
        """
        for message in preflight.missing_messages or []:
            text = _assistant_text(message)
            if text:
                await self._deliver_text(telegram_chat_id, text)
        self._runtime.chat_coordinator.mark_observed(chat_id=chat_id, ref=ref, revision=preflight.current_revision)
        return preflight.current_revision

    async def _notify_unsupported(self, telegram_chat_id: str) -> None:
        try:
            await self._api_call(
                "sendMessage",
                {
                    "chat_id": telegram_chat_id,
                    "text": (
                        "I can't process stickers, polls, contacts, or locations — but I can "
                        "handle text, photos, and file attachments. Please send one of those."
                    ),
                },
            )
        except Exception as exc:
            logger.warning("Telegram unsupported-format notice failed for chat_id=%s: %s", telegram_chat_id, exc)

    async def _send_preflight_rejection(self, telegram_chat_id: str, preflight) -> None:
        if preflight.stale:
            text = (
                f"This chat has new messages. Please retry after refreshing to revision {preflight.current_revision}."
            )
        else:
            text = "A response is already in progress for this chat."
        await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": text})

    async def _handle_command(self, command: str, ref: ChannelConversationRef, telegram_chat_id: str) -> None:
        """Run a slash-command through the gateway control plane and send the
        result back to Telegram. No COMMAND envelope, no actor mailbox."""
        handler = self._runtime.command_handler
        if handler is None:
            await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": "Commands are unavailable."})
            return
        result = await handler.run(ref, command, target_actor=self.target_actor)
        if result.ok and result.name in ("new", "resume") and result.chat_id:
            # Re-map this Telegram chat to the newly selected internal chat
            # (the coordinator cursor was already moved by the handler).
            self._conversation_to_telegram_chat[ref.channel_conversation_id] = telegram_chat_id
            for old_chat, mapped in list(self._chat_to_telegram_chat.items()):
                if mapped == telegram_chat_id and old_chat != result.chat_id:
                    self._chat_to_telegram_chat.pop(old_chat, None)
            self._chat_to_telegram_chat[result.chat_id] = telegram_chat_id
        body: dict[str, Any] = {"name": result.name, "ok": result.ok}
        if result.result is not None:
            body["result"] = result.result
        if result.error is not None:
            body["error"] = result.error
            body.setdefault("result", result.error)
        if result.chat_id is not None:
            body["chat_id"] = result.chat_id
        await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": json.dumps(body, default=str)})

    async def _forward_replies(self, mailbox: MailBox) -> None:
        while True:
            env = await mailbox.receive()
            telegram_chat_id = self._resolve_telegram_chat_id(env)
            if telegram_chat_id is None:
                logger.warning("Dropping Telegram reply without chat mapping (chat_id=%r)", env.chat_id)
                continue

            if env.content_type == MessageType.TURN_EVENT:
                try:
                    payload = json.loads(env.content) if isinstance(env.content, str) else {}
                    content = _render_turn_event(TurnEvent.from_payload(payload)) if payload else None
                except Exception as exc:
                    logger.debug("Telegram turn-event parse error: %s", exc)
                    continue
                if not content:
                    continue
                await self._set_status_message(telegram_chat_id, env.chat_id, content)
                continue
            else:
                content = str(env.content)
                await self._clear_status_message(telegram_chat_id, env.chat_id)
                if env.chat_id:
                    revision = await self._runtime.chat_coordinator.current_revision(env.chat_id)
                    if (ref := self._ref_from_env(env)) is not None:
                        self._runtime.chat_coordinator.mark_observed(chat_id=env.chat_id, ref=ref, revision=revision)

            await self._deliver_text(telegram_chat_id, content)

    async def _deliver_text(self, telegram_chat_id: str, text: str) -> None:
        """Send a final reply, as a document attachment if it exceeds the byte threshold."""
        if len(text.encode("utf-8")) > TELEGRAM_ATTACHMENT_THRESHOLD:
            await self._send_document(telegram_chat_id, text)
            return
        for part in _split_message(text):
            try:
                await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": part})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram sendMessage failed for chat_id=%s: %s", telegram_chat_id, exc)
                break

    def _resolve_telegram_chat_id(self, env: Envelope) -> str | None:
        if (ref := self._ref_from_env(env)) is not None:
            mapped = self._conversation_to_telegram_chat.get(ref.channel_conversation_id)
            if mapped:
                return mapped
        if env.chat_id and env.chat_id in self._chat_to_telegram_chat:
            return self._chat_to_telegram_chat[env.chat_id]
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

    async def _set_status_message(self, telegram_chat_id: str, chat_id: str | None, text: str) -> None:
        if not chat_id:
            return
        if self._chat_to_status_text.get(chat_id) == text:
            return

        message_id = self._chat_to_status_message_id.get(chat_id)
        if message_id is not None:
            # Overwrite the single status message in place. If the edit fails
            # (Telegram rate-limits editMessageText, transient error), keep the
            # existing message and skip this update — do NOT fall through to
            # sendMessage, which would spam a new message per failed edit and
            # leave every intermediate status in the chat.
            try:
                await self._api_call(
                    "editMessageText",
                    {"chat_id": telegram_chat_id, "message_id": message_id, "text": text},
                )
                self._chat_to_status_text[chat_id] = text
            except Exception as exc:
                logger.debug("Telegram editMessageText failed for chat_id=%s: %s", chat_id, exc)
            return

        # No status message yet for this chat — create the one we will edit.
        try:
            data = await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": text})
        except Exception as exc:
            logger.warning("Telegram status sendMessage failed for chat_id=%s: %s", chat_id, exc)
            return

        result = data.get("result") or {}
        message_id = result.get("message_id")
        if isinstance(message_id, int):
            self._chat_to_status_message_id[chat_id] = message_id
            self._chat_to_status_text[chat_id] = text

    async def _clear_status_message(self, telegram_chat_id: str, chat_id: str | None) -> None:
        if not chat_id:
            return
        message_id = self._chat_to_status_message_id.pop(chat_id, None)
        self._chat_to_status_text.pop(chat_id, None)
        if message_id is None:
            return
        try:
            await self._api_call("deleteMessage", {"chat_id": telegram_chat_id, "message_id": message_id})
        except Exception as exc:
            logger.debug("Telegram deleteMessage failed for chat_id=%s: %s", chat_id, exc)
