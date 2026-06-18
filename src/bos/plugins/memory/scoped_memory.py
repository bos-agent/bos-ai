"""Memory backend types — entries, index, protocol, scoped wrapper (BEP 10 §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

RequestedBy = Literal["user", "consolidator", "admin", "retention"]


@dataclass
class MemoryEntry:
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    # importance:int(1-10), valid:bool, invalidated_at, invalidated_by,
    # last_used, links:list[str], source_turn_ids:list[str], summary
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryIndexEntry:
    id: str
    tags: list[str]
    summary: str


def _meta(entry: MemoryEntry, key: str, default):
    """Read a metadata field tolerating legacy entries (metadata None/missing)."""
    md = entry.metadata or {}
    val = md.get(key, default)
    return default if val is None else val


class MemoryBackend(Protocol):
    # maxims — unchanged
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...

    # capture (raw append) + read
    async def ingest_memory(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: int = 5,
        summary: str | None = None,
        source_turn_ids: list[str] | None = None,
    ) -> str: ...
    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None: ...
    async def search_memories(
        self,
        query: str,
        *,
        top_k: int = 5,
        include_invalid: bool = False,
    ) -> list[MemoryEntry]: ...
    async def list_index(self) -> list[MemoryIndexEntry]: ...

    # curation writes (driven by the L1 operation service)
    async def update_memory(
        self,
        entry_id: str,
        *,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: int | None = None,
        summary: str | None = None,
        links: list[str] | None = None,
        last_used: str | None = None,
    ) -> None: ...
    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None: ...
    async def restore_memory(self, entry_id: str) -> None: ...
    async def purge_invalidated(self, *, older_than_days: int) -> int: ...


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
        return "user" if key == "user" else f"actors:{self._scope}:{key}"

    def _scope_tag(self) -> str:
        return f"scope:{self._scope}"

    def _is_visible(self, entry: MemoryEntry) -> bool:
        tags = set(entry.tags)
        return self._scope_tag() in tags or "scope:global" in tags

    async def get_maxim(self, key: str) -> str:
        return await self._inner.get_maxim(self._maxim_key(key))

    async def set_maxim(self, key: str, content: str) -> None:
        await self._inner.set_maxim(self._maxim_key(key), content)

    async def ingest_memory(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: int = 5,
        summary: str | None = None,
        source_turn_ids: list[str] | None = None,
    ) -> str:
        scoped_tags = [*(tags or []), self._scope_tag()]
        return await self._inner.ingest_memory(
            content,
            tags=scoped_tags,
            importance=importance,
            summary=summary,
            source_turn_ids=source_turn_ids,
        )

    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None:
        entry = await self._inner.get_memory(entry_id, include_invalid=include_invalid)
        return entry if entry is not None and self._is_visible(entry) else None

    async def search_memories(
        self,
        query: str,
        *,
        top_k: int = 5,
        include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        entries = await self._inner.search_memories(
            query,
            top_k=max(top_k * 4, 20),
            include_invalid=include_invalid,
        )
        return [e for e in entries if self._is_visible(e)][:top_k]

    async def list_index(self) -> list[MemoryIndexEntry]:
        # The inner backend orders by importance; we cannot re-check visibility on
        # an index entry (no tags-on-disk guarantee), so filter via the full list.
        full = await self._inner.search_memories("", top_k=10_000, include_invalid=False)
        visible = {e.id for e in full if self._is_visible(e)}
        return [ie for ie in await self._inner.list_index() if ie.id in visible]

    async def update_memory(self, entry_id: str, **kwargs) -> None:
        if await self.get_memory(entry_id, include_invalid=True) is not None:
            await self._inner.update_memory(entry_id, **kwargs)

    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None:
        if await self.get_memory(entry_id, include_invalid=True) is not None:
            await self._inner.invalidate_memory(entry_id, requested_by=requested_by)

    async def restore_memory(self, entry_id: str) -> None:
        if await self.get_memory(entry_id, include_invalid=True) is not None:
            await self._inner.restore_memory(entry_id)

    async def purge_invalidated(self, *, older_than_days: int) -> int:
        return await self._inner.purge_invalidated(older_than_days=older_than_days)
