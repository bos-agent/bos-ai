"""Tests for MemoryExtension protocol, Remember/Recall/Forget tools, and maxim limit."""

import pytest

from bos.core import (
    InMemMemoryExtension,
    MemoryExtension,
    ReactAgent,
)
from bos.core.defaults import FileSystemSkillsLoader, InMemMessageStore, NaiveConsolidator, default_maxims


class TestMemoryExtensionProtocol:
    """Protocol conformance tests for MemoryExtension implementations."""

    def test_inmem_implements_protocol(self):
        store = InMemMemoryExtension()
        assert isinstance(store, MemoryExtension)

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
    async def test_inmem_memory_get_and_forget(self):
        store = InMemMemoryExtension()
        eid = await store.ingest_memory("test fact", tags=["test"])
        entry = await store.get_memory(eid)
        assert entry is not None
        assert entry.content == "test fact"

        await store.forget_memory(eid)
        assert await store.get_memory(eid) is None

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

    @pytest.mark.asyncio
    async def test_optimize_is_noop(self):
        store = InMemMemoryExtension()
        await store.optimize()  # should not raise


class TestRememberTool:
    """Tests for the Remember tool (maxim write and memory ingest)."""

    @pytest.mark.asyncio
    async def test_remember_maxim_write(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        result = await agent._invoke_tool("Remember", content="test content", key="user")
        assert "updated" in result
        maxim_content = await agent._memory.get_maxim("user")
        assert maxim_content == "test content"

    @pytest.mark.asyncio
    async def test_remember_maxim_rejects_unknown_key(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        result = await agent._invoke_tool("Remember", content="content", key="soul")
        assert "not allowed" in result

    @pytest.mark.asyncio
    async def test_remember_maxim_enforces_length_limit(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        big_content = "x" * 3000
        result = await agent._invoke_tool("Remember", content=big_content, key="user")
        assert "exceeds the limit" in result
        assert "2048" in result

    @pytest.mark.asyncio
    async def test_remember_memory_ingest(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        result = await agent._invoke_tool("Remember", content="a new fact", tags=["test"])
        assert "entry_id" in result

    @pytest.mark.asyncio
    async def test_remember_maxim_at_limit_is_accepted(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        content = "x" * 2048
        result = await agent._invoke_tool("Remember", content=content, key="user")
        assert "updated" in result


class TestRecallTool:
    """Tests for the Recall tool (search and entry fetch)."""

    @pytest.mark.asyncio
    async def test_recall_search(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        await agent._memory.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
        result = await agent._invoke_tool("Recall", query="postgresql")
        assert "PostgreSQL" in result

    @pytest.mark.asyncio
    async def test_recall_entry_id(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        eid = await agent._memory.ingest_memory("full fact content here")
        result = await agent._invoke_tool("Recall", entry_id=eid)
        assert "full fact content here" in result

    @pytest.mark.asyncio
    async def test_recall_no_results(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        result = await agent._invoke_tool("Recall", query="nonexistent")
        assert "No memories found" in result


class TestForgetTool:
    """Tests for the Forget tool."""

    @pytest.mark.asyncio
    async def test_forget_by_entry_id(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        eid = await agent._memory.ingest_memory("forgettable fact")
        result = await agent._invoke_tool("Forget", entry_id=eid)
        assert "forgotten" in result
        assert await agent._memory.get_memory(eid) is None

    @pytest.mark.asyncio
    async def test_forget_by_query(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="test",
        )
        await agent._memory.ingest_memory("project alpha details")
        await agent._memory.ingest_memory("project alpha timeline")
        result = await agent._invoke_tool("Forget", query="project alpha")
        assert "Forgot 2" in result


class TestSystemPromptIntegration:
    """Tests for maxim system prompt injection."""

    @pytest.mark.asyncio
    async def test_maxims_injected_into_prompt(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(user="test user content"),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": default_maxims["user"]},
            system_prompt="base prompt",
        )
        prompt = await agent._build_system_prompt()
        assert "MAXIMS" in prompt
        assert "test user content" in prompt
        assert "your knowledge about the user" in prompt  # scope description

    @pytest.mark.asyncio
    async def test_only_dict_keys_appear_in_prompt(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": "user content"},
            system_prompt="base prompt",
        )
        await agent._memory.set_maxim("soul", "soul content")
        prompt = await agent._build_system_prompt()
        assert "user content" in prompt
        assert "soul content" not in prompt

    @pytest.mark.asyncio
    async def test_memory_usage_prompt_injected(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"user": ""},
            system_prompt="base prompt",
        )
        prompt = await agent._build_system_prompt()
        assert "USING YOUR MEMORY" in prompt

    @pytest.mark.asyncio
    async def test_maxim_header_has_scope_description(self):
        agent = ReactAgent(
            message_store=InMemMessageStore(),
            memory=InMemMemoryExtension(rules="rule content", soul="soul content"),
            consolidator=NaiveConsolidator(),
            skills_loader=FileSystemSkillsLoader(),
            maxims={"rules": default_maxims["rules"], "soul": default_maxims["soul"]},
            system_prompt="base prompt",
        )
        prompt = await agent._build_system_prompt()
        assert "hard constraints" in prompt
        assert "operating philosophy" in prompt
