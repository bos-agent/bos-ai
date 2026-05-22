"""In-memory memory extension.

Useful for testing and lightweight ephemeral workloads that do not
require persistence.
"""

from __future__ import annotations

from datetime import datetime

from bos.plugins.memory import MemoryEntry, ep_memory_backend


@ep_memory_backend(name="in_memory")
class InMemMemoryExtension:
    """In-memory store for maxims and episodic memories."""

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
