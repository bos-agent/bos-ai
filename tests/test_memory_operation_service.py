"""Tests for the L1 memory operation service and audit log."""

import pytest
from conftest import InMemMemoryExtension

from bos.plugins.memory._audit_log import JsonlLog


class TestJsonlLog:
    @pytest.mark.asyncio
    async def test_append_and_read_roundtrip(self, tmp_path):
        log = JsonlLog(tmp_path / "audit.jsonl")
        await log.append({"op": "ADD", "result": "applied"})
        await log.append({"op": "NOOP", "result": "noop"})
        rows = await log.read()
        assert [r["result"] for r in rows] == ["applied", "noop"]

    @pytest.mark.asyncio
    async def test_read_missing_file_is_empty(self, tmp_path):
        log = JsonlLog(tmp_path / "nope.jsonl")
        assert await log.read() == []


class TestDataContracts:
    def test_operation_defaults(self):
        from bos.plugins.memory.operation_service import MemoryOperation

        op = MemoryOperation(op="ADD", reason="seen in transcript", content="a fact")
        assert op.requested_by == "consolidator"
        assert op.source_turn_ids == []
        assert op.target_id is None

    def test_audit_record_shape(self):
        from bos.plugins.memory.operation_service import AuditRecord, MemoryOperation

        rec = AuditRecord(
            op=MemoryOperation(op="NOOP", reason="declined"),
            result="noop", entry_id=None, at="2026-06-17T00:00:00",
        )
        assert rec.result == "noop"
        assert rec.error is None


from bos.plugins.memory.operation_service import (  # noqa: E402
    DefaultMemoryOperationService,
    MemoryOperation,
)


def _svc(tmp_path, backend, maxim_keys=("user", "soul", "identity", "rules")):
    return DefaultMemoryOperationService(
        backend, audit_path=tmp_path / "audit.jsonl", maxim_keys=set(maxim_keys),
    )


class TestApply:
    @pytest.mark.asyncio
    async def test_add_creates_entry_and_audits(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([
            MemoryOperation(op="ADD", reason="durable pref", content="likes dark mode", importance=6)
        ])
        assert recs[0].result == "applied"
        eid = recs[0].entry_id
        assert (await b.get_memory(eid)).content == "likes dark mode"
        audit = await svc.audit()
        assert audit[0].op.reason == "durable pref"

    @pytest.mark.asyncio
    async def test_dry_run_mutates_nothing(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply(
            [MemoryOperation(op="ADD", reason="x", content="should not persist")],
            dry_run=True,
        )
        assert recs[0].result == "dry_run"
        assert await b.search_memories("persist") == []
        # dry-run is still audited
        assert (await svc.audit())[0].result == "dry_run"

    @pytest.mark.asyncio
    async def test_invalidate_records_requested_by(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("stale fact")
        svc = _svc(tmp_path, b)
        recs = await svc.apply(
            [MemoryOperation(op="INVALIDATE", reason="user said stop", target_id=eid, requested_by="user")]
        )
        assert recs[0].result == "applied"
        assert await b.get_memory(eid) is None
        assert (await b.get_memory(eid, include_invalid=True)).metadata["invalidated_by"] == "user"

    @pytest.mark.asyncio
    async def test_update_missing_target_is_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(op="UPDATE", reason="x", target_id="ghost", content="y")])
        assert recs[0].result == "rejected"
        assert "ghost" in recs[0].error

    @pytest.mark.asyncio
    async def test_add_without_content_is_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(op="ADD", reason="x")])
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_promote_bad_maxim_key_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a durable pattern")
        svc = _svc(tmp_path, b)
        recs = await svc.apply(
            [MemoryOperation(op="PROMOTE", reason="x", target_id=eid, maxim_key="bogus")]
        )
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_noop_is_recorded(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(op="NOOP", reason="considered, declined")])
        assert recs[0].result == "noop"


class TestServiceHelpers:
    @pytest.mark.asyncio
    async def test_search_candidates(self, tmp_path):
        b = InMemMemoryExtension()
        await b.ingest_memory("alpha plan")
        svc = _svc(tmp_path, b)
        hits = await svc.search_candidates("alpha", top_k=5)
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_touch_last_used(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a fact")
        svc = _svc(tmp_path, b)
        await svc.touch_last_used([eid])
        assert (await b.get_memory(eid)).metadata["last_used"] is not None

    @pytest.mark.asyncio
    async def test_restore(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a fact")
        await b.invalidate_memory(eid, requested_by="consolidator")
        svc = _svc(tmp_path, b)
        await svc.restore(eid)
        assert await b.get_memory(eid) is not None


class TestMaximCompact:
    @pytest.mark.asyncio
    async def test_update_with_maxim_key_rewrites_maxim(self, tmp_path):
        b = InMemMemoryExtension()
        await b.set_maxim("user", "old long content\n[2026-01-01 10:00] note A\n[2026-01-02 11:00] note B")
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="compact maxim notes",
            maxim_key="user", content="compacted prose: A and B",
        )])
        assert recs[0].result == "applied"
        assert await b.get_maxim("user") == "compacted prose: A and B"

    @pytest.mark.asyncio
    async def test_update_maxim_unknown_key_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="x", maxim_key="bogus", content="x",
        )])
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_update_maxim_requires_content(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="x", maxim_key="user",
        )])
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_update_maxim_and_target_id_mutually_exclusive(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a fact")
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="x", maxim_key="user", target_id=eid, content="y",
        )])
        assert recs[0].result == "rejected"
