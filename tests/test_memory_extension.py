"""Tests for MemoryPlugin tools and system prompt integration."""

import pytest
from conftest import InMemMemoryExtension, create_test_agent

from bos.plugins.memory import MemoryAgentPlugin


def _create_memory_agent(*, memory=None, maxim_keys=None, **kwargs):
    """Create an agent with MemoryPlugin configured with InMemMemoryExtension."""
    if memory is None:
        memory = InMemMemoryExtension()
    if maxim_keys is None:
        maxim_keys = {"user"}
    plugin = MemoryAgentPlugin(memory, maxim_keys)
    return create_test_agent(plugins=[plugin], **kwargs)


class TestInMemBackend:
    """Tests for InMemMemoryExtension backend."""

    @pytest.mark.asyncio
    async def test_inmem_maxims_crud(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "test user content")
        assert await store.get_maxim("user") == "test user content"

    @pytest.mark.asyncio
    async def test_inmem_maxims_case_insensitive(self):
        store = InMemMemoryExtension()
        await store.set_maxim("USER", "content")
        assert await store.get_maxim("user") == "content"

    @pytest.mark.asyncio
    async def test_inmem_memory_ingest_and_search(self):
        store = InMemMemoryExtension()
        eid = await store.ingest_memory("PostgreSQL 16 on AWS RDS", tags=["infra", "db"])
        assert eid.startswith("mem_")

        results = await store.search_memories("postgresql")
        assert len(results) == 1
        assert results[0].content == "PostgreSQL 16 on AWS RDS"
        assert "infra" in results[0].tags

    @pytest.mark.asyncio
    async def test_inmem_metadata_and_invalidate(self):
        store = InMemMemoryExtension()
        eid = await store.ingest_memory("fact", tags=["t"], importance=7, summary="s")
        entry = await store.get_memory(eid)
        assert entry.metadata["importance"] == 7
        assert entry.metadata["valid"] is True
        await store.invalidate_memory(eid, requested_by="user")
        assert await store.get_memory(eid) is None
        assert (await store.get_memory(eid, include_invalid=True)).metadata["valid"] is False
        await store.restore_memory(eid)
        assert await store.get_memory(eid) is not None

    @pytest.mark.asyncio
    async def test_inmem_list_index_orders_by_importance(self):
        store = InMemMemoryExtension()
        a = await store.ingest_memory("a", importance=2, summary="A")
        b = await store.ingest_memory("b", importance=9, summary="B")
        idx = await store.list_index()
        assert [ie.id for ie in idx] == [b, a]

    @pytest.mark.asyncio
    async def test_inmem_search_returns_top_k(self):
        store = InMemMemoryExtension()
        for i in range(10):
            await store.ingest_memory(f"fact number {i}", tags=["test"])
        results = await store.search_memories("fact", top_k=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_inmem_search_no_results(self):
        store = InMemMemoryExtension()
        results = await store.search_memories("nonexistent")
        assert results == []



class TestRememberTool:
    @pytest.mark.asyncio
    async def test_remember_memory_ingest(self):
        agent = _create_memory_agent()
        result = await agent._invoke_tool("Remember", content="a new fact", tags=["test"])
        assert "entry_id" in result


class TestReviseMaximTool:
    @pytest.mark.asyncio
    async def test_revise_appends_timestamped_entry(self):
        agent = _create_memory_agent(maxim_keys={"user"})
        result = await agent._invoke_tool("ReviseMaxim", key="user", content="likes Python")
        assert "appended" in result
        backend = agent._plugins[0]._backend
        maxim = await backend.get_maxim("user")
        assert "likes Python" in maxim
        assert "]" in maxim

    @pytest.mark.asyncio
    async def test_revise_preserves_existing_content(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "seed content")
        agent = _create_memory_agent(memory=store, maxim_keys={"user"})
        await agent._invoke_tool("ReviseMaxim", key="user", content="new note")
        maxim = await store.get_maxim("user")
        assert "seed content" in maxim
        assert "new note" in maxim

    @pytest.mark.asyncio
    async def test_revise_rejects_unknown_key(self):
        agent = _create_memory_agent(maxim_keys={"user"})
        result = await agent._invoke_tool("ReviseMaxim", key="soul", content="content")
        assert "not allowed" in result

    @pytest.mark.asyncio
    async def test_revise_enforces_length_limit(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "x" * 2000)
        agent = _create_memory_agent(memory=store, maxim_keys={"user"})
        result = await agent._invoke_tool("ReviseMaxim", key="user", content="x" * 100)
        assert "limit" in result.lower()


class TestRecallTool:
    @pytest.mark.asyncio
    async def test_recall_search(self):
        store = InMemMemoryExtension()
        await store.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
        agent = _create_memory_agent(memory=store)
        result = await agent._invoke_tool("Recall", query="postgresql")
        assert "PostgreSQL" in result

    @pytest.mark.asyncio
    async def test_recall_entry_id(self):
        store = InMemMemoryExtension()
        eid = await store.ingest_memory("full fact content here")
        agent = _create_memory_agent(memory=store)
        result = await agent._invoke_tool("Recall", entry_id=eid)
        assert "full fact content here" in result

    @pytest.mark.asyncio
    async def test_recall_no_results(self):
        agent = _create_memory_agent()
        result = await agent._invoke_tool("Recall", query="nonexistent")
        assert "No memories found" in result


class TestForgetRemoved:
    @pytest.mark.asyncio
    async def test_forget_tool_not_registered(self):
        agent = _create_memory_agent()
        with pytest.raises(Exception):
            await agent._invoke_tool("Forget", entry_id="x")


class TestSystemPromptIntegration:
    @pytest.mark.asyncio
    async def test_maxims_injected_into_prompt(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "test user content")
        agent = _create_memory_agent(memory=store, maxim_keys={"user"})
        prompt = await agent._build_system_prompt()
        assert "<active_maxims>" in prompt
        assert '<maxim name="user" scope="your knowledge about the user' in prompt
        assert "test user content" in prompt

    @pytest.mark.asyncio
    async def test_only_maxim_keys_in_prompt(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "user content")
        await store.set_maxim("soul", "soul content")
        agent = _create_memory_agent(memory=store, maxim_keys={"user"})
        prompt = await agent._build_system_prompt()
        assert "user content" in prompt
        assert "soul content" not in prompt

    @pytest.mark.asyncio
    async def test_maxim_header_has_scope_description(self):
        store = InMemMemoryExtension()
        await store.set_maxim("rules", "rule content")
        await store.set_maxim("soul", "soul content")
        agent = _create_memory_agent(memory=store, maxim_keys={"rules", "soul"})
        prompt = await agent._build_system_prompt()
        assert '<maxim name="rules" scope="hard constraints' in prompt
        assert '<maxim name="soul" scope="your character' in prompt

    @pytest.mark.asyncio
    async def test_empty_maxims_are_not_injected_into_prompt(self):
        agent = _create_memory_agent(maxim_keys={"user"})
        prompt = await agent._build_system_prompt()
        assert "<memory_workflow>" in prompt
        assert "<active_maxims>" in prompt
        assert '<maxim name="user" scope="your knowledge about the user' in prompt
        assert "(empty)" not in prompt
