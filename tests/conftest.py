"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bos.core.contract import MemoryEntry, Message
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


class InMemMemoryExtension:
    """In-memory store for maxims and episodic memories (test-only)."""

    def __init__(self, **maxims: str) -> None:
        self._maxims = {k.lower(): v for k, v in maxims.items()}
        self._memories: dict[str, MemoryEntry] = {}
        self._counter = 0

    # ── Maxims ──

    async def get_maxim(self, key: str) -> str:
        return self._maxims.get(key.lower(), "")

    async def set_maxim(self, key: str, content: str) -> None:
        self._maxims[key.lower()] = content

    # ── Memories ──

    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        q = query.lower()
        results = [e for e in self._memories.values() if q in e.content.lower() or any(q in t.lower() for t in e.tags)]
        return sorted(results, key=lambda e: e.created_at, reverse=True)[:top_k]

    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str:
        self._counter += 1
        entry_id = f"mem_{self._counter}"
        self._memories[entry_id] = MemoryEntry(
            id=entry_id,
            content=content,
            tags=tags or [],
            created_at=datetime.now().isoformat(),
        )
        return entry_id

    async def get_memory(self, entry_id: str) -> MemoryEntry | None:
        return self._memories.get(entry_id)

    async def forget_memory(self, entry_id: str) -> None:
        self._memories.pop(entry_id, None)

    # ── Optimization ──

    async def optimize(self) -> None:
        pass

