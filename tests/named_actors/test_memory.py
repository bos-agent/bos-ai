from datetime import datetime

import pytest

from bos.core import MemoryEntry
from bos.named_actors.memory import ScopedMemory


class FakeMemory:
    def __init__(self):
        self.maxims: dict[str, str] = {}
        self.entries: dict[str, MemoryEntry] = {}
        self.forgotten: list[str] = []
        self.optimized = False
        self.search_top_k: list[int] = []

    async def get_maxim(self, key: str) -> str:
        return self.maxims.get(key, "")

    async def set_maxim(self, key: str, content: str) -> None:
        self.maxims[key] = content

    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        self.search_top_k.append(top_k)
        q = query.lower()
        return [
            entry
            for entry in self.entries.values()
            if q in entry.content.lower() or any(q in tag.lower() for tag in entry.tags)
        ][:top_k]

    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str:
        entry_id = f"mem-{len(self.entries) + 1}"
        self.entries[entry_id] = MemoryEntry(
            id=entry_id,
            content=content,
            tags=tags or [],
            created_at=datetime.now().isoformat(),
        )
        return entry_id

    async def get_memory(self, entry_id: str) -> MemoryEntry | None:
        return self.entries.get(entry_id)

    async def forget_memory(self, entry_id: str) -> None:
        self.forgotten.append(entry_id)
        self.entries.pop(entry_id, None)

    async def optimize(self) -> None:
        self.optimized = True


@pytest.mark.asyncio
async def test_user_maxim_is_shared():
    inner = FakeMemory()
    memory = ScopedMemory(inner, "bob")
    await memory.set_maxim("user", "shared user facts")
    assert inner.maxims["user"] == "shared user facts"
    assert await memory.get_maxim("user") == "shared user facts"


@pytest.mark.asyncio
async def test_non_user_maxims_are_scoped_including_custom():
    inner = FakeMemory()
    memory = ScopedMemory(inner, "bob")
    await memory.set_maxim("identity", "Bob identity")
    await memory.set_maxim("custom", "custom scope")
    assert inner.maxims["actors:bob:identity"] == "Bob identity"
    assert inner.maxims["actors:bob:custom"] == "custom scope"


@pytest.mark.asyncio
async def test_ingest_attaches_scope_tag():
    inner = FakeMemory()
    memory = ScopedMemory(inner, "bob")
    entry_id = await memory.ingest_memory("Bob note", tags=["project"])
    assert inner.entries[entry_id].tags == ["project", "scope:bob"]


@pytest.mark.asyncio
async def test_search_overfetches_and_filters_visible_entries():
    inner = FakeMemory()
    inner.entries = {
        "a": MemoryEntry(id="a", content="alpha", tags=["scope:alice"]),
        "b": MemoryEntry(id="b", content="alpha", tags=["scope:bob"]),
        "c": MemoryEntry(id="c", content="alpha", tags=["scope:global"]),
    }
    memory = ScopedMemory(inner, "bob")
    results = await memory.search_memories("alpha", top_k=2)
    assert [entry.id for entry in results] == ["b", "c"]
    assert inner.search_top_k == [20]


@pytest.mark.asyncio
async def test_get_and_forget_enforce_scope_visibility():
    inner = FakeMemory()
    inner.entries = {
        "bob": MemoryEntry(id="bob", content="visible", tags=["scope:bob"]),
        "alice": MemoryEntry(id="alice", content="hidden", tags=["scope:alice"]),
    }
    memory = ScopedMemory(inner, "bob")
    assert (await memory.get_memory("bob")).content == "visible"
    assert await memory.get_memory("alice") is None

    await memory.forget_memory("alice")
    await memory.forget_memory("bob")
    assert inner.forgotten == ["bob"]


@pytest.mark.asyncio
async def test_optimize_delegates():
    inner = FakeMemory()
    memory = ScopedMemory(inner, "bob")
    await memory.optimize()
    assert inner.optimized is True
