"""MemoryConsolidationJob — end-to-end propose -> apply -> advance watermark."""

import pytest
from conftest import InMemChatStore, InMemMemoryExtension

from bos.core.contract import Message
from bos.plugins.memory._watermark import WatermarkStore
from bos.plugins.memory.consolidator import ConsolidationPolicy, DefaultMemoryConsolidator
from bos.plugins.memory.job import MemoryConsolidationJob
from bos.plugins.memory.operation_service import DefaultMemoryOperationService


class _StubAgentRunner:
    """Disposable consolidation agent stand-in (BEP 12): returns a pre-canned
    validated payload, or raises to simulate a failed structured proposal."""

    def __init__(self, ops_payload, *, error=None):
        self._payload = ops_payload
        self._error = error

    async def run(self, message, *, kind=None, agent_cfg=None, schema=None, parent=None, model=None):
        from bos.core import AgentResult

        if self._error is not None:
            raise self._error
        return AgentResult(output=self._payload, structured=True)


def _msg(role, content, *, turn_id="t1"):
    return Message(llm_message={"role": role, "content": content}, turn_id=turn_id)


class TestJobRun:
    @pytest.mark.asyncio
    async def test_persists_and_advances(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend,
            audit_path=tmp_path / "audit.jsonl",
            maxim_keys={"user"},
        )
        blm = _StubAgentRunner({
            "operations": [
                {"op": "ADD", "reason": "stable preference", "content": "prefers dark mode", "importance": 7},
            ]
        })
        consolidator = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            chat_id="c1",
            actor_name="test-agent",
            base_revision=head,
            trigger="manual",
            policy=ConsolidationPolicy(),
            chat_store=chat_store,
            backend=backend,
            consolidator=consolidator,
            operation_service=op_svc,
            watermarks=wm,
            maxim_keys={"user"},
        )
        await job.run()
        assert (await backend.search_memories("dark"))[0].content == "prefers dark mode"
        assert await wm.get("c1") == head

    @pytest.mark.asyncio
    async def test_empty_proposal_advances_watermark(self, tmp_path):
        """A valid but empty proposal means 'nothing durable in this window' and
        legitimately advances the watermark (no memory written)."""
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "just chatter")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(backend, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
        consolidator = DefaultMemoryConsolidator(_StubAgentRunner({"operations": []}), maxim_keys={"user"})
        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            chat_id="c1",
            actor_name="test-agent",
            base_revision=head,
            trigger="manual",
            policy=ConsolidationPolicy(),
            chat_store=chat_store,
            backend=backend,
            consolidator=consolidator,
            operation_service=op_svc,
            watermarks=wm,
            maxim_keys={"user"},
        )
        await job.run()
        assert await backend.search_memories("chatter") == []
        assert await wm.get("c1") == head

    @pytest.mark.asyncio
    async def test_unparseable_proposal_does_not_advance_watermark(self, tmp_path):
        """Regression: a failed structured proposal must NOT burn the window. It
        raises (ConsolidationUnavailable), leaving the watermark for a retry."""
        from bos.core.agent import StructuredOutputError
        from bos.plugins.memory.consolidator import ConsolidationUnavailable

        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(backend, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
        consolidator = DefaultMemoryConsolidator(
            _StubAgentRunner({}, error=StructuredOutputError("no valid structured output")), maxim_keys={"user"}
        )
        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            chat_id="c1",
            actor_name="test-agent",
            base_revision=head,
            trigger="manual",
            policy=ConsolidationPolicy(),
            chat_store=chat_store,
            backend=backend,
            consolidator=consolidator,
            operation_service=op_svc,
            watermarks=wm,
            maxim_keys={"user"},
        )
        with pytest.raises(ConsolidationUnavailable):
            await job.run()
        assert await wm.get("c1") == 0  # not advanced — turns retried later
        assert await backend.search_memories("dark") == []

    @pytest.mark.asyncio
    async def test_watermark_does_not_advance_on_failure(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "msg")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend,
            audit_path=tmp_path / "audit.jsonl",
            maxim_keys={"user"},
        )

        class _RaisingConsolidator:
            async def propose(self, request):
                raise RuntimeError("network down")

        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            chat_id="c1",
            actor_name="test-agent",
            base_revision=head,
            trigger="manual",
            policy=ConsolidationPolicy(),
            chat_store=chat_store,
            backend=backend,
            consolidator=_RaisingConsolidator(),
            operation_service=op_svc,
            watermarks=wm,
            maxim_keys={"user"},
        )
        with pytest.raises(RuntimeError, match="network down"):
            await job.run()
        assert await wm.get("c1") == 0  # not advanced

    @pytest.mark.asyncio
    async def test_idempotency_key_includes_actor_chat_revision_trigger(self, tmp_path):
        op_svc = DefaultMemoryOperationService(
            InMemMemoryExtension(),
            audit_path=tmp_path / "audit.jsonl",
            maxim_keys={"user"},
        )
        job = MemoryConsolidationJob(
            chat_id="c1",
            actor_name="test-agent",
            base_revision=4,
            trigger="manual",
            policy=ConsolidationPolicy(),
            chat_store=InMemChatStore(),
            backend=InMemMemoryExtension(),
            consolidator=DefaultMemoryConsolidator(_StubAgentRunner({"operations": []}), maxim_keys={"user"}),
            operation_service=op_svc,
            watermarks=WatermarkStore(tmp_path / "wm.json"),
            maxim_keys={"user"},
        )
        assert job.key == "consolidate:test-agent:c1:4:manual"
