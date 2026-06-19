"""Memory backend types — entries, index, protocol (BEP 10 §6).

Ω: ScopedMemory class removed. Per-agent isolation is now achieved by
giving each agent its own backend instance rooted at its own storage
subtree (see MemoryHarnessPlugin._build_for). The file retains its name
for backward-compatible imports of the types and protocol below."""

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


class MemoryBackend(Protocol):
    # maxims
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...
    async def append_to_maxim(self, key: str, line: str, *, max_len: int | None = None) -> tuple[bool, int]:
        """Atomically append ``line`` as a new line to the maxim's current content.

        The read-append-write is a single atomic operation so concurrent revisers
        (e.g. a consolidation PROMOTE racing the revise_maxim tool) never clobber
        each other's appends. If ``max_len`` is set and the result would exceed it,
        nothing is written. Returns ``(written, resulting_length)``."""
        ...

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
