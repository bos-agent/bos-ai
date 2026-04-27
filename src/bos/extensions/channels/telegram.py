"""TelegramChannel — Telegram Bot API bridge backed by a bound mailbox.

Uses Bot API long polling via ``getUpdates`` and routes each Telegram chat to a
stable BOS ``chat_id`` so replies can be delivered back to the correct
chat when the actor responds on the shared channel address.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

from aiohttp import ClientSession

from bos.core import MailBox, ep_channel
from bos.core.chat_state import ChatState
from bos.protocol import Envelope, MessageType, TurnEvent

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


def _client_id_for_telegram_chat(telegram_chat_id: int | str) -> str:
    return f"telegram:{telegram_chat_id}"


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
    telegram_chat_id = chat.get("id")
    if telegram_chat_id is None:
        return None

    text = message.get("text") or message.get("caption")
    if not isinstance(text, str) or not text.strip():
        return None

    normalized = _normalize_command(text, bot_username)
    client_id = _client_id_for_telegram_chat(telegram_chat_id)
    return {
        "chat_id": telegram_chat_id,
        "client_id": client_id,
        "text": normalized,
        "content_type": MessageType.COMMAND if normalized.startswith("/") else MessageType.MESSAGE,
    }


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


def _turn_event_label(event: TurnEvent) -> str:
    if event.parent_agent_name and event.agent_name and event.agent_name != event.parent_agent_name:
        return f"{event.parent_agent_name} -> {event.agent_name}"
    return event.agent_name or "agent"


def _render_turn_event(event: TurnEvent) -> str | None:
    label = _turn_event_label(event)

    if event.event_type == "llm" and event.detail == "thinking":
        return f"{label} is thinking…"

    if event.event_type == "llm" and event.detail == "tool_calls" and event.tool_calls:
        tool_names = ", ".join(tc["name"] for tc in event.tool_calls)
        return f"{label} is using: {tool_names}"

    if event.event_type == "tool" and event.detail == "tool_result":
        preview = str(event.content or "").strip().replace("\n", " ")
        preview = preview[:120] + ("…" if len(preview) > 120 else "")
        return f"{label} finished {event.tool_name or 'tool'}: {preview}"

    if event.detail == "max_iteration":
        return f"{label} reached the maximum iteration limit."

    if event.detail == "error":
        return f"{label} error: {event.content or 'unknown error'}"

    return None


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
        bos_dir: str | None = None,
        chat_state_path: str | None = None,
    ) -> None:
        self._token = token
        self._poll_timeout = int(poll_timeout)
        self._api_base = api_base.rstrip("/")
        self._allowed_chat_ids = {str(v) for v in (allowed_chat_ids or [])}
        self._default_chat_id = str(default_chat_id or "").strip()
        self.target_address = target_address
        self._chat_state = ChatState(bos_dir=bos_dir, path=chat_state_path)

        self._session: ClientSession | None = None
        self._chat_to_telegram_chat: dict[str, str] = {}
        self._client_to_telegram_chat: dict[str, str] = {}
        self._chat_to_status_message_id: dict[str, int] = {}
        self._chat_to_status_text: dict[str, str] = {}
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

                    client_id = inbound["client_id"]
                    chat_id = self._chat_state.resolve_for_client(client_id)
                    telegram_chat_id = str(inbound["chat_id"])
                    self._client_to_telegram_chat[client_id] = telegram_chat_id
                    self._chat_to_telegram_chat[chat_id] = telegram_chat_id
                    await mailbox.send(
                        target,
                        inbound["text"],
                        content_type=inbound["content_type"],
                        chat_id=chat_id,
                        metadata={"routing": {"client_id": client_id, "chat_id": chat_id}},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(2)

    async def _forward_replies(self, mailbox: MailBox) -> None:
        while True:
            env = await mailbox.receive()
            telegram_chat_id = self._resolve_telegram_chat_id(env)
            if telegram_chat_id is None:
                logger.warning("Dropping Telegram reply without chat mapping (chat_id=%r)", env.chat_id)
                continue
            selected_chat_id = _selected_chat_from_command_result(env)
            if selected_chat_id:
                client_id = self._client_id_from_env(env)
                if client_id:
                    self._chat_state.set_cursor(client_id, selected_chat_id)
                    self._client_to_telegram_chat[client_id] = telegram_chat_id
                if env.chat_id and env.chat_id != selected_chat_id:
                    self._chat_to_telegram_chat.pop(env.chat_id, None)
                self._chat_to_telegram_chat[selected_chat_id] = telegram_chat_id

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
                content = env.content
                await self._clear_status_message(telegram_chat_id, env.chat_id)

            for part in _split_message(content):
                try:
                    await self._api_call("sendMessage", {"chat_id": telegram_chat_id, "text": part})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Telegram sendMessage failed for chat_id=%s: %s", telegram_chat_id, exc)
                    break

    def _resolve_telegram_chat_id(self, env: Envelope) -> str | None:
        client_id = self._client_id_from_env(env)
        if client_id and client_id in self._client_to_telegram_chat:
            return self._client_to_telegram_chat[client_id]
        if env.chat_id and env.chat_id in self._chat_to_telegram_chat:
            return self._chat_to_telegram_chat[env.chat_id]
        if env.chat_id and env.chat_id.startswith("telegram:"):
            return env.chat_id.split(":", 1)[1]
        return self._default_chat_id or None

    @staticmethod
    def _client_id_from_env(env: Envelope) -> str | None:
        routing = env.metadata.get("routing")
        if not isinstance(routing, dict):
            return None
        client_id = routing.get("client_id")
        return client_id if isinstance(client_id, str) and client_id else None

    async def _set_status_message(self, telegram_chat_id: str, chat_id: str | None, text: str) -> None:
        if not chat_id:
            return
        if self._chat_to_status_text.get(chat_id) == text:
            return

        message_id = self._chat_to_status_message_id.get(chat_id)
        if message_id is not None:
            try:
                await self._api_call(
                    "editMessageText",
                    {"chat_id": telegram_chat_id, "message_id": message_id, "text": text},
                )
                self._chat_to_status_text[chat_id] = text
                return
            except Exception as exc:
                logger.debug("Telegram editMessageText failed for chat_id=%s: %s", chat_id, exc)

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
