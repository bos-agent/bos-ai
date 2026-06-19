"""MemoryConsolidationJob — end-to-end propose -> apply -> advance watermark."""

import pytest
from conftest import InMemChatStore, InMemMemoryExtension

from bos.core.contract import Message
from bos.plugins.memory._watermark import WatermarkStore
from bos.plugins.memory.consolidator import ConsolidationPolicy, DefaultMemoryConsolidator
from bos.plugins.memory.job import MemoryConsolidationJob
from bos.plugins.memory.operation_service import DefaultMemoryOperationService


class _StubBLM:
    def __init__(self, ops_payload):
        self._payload = ops_payload

    async def ask(self, **kwargs):
        import json as _json

        from bos.core.llm import LLMResponse

        return LLMResponse(content=_json.dumps(self._payload))


def _msg(role, content, *, turn_id="t1"):
    return Message(llm_message={"role": role, "content": content}, turn_id=turn_id)


class TestJobRun:
    @pytest.mark.asyncio
    async def test_dry_run_applies_no_writes_and_does_not_advance_watermark(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode", turn_id="t1")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend,
            audit_path=tmp_path / "audit.jsonl",
            maxim_keys={"user"},
        )
        blm = _StubBLM({
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
            policy=ConsolidationPolicy(auto_apply=False),
            chat_store=chat_store,
            backend=backend,
            consolidator=consolidator,
            operation_service=op_svc,
            watermarks=wm,
            maxim_keys={"user"},
        )
        await job.run()
        # dry-run: no entries actually created
        assert await backend.search_memories("dark") == []
        # watermark NOT advanced: a dry-run wrote nothing, so burning the
        # watermark would silently exclude these turns from future real runs.
        assert await wm.get("c1") == 0
        # and dry-run record is in audit
        audit = await op_svc.audit()
        assert audit and audit[0].result == "dry_run"

    @pytest.mark.asyncio
    async def test_dry_run_then_apply_still_persists_memory(self, tmp_path):
        """Regression: a default dry-run must not poison a later real --apply."""
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend,
            audit_path=tmp_path / "audit.jsonl",
            maxim_keys={"user"},
        )
        blm = _StubBLM({
            "operations": [
                {"op": "ADD", "reason": "stable preference", "content": "prefers dark mode", "importance": 7},
            ]
        })
        consolidator = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        head = await chat_store.get_revision("c1")

        def _job(*, auto_apply: bool) -> MemoryConsolidationJob:
            return MemoryConsolidationJob(
                chat_id="c1",
                actor_name="test-agent",
                base_revision=head,
                trigger="manual",
                policy=ConsolidationPolicy(auto_apply=auto_apply),
                chat_store=chat_store,
                backend=backend,
                consolidator=consolidator,
                operation_service=op_svc,
                watermarks=wm,
                maxim_keys={"user"},
            )

        # First: a dry-run over the new turns (CLI default).
        await _job(auto_apply=False).run()
        assert await backend.search_memories("dark") == []
        assert await wm.get("c1") == 0

        # Then: a real --apply over the same turns still persists and advances.
        await _job(auto_apply=True).run()
        assert (await backend.search_memories("dark"))[0].content == "prefers dark mode"
        assert await wm.get("c1") == head

    @pytest.mark.asyncio
    async def test_auto_apply_persists_and_advances(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend,
            audit_path=tmp_path / "audit.jsonl",
            maxim_keys={"user"},
        )
        blm = _StubBLM({
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
            policy=ConsolidationPolicy(auto_apply=True),
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
            policy=ConsolidationPolicy(auto_apply=True),
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
            consolidator=DefaultMemoryConsolidator(_StubBLM({"operations": []}), maxim_keys={"user"}),
            operation_service=op_svc,
            watermarks=WatermarkStore(tmp_path / "wm.json"),
            maxim_keys={"user"},
        )
        assert job.key == "consolidate:test-agent:c1:4:manual"
