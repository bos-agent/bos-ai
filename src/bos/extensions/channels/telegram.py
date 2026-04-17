"""TelegramChannel — Telegram Bot API bridge backed by a bound mailbox.

Uses Bot API long polling via ``getUpdates`` and routes each Telegram chat to a
stable BOS ``conversation_id`` so replies can be delivered back to the correct
chat when the actor responds on the shared channel address.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from aiohttp import ClientSession

from bos.core import MailBox, ep_channel
from bos.protocol import Envelope, MessageType

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


def _conversation_id_for_chat(chat_id: int | str) -> str:
    return f"telegram:{chat_id}"


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


def _extract_inbound_message(update: dict[str, Any], *, bot_username: str | None = None) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None

    text = message.get("text") or message.get("caption")
    if not isinstance(text, str) or not text.strip():
        return None

    normalized = _normalize_command(text, bot_username)
    return {
        "chat_id": chat_id,
        "text": normalized,
        "conversation_id": _conversation_id_for_chat(chat_id),
        "content_type": MessageType.COMMAND if normalized.startswith("/") else MessageType.MESSAGE,
    }


@ep_channel(name="TelegramChannel")
class TelegramChannel:
    """Telegram Bot API channel using long polling."""

    def __init__(
        self,
        token: str,
        target_address: str | None = None,
        poll_timeout: int = 30,
        api_base: str = "https://api.telegram.org",
        allowed_chat_ids: Iterable[int | str] | None = None,
        default_chat_id: int | str | None = None,
    ) -> None:
        self._token = token
        self._poll_timeout = int(poll_timeout)
        self._api_base = api_base.rstrip("/")
        self._allowed_chat_ids = {str(v) for v in (allowed_chat_ids or [])}
        self._default_chat_id = str(default_chat_id or "").strip()
        self.target_address = target_address

        self._session: ClientSession | None = None
        self._conversation_to_chat: dict[str, str] = {}
        self._offset: int = 0
        self._bot_username: str | None = None

    async def run(self, mailbox: MailBox) -> None:
        if not self._token:
            raise ValueError("Telegram bot token is required.")

        address = mailbox.address
        target = self.target_address or address
        async with ClientSession(base_url=f"{self._api_base}/bot{self._token}/", raise_for_status=True) as session:
            self._session = session
            self._bot_username = await self._get_bot_username()
            logger.info("TelegramChannel polling started for address=%r", address)
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._poll_updates(mailbox, target), name="telegram:poll")
                    tg.create_task(self._forward_replies(mailbox), name="telegram:send")
            except* asyncio.CancelledError:
                logger.info("TelegramChannel stopped")
                raise
            finally:
                self._session = None

    async def aclose(self) -> None:
        pass

    async def _api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Telegram session is not initialized.")
        async with self._session.post(method, json=payload, timeout=self._poll_timeout + 10) as resp:
            data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {data}")
        return data

    async def _get_bot_username(self) -> str | None:
        try:
            data = await self._api_call("getMe", {})
        except Exception as exc:
            logger.warning("Telegram getMe failed: %s", exc)
            return None
        result = data.get("result") or {}
        username = result.get("username")
        return username if isinstance(username, str) else None

    async def _poll_updates(self, mailbox: MailBox, target: str) -> None:
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
                        continue
                    if self._allowed_chat_ids and str(inbound["chat_id"]) not in self._allowed_chat_ids:
                        logger.info("Ignoring Telegram update from unauthorized chat_id=%s", inbound["chat_id"])
                        continue

                    conversation_id = inbound["conversation_id"]
                    chat_id = str(inbound["chat_id"])
                    self._conversation_to_chat[conversation_id] = chat_id
                    await mailbox.send(
                        target,
                        inbound["text"],
                        content_type=inbound["content_type"],
                        conversation_id=conversation_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(2)

    async def _forward_replies(self, mailbox: MailBox) -> None:
        while True:
            env = await mailbox.receive()
            if env.content_type == MessageType.AGENT_STEP:
                continue

            chat_id = self._resolve_chat_id(env)
            if chat_id is None:
                logger.warning("Dropping Telegram reply without chat mapping (conversation_id=%r)", env.conversation_id)
                continue

            for part in _split_message(env.content):
                try:
                    await self._api_call("sendMessage", {"chat_id": chat_id, "text": part})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Telegram sendMessage failed for chat_id=%s: %s", chat_id, exc)
                    break

    def _resolve_chat_id(self, env: Envelope) -> str | None:
        if env.conversation_id and env.conversation_id in self._conversation_to_chat:
            return self._conversation_to_chat[env.conversation_id]
        if env.conversation_id and env.conversation_id.startswith("telegram:"):
            return env.conversation_id.split(":", 1)[1]
        return self._default_chat_id or None
