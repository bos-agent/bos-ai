"""In-memory memory backend mirroring the BEP 10 §6 MemoryBackend protocol."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from bos.plugins.memory import MemoryEntry, MemoryIndexEntry, pep_memory_backend
from bos.plugins.memory.scoped_memory import RequestedBy

_DEFAULT_META = {
    "importance": 5,
    "valid": True,
    "invalidated_at": None,
    "invalidated_by": None,
    "last_used": None,
    "links": [],
    "source_turn_ids": [],
    "summary": None,
}


@pep_memory_backend(name="in_memory")
class InMemMemoryExtension:
    def __init__(self, **maxims: str) -> None:
        self._maxims = {k.lower(): v for k, v in maxims.items()}
        self._memories: dict[str, MemoryEntry] = {}
        self._counter = 0

    async def get_maxim(self, key: str) -> str:
        return self._maxims.get(key.lower(), "")

    async def set_maxim(self, key: str, content: str) -> None:
        self._maxims[key.lower()] = content

    async def append_to_maxim(self, key: str, line: str, *, max_len: int | None = None) -> tuple[bool, int]:
        # Synchronous dict access with no intervening await — atomic on the loop.
        current = self._maxims.get(key.lower(), "")
        revised = f"{current}\n{line}" if current else line
        if max_len is not None and len(revised) > max_len:
            return (False, len(revised))
        self._maxims[key.lower()] = revised
        return (True, len(revised))

    async def ingest_memory(
        self,
        content: str,
        *,
        tags=None,
        importance: int = 5,
        summary=None,
        source_turn_ids=None,
    ) -> str:
        self._counter += 1
        entry_id = f"mem_{self._counter}"
        meta = dict(_DEFAULT_META)
        meta.update(importance=importance, summary=summary, source_turn_ids=list(source_turn_ids or []))
        self._memories[entry_id] = MemoryEntry(
            id=entry_id,
            content=content,
            tags=list(tags or []),
            created_at=datetime.now().isoformat(),
            metadata=meta,
        )
        return entry_id

    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None:
        e = self._memories.get(entry_id)
        if e is None or (not include_invalid and not e.metadata.get("valid", True)):
            return None
        return e

    async def search_memories(self, query: str, *, top_k: int = 5, include_invalid: bool = False):
        tokens = re.findall(r"\w+", query.lower())
        scored = []
        for e in self._memories.values():
            if not include_invalid and not e.metadata.get("valid", True):
                continue
            text = (e.content + " " + " ".join(e.tags)).lower()
            lex = sum(1 for t in tokens if t in text) if tokens else 1
            if lex == 0:
                continue
            scored.append((lex, e.metadata.get("importance", 5), e.created_at, e))
        scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
        return [s[3] for s in scored[:top_k]]

    async def list_index(self):
        entries = [e for e in self._memories.values() if e.metadata.get("valid", True)]
        entries.sort(key=lambda e: (e.metadata.get("importance", 5), e.created_at), reverse=True)
        return [
            MemoryIndexEntry(
                id=e.id,
                tags=e.tags,
                summary=e.metadata.get("summary") or (e.content[:80] + ("…" if len(e.content) > 80 else "")),
            )
            for e in entries
        ]

    async def update_memory(
        self,
        entry_id: str,
        *,
        content=None,
        tags=None,
        importance=None,
        summary=None,
        links=None,
        last_used=None,
    ) -> None:
        e = self._memories.get(entry_id)
        if e is None:
            return
        if content is not None:
            e.content = content
        if tags is not None:
            e.tags = list(tags)
        if importance is not None:
            e.metadata["importance"] = importance
        if summary is not None:
            e.metadata["summary"] = summary
        if links is not None:
            e.metadata["links"] = list(links)
        if last_used is not None:
            e.metadata["last_used"] = last_used

    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None:
        e = self._memories.get(entry_id)
        if e is None:
            return
        e.metadata.update(valid=False, invalidated_at=datetime.now().isoformat(), invalidated_by=requested_by)

    async def restore_memory(self, entry_id: str) -> None:
        e = self._memories.get(entry_id)
        if e is None:
            return
        e.metadata.update(valid=True, invalidated_at=None, invalidated_by=None)

    async def purge_invalidated(self, *, older_than_days: int) -> int:
        cutoff = datetime.now() - timedelta(days=older_than_days)
        to_drop = []
        for eid, e in self._memories.items():
            if e.metadata.get("valid", True):
                continue
            inv_at = e.metadata.get("invalidated_at")
            try:
                when = datetime.fromisoformat(inv_at) if inv_at else cutoff
            except ValueError:
                when = cutoff
            if when <= cutoff:
                to_drop.append(eid)
        for eid in to_drop:
            del self._memories[eid]
        return len(to_drop)
