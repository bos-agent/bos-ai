"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

from typing import Any


from bos.core.contract import Message
from bos.protocol.content import content_preview


class InMemMessageStore:
    """In-process message store kept for fast, dependency-free tests."""

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}

    async def save_messages(self, chat_id: str, messages: list[Message]) -> None:
        self._messages.setdefault(chat_id, []).extend(messages)

    async def get_messages(self, chat_id: str, original: bool = False) -> list[Message]:
        if original:
            return [m for m in self._messages.get(chat_id, []) if not m.is_summary]
        result = []
        for m in reversed(self._messages.get(chat_id, [])):
            if m.is_summary:
                result.append(m)
                break
            result.append(m)
        result.reverse()
        return result

    async def save_summary(self, chat_id: str, summary: str) -> None:
        self._messages.setdefault(chat_id, []).append(
            Message(llm_message={"role": "system", "content": f"Chat summary:\n{summary}"}, is_summary=True)
        )

    async def list_chats(self) -> dict[str, Any]:
        contexts = {}
        for chat_id, messages in self._messages.items():
            if not (m := next((m for m in messages if m.llm_message["role"] == "user"), None)):
                m = messages[0]
            contexts[chat_id] = {
                "description": content_preview(m.llm_message["content"]),
                "created_at": m.created_at,
                "last_activity": messages[-1].created_at,
                "message_count": len(messages),
            }
        return contexts
