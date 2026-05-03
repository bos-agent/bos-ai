"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from bos.core.contract import MemoryEntry, Message
from bos.protocol import Envelope, MessageContent, MessageType
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


class _InMemMailBox:
    def __init__(self, route: "InMemMailRoute", address: str) -> None:
        self._route = route
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    async def receive(self) -> Envelope:
        return await self._route.receive(self._address)

    async def send(
        self,
        recipient: str,
        content: MessageContent,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._route.deliver(
            Envelope(
                sender=self._address,
                recipient=recipient,
                content=content,
                content_type=content_type,
                chat_id=chat_id,
                metadata=metadata or {},
            )
        )

    async def receive_nowait(self) -> Envelope | None:
        return await self._route.receive_nowait(self._address)


class InMemMailRoute:
    """In-memory mail route for fast, dependency-free tests."""

    _queues: dict[str, asyncio.Queue[Envelope]] = {}

    @classmethod
    def _get_queue(cls, address: str) -> asyncio.Queue[Envelope]:
        if address not in cls._queues:
            cls._queues[address] = asyncio.Queue()
        return cls._queues[address]

    def bind(self, address: str) -> _InMemMailBox:
        return _InMemMailBox(self, address)

    async def deliver(self, env: Envelope) -> None:
        await self._get_queue(env.recipient).put(env)

    async def receive(self, address: str) -> Envelope:
        return await self._get_queue(address).get()

    async def receive_nowait(self, address: str) -> Envelope | None:
        try:
            return self._get_queue(address).get_nowait()
        except asyncio.QueueEmpty:
            return None
