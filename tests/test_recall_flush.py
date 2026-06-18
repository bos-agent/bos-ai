"""Recall-log flush: turn_complete payload carries 'recalled' ids; subscriber
calls touch_last_used on the L1 operation service."""

import pytest
from conftest import InMemMemoryExtension

from bos.core.contract import LifecycleEvent
from bos.plugins.memory.operation_service import DefaultMemoryOperationService
from bos.plugins.memory.recall_flush import RecallFlushSubscriber


class TestRecallFlush:
    @pytest.mark.asyncio
    async def test_flush_touches_last_used(self, tmp_path):
        b = InMemMemoryExtension()
        e1 = await b.ingest_memory("a fact")
        e2 = await b.ingest_memory("another fact")
        svc = DefaultMemoryOperationService(b, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
        sub = RecallFlushSubscriber(svc)
        await sub.handle(
            LifecycleEvent(
                kind="turn_complete",
                chat_id="c1",
                actor_name="A",
                base_revision=1,
                payload={"recalled": [e1, e2]},
            )
        )
        assert (await b.get_memory(e1)).metadata["last_used"] is not None
        assert (await b.get_memory(e2)).metadata["last_used"] is not None

    @pytest.mark.asyncio
    async def test_flush_noop_when_empty(self, tmp_path):
        b = InMemMemoryExtension()
        svc = DefaultMemoryOperationService(b, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
        sub = RecallFlushSubscriber(svc)
        # must not raise on missing or empty recalled list
        await sub.handle(
            LifecycleEvent(
                kind="turn_complete",
                chat_id="c1",
                actor_name="A",
                base_revision=1,
                payload={},
            )
        )
        await sub.handle(
            LifecycleEvent(
                kind="turn_complete",
                chat_id="c1",
                actor_name="A",
                base_revision=1,
                payload={"recalled": []},
            )
        )
