"""run_consolidation — end-to-end propose -> apply -> advance watermark."""

import pytest
from conftest import InMemChatStore, InMemMemoryExtension

from bos.core.contract import Message
from bos.plugins.memory._watermark import WatermarkStore
from bos.plugins.memory.consolidator import DefaultMemoryConsolidator, run_consolidation
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


async def _fixture(tmp_path, text, consolidator):
    """Commit one turn and wire the collaborators run_consolidation needs."""
    chat_store = InMemChatStore()
    backend = InMemMemoryExtension()
    await chat_store.commit_turn("c1", [_msg("user", text)], turn_id="t1")
    wm = WatermarkStore(tmp_path / "wm.json")
    op_svc = DefaultMemoryOperationService(backend, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
    head = await chat_store.get_revision("c1")
    kwargs = dict(
        actor_name="test-agent",
        chat_id="c1",
        base_revision=head,
        chat_store=chat_store,
        backend=backend,
        consolidator=consolidator,
        operation_service=op_svc,
        watermarks=wm,
        maxim_keys={"user"},
    )
    return backend, wm, head, kwargs


class TestRunConsolidation:
    @pytest.mark.asyncio
    async def test_persists_and_advances(self, tmp_path):
        consolidator = DefaultMemoryConsolidator(
            _StubAgentRunner({
                "operations": [
                    {"op": "ADD", "reason": "stable preference", "content": "prefers dark mode", "importance": 7},
                ]
            }),
            maxim_keys={"user"},
        )
        backend, wm, head, kwargs = await _fixture(tmp_path, "I prefer dark mode", consolidator)
        await run_consolidation(**kwargs)
        assert (await backend.search_memories("dark"))[0].content == "prefers dark mode"
        assert await wm.get("c1") == head

    @pytest.mark.asyncio
    async def test_empty_proposal_advances_watermark(self, tmp_path):
        """A valid but empty proposal means 'nothing durable in this window' and
        legitimately advances the watermark (no memory written)."""
        consolidator = DefaultMemoryConsolidator(_StubAgentRunner({"operations": []}), maxim_keys={"user"})
        backend, wm, head, kwargs = await _fixture(tmp_path, "just chatter", consolidator)
        await run_consolidation(**kwargs)
        assert await backend.search_memories("chatter") == []
        assert await wm.get("c1") == head

    @pytest.mark.asyncio
    async def test_unparseable_proposal_does_not_advance_watermark(self, tmp_path):
        """Regression: a failed structured proposal must NOT burn the window. It
        raises (ConsolidationUnavailable), leaving the watermark for a retry."""
        from bos.core.agent import StructuredOutputError
        from bos.plugins.memory.consolidator import ConsolidationUnavailable

        consolidator = DefaultMemoryConsolidator(
            _StubAgentRunner({}, error=StructuredOutputError("no valid structured output")), maxim_keys={"user"}
        )
        backend, wm, _head, kwargs = await _fixture(tmp_path, "I prefer dark mode", consolidator)
        with pytest.raises(ConsolidationUnavailable):
            await run_consolidation(**kwargs)
        assert await wm.get("c1") == 0  # not advanced — turns retried later
        assert await backend.search_memories("dark") == []

    @pytest.mark.asyncio
    async def test_watermark_does_not_advance_on_failure(self, tmp_path):
        class _RaisingConsolidator:
            async def propose(self, request):
                raise RuntimeError("network down")

        _backend, wm, _head, kwargs = await _fixture(tmp_path, "msg", _RaisingConsolidator())
        with pytest.raises(RuntimeError, match="network down"):
            await run_consolidation(**kwargs)
        assert await wm.get("c1") == 0  # not advanced

    @pytest.mark.asyncio
    async def test_window_already_consolidated_is_skipped(self, tmp_path):
        """The watermark is the only guard against re-consolidating a window:
        at or past it, the consolidator is never called."""
        called = False

        class _TrackingConsolidator:
            async def propose(self, request):
                nonlocal called
                called = True
                return []

        _backend, wm, head, kwargs = await _fixture(tmp_path, "msg", _TrackingConsolidator())
        await wm.set("c1", head)
        await run_consolidation(**kwargs)
        assert not called
