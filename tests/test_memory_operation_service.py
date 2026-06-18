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
