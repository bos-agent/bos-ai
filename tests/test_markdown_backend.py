"""Regression + feature tests for MarkdownMemoryBackend."""

import pytest

from bos.plugins.memory.markdown_backend import MarkdownMemoryBackend


def _backend(tmp_path):
    return MarkdownMemoryBackend(store_dir="memory", bos_dir=tmp_path)


class TestMarkdownRegression:
    @pytest.mark.asyncio
    async def test_maxim_roundtrip(self, tmp_path):
        b = _backend(tmp_path)
        await b.set_maxim("user", "likes Python")
        assert await b.get_maxim("user") == "likes Python"

    @pytest.mark.asyncio
    async def test_ingest_get_search(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("PostgreSQL 16 on RDS", tags=["db"])
        entry = await b.get_memory(eid)
        assert entry is not None and entry.content == "PostgreSQL 16 on RDS"
        assert "db" in entry.tags
        hits = await b.search_memories("postgresql")
        assert [h.id for h in hits] == [eid]


class TestMemoryEntryDefaults:
    def test_metadata_defaults_to_dict(self):
        from bos.plugins.memory.scoped_memory import MemoryEntry

        e = MemoryEntry(id="x", content="c")
        assert e.metadata == {}

    def test_index_entry_shape(self):
        from bos.plugins.memory.scoped_memory import MemoryIndexEntry

        ie = MemoryIndexEntry(id="x", tags=["a"], summary="one line")
        assert (ie.id, ie.tags, ie.summary) == ("x", ["a"], "one line")


class TestMarkdownMetadataAndSoftDelete:
    @pytest.mark.asyncio
    async def test_metadata_roundtrips(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory(
            "deploys on Fridays",
            tags=["ops"],
            importance=8,
            summary="Friday deploys",
            source_turn_ids=["t1", "t2"],
        )
        e = await b.get_memory(eid)
        assert e.metadata["importance"] == 8
        assert e.metadata["valid"] is True
        assert e.metadata["summary"] == "Friday deploys"
        assert e.metadata["source_turn_ids"] == ["t1", "t2"]
        assert "ops" in e.tags

    @pytest.mark.asyncio
    async def test_invalidate_hides_by_default_and_restore(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("temporary fact", tags=["x"])
        await b.invalidate_memory(eid, requested_by="user")
        assert await b.get_memory(eid) is None
        assert await b.search_memories("temporary") == []
        got = await b.get_memory(eid, include_invalid=True)
        assert got.metadata["valid"] is False
        assert got.metadata["invalidated_by"] == "user"
        await b.restore_memory(eid)
        assert (await b.get_memory(eid)).metadata["valid"] is True

    @pytest.mark.asyncio
    async def test_search_ranks_importance_over_recency(self, tmp_path):
        b = _backend(tmp_path)
        low = await b.ingest_memory("alpha project notes", importance=2)
        high = await b.ingest_memory("alpha project plan", importance=9)
        hits = await b.search_memories("alpha", top_k=5)
        assert [h.id for h in hits][:2] == [high, low]

    @pytest.mark.asyncio
    async def test_list_index_orders_by_importance_and_excludes_invalid(self, tmp_path):
        b = _backend(tmp_path)
        a = await b.ingest_memory("aaa", importance=3, summary="A")
        c = await b.ingest_memory("ccc", importance=9, summary="C")
        bad = await b.ingest_memory("bbb", importance=10)
        await b.invalidate_memory(bad, requested_by="consolidator")
        idx = await b.list_index()
        ids = [ie.id for ie in idx]
        assert ids == [c, a]
        assert bad not in ids
        assert idx[0].summary == "C"

    @pytest.mark.asyncio
    async def test_update_memory_changes_fields(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("old", importance=5)
        await b.update_memory(eid, content="new", importance=7, links=["other"])
        e = await b.get_memory(eid)
        assert e.content == "new"
        assert e.metadata["importance"] == 7
        assert e.metadata["links"] == ["other"]

    @pytest.mark.asyncio
    async def test_purge_invalidated_respects_age(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("purge me")
        await b.invalidate_memory(eid, requested_by="retention")
        # invalidated just now -> not purged with a 30-day window
        assert await b.purge_invalidated(older_than_days=30) == 0
        assert await b.get_memory(eid, include_invalid=True) is not None
        # older_than_days=-1 forces purge regardless of age
        assert await b.purge_invalidated(older_than_days=-1) == 1
        assert await b.get_memory(eid, include_invalid=True) is None

    @pytest.mark.asyncio
    async def test_legacy_plain_file_defaults_metadata(self, tmp_path):
        b = _backend(tmp_path)
        # write a pre-BEP10 file (tags header + body, no frontmatter)
        (b._memories_dir / "legacy01.md").write_text("tags:db\nlegacy content", encoding="utf-8")
        e = await b.get_memory("legacy01")
        assert e.content == "legacy content"
        assert e.tags == ["db"]
        assert e.metadata["valid"] is True
        assert e.metadata["importance"] == 5


class TestRemovedMethods:
    def test_protocol_has_no_optimize_or_forget(self):
        from bos.plugins.memory.scoped_memory import MemoryBackend

        names = set(dir(MemoryBackend))
        assert "optimize" not in names
        assert "forget_memory" not in names

    def test_backends_have_no_optimize(self):
        from bos.extensions.memory_stores.in_memory import InMemMemoryExtension
        from bos.plugins.memory.markdown_backend import MarkdownMemoryBackend

        assert not hasattr(InMemMemoryExtension, "optimize")
        assert not hasattr(MarkdownMemoryBackend, "optimize")
        assert not hasattr(InMemMemoryExtension, "forget_memory")
        assert not hasattr(MarkdownMemoryBackend, "forget_memory")
