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
