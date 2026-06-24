"""AutoRecallInterceptor emits memory.recalled TurnEvent on the per-turn
event_sink, instead of writing ctx.metadata['recalled']. The event carries
the surfaced entry ids in metadata['ids']."""

import asyncio

import pytest
from conftest import InMemMemoryExtension

from bos.core.agent import TurnContext, TurnEvent
from bos.core.contract import Message
from bos.plugins.memory.auto_recall import AutoRecallInterceptor


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    async def emit(self, event: TurnEvent) -> None:
        self.events.append(event)


def _recall_event_ids(sink: CaptureSink) -> list[str]:
    out: list[str] = []
    for e in sink.events:
        if e.event_type == "memory.recalled":
            out.extend(e.metadata.get("ids", []))
    return out


def _ctx_with_query(query: str, sink: CaptureSink | None = None) -> TurnContext:
    ctx = TurnContext(agent_name="A", chat_id="c1", turn_id="t1", event_sink=sink)
    ctx.current = [Message(llm_message={"role": "user", "content": query})]
    return ctx


@pytest.mark.asyncio
async def test_emits_memory_recalled_with_surfaced_ids():
    backend = InMemMemoryExtension()
    eid = await backend.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
    interceptor = AutoRecallInterceptor(backend, top_k=5)
    sink = CaptureSink()
    ctx = _ctx_with_query("what database do I like? postgresql?", sink=sink)

    await interceptor.intercept("prepare", ctx)

    assert _recall_event_ids(sink) == [eid]


@pytest.mark.asyncio
async def test_no_event_when_no_hits():
    backend = InMemMemoryExtension()
    interceptor = AutoRecallInterceptor(backend, top_k=5)
    sink = CaptureSink()
    ctx = _ctx_with_query("anything", sink=sink)

    await interceptor.intercept("prepare", ctx)

    assert _recall_event_ids(sink) == []


@pytest.mark.asyncio
async def test_no_event_when_sink_is_none():
    """Sink is optional — interceptor must not crash when ctx.event_sink is None."""
    backend = InMemMemoryExtension()
    await backend.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
    interceptor = AutoRecallInterceptor(backend, top_k=5)
    ctx = _ctx_with_query("postgresql?", sink=None)
    # must not raise
    await interceptor.intercept("prepare", ctx)


@pytest.mark.asyncio
async def test_concurrent_intercepts_do_not_cross_contaminate():
    """Two contexts intercepted concurrently — each sink sees only its own
    recalled ids. This is the per-chat isolation guarantee that was broken
    by agent._current_context.
    """
    backend = InMemMemoryExtension()
    eid_a = await backend.ingest_memory("Alice loves PostgreSQL", tags=["db"])
    eid_b = await backend.ingest_memory("Bob writes Rust daily", tags=["lang"])
    interceptor = AutoRecallInterceptor(backend, top_k=1)

    sink_a, sink_b = CaptureSink(), CaptureSink()
    ctx_a = _ctx_with_query("postgresql?", sink=sink_a)
    ctx_b = _ctx_with_query("rust?", sink=sink_b)

    await asyncio.gather(
        interceptor.intercept("prepare", ctx_a),
        interceptor.intercept("prepare", ctx_b),
    )

    assert _recall_event_ids(sink_a) == [eid_a]
    assert _recall_event_ids(sink_b) == [eid_b]
