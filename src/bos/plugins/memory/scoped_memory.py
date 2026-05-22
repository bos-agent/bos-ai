"""Memory backend types — MemoryEntry, MemoryBackend protocol, ScopedMemory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class MemoryEntry:
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    metadata: dict | None = None


class MemoryBackend(Protocol):
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...
    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]: ...
    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str: ...
    async def get_memory(self, entry_id: str) -> MemoryEntry | None: ...
    async def forget_memory(self, entry_id: str) -> None: ...
    async def optimize(self) -> None: ...


class ScopedMemory:
    """MemoryBackend wrapper that presents an actor-scoped memory view."""

    def __init__(self, inner: MemoryBackend, scope: str) -> None:
        self._inner = inner
        self._scope = scope

    @property
    def scope(self) -> str:
        return self._scope

    def _maxim_key(self, key: str) -> str:
        key = key.lower()
        if key == "user":
            return "user"
        return f"actors:{self._scope}:{key}"

    def _scope_tag(self) -> str:
        return f"scope:{self._scope}"

    async def get_maxim(self, key: str) -> str:
        return await self._inner.get_maxim(self._maxim_key(key))

    async def set_maxim(self, key: str, content: str) -> None:
        await self._inner.set_maxim(self._maxim_key(key), content)

    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        entries = await self._inner.search_memories(query, top_k=max(top_k * 4, 20))
        return [entry for entry in entries if self._is_visible(entry)][:top_k]

    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str:
        scoped_tags = [*(tags or []), self._scope_tag()]
        return await self._inner.ingest_memory(content, tags=scoped_tags)

    async def get_memory(self, entry_id: str) -> MemoryEntry | None:
        entry = await self._inner.get_memory(entry_id)
        return entry if entry is not None and self._is_visible(entry) else None

    async def forget_memory(self, entry_id: str) -> None:
        if await self.get_memory(entry_id) is not None:
            await self._inner.forget_memory(entry_id)

    async def optimize(self) -> None:
        if hasattr(self._inner, "optimize"):
            await self._inner.optimize()

    def _is_visible(self, entry: MemoryEntry) -> bool:
        tags = set(entry.tags)
        return self._scope_tag() in tags or "scope:global" in tags
