"""HostChannelSink: opt-in pub/sub for in-turn events.

Handlers register per event_type; emit() dispatches to all registered handlers
for that type. Events with no registered handler drop silently — the mailbox
forwarder is just one consumer that registers for the client-facing types.
"""

import pytest

from bos.core.agent import TurnEvent
from bos.core.sinks import HostChannelSink


def _evt(event_type: str, **kw) -> TurnEvent:
    return TurnEvent(
        event_type=event_type,
        phase=kw.pop("phase", "start"),
        chat_id=kw.pop("chat_id", "c1"),
        turn_id=kw.pop("turn_id", "t1"),
        **kw,
    )


@pytest.mark.asyncio
async def test_dispatches_to_registered_handler():
    seen: list[TurnEvent] = []
    sink = HostChannelSink()
    sink.on("turn", seen.append)
    await sink.emit(_evt("turn"))
    assert len(seen) == 1
    assert seen[0].event_type == "turn"


@pytest.mark.asyncio
async def test_unregistered_event_drops_silently():
    sink = HostChannelSink()
    # no handlers registered at all
    await sink.emit(_evt("memory.recalled"))  # must not raise


@pytest.mark.asyncio
async def test_multiple_handlers_for_same_event_type():
    a: list[TurnEvent] = []
    b: list[TurnEvent] = []
    sink = HostChannelSink()
    sink.on("turn", a.append)
    sink.on("turn", b.append)
    await sink.emit(_evt("turn"))
    assert len(a) == 1
    assert len(b) == 1


@pytest.mark.asyncio
async def test_handlers_isolated_by_event_type():
    turn_seen: list[TurnEvent] = []
    recall_seen: list[TurnEvent] = []
    sink = HostChannelSink()
    sink.on("turn", turn_seen.append)
    sink.on("memory.recalled", recall_seen.append)

    await sink.emit(_evt("turn"))
    await sink.emit(_evt("memory.recalled"))
    await sink.emit(_evt("llm"))  # no handler — drops

    assert [e.event_type for e in turn_seen] == ["turn"]
    assert [e.event_type for e in recall_seen] == ["memory.recalled"]


@pytest.mark.asyncio
async def test_async_handler_is_awaited():
    seen: list[TurnEvent] = []

    async def handler(event: TurnEvent) -> None:
        seen.append(event)

    sink = HostChannelSink()
    sink.on("turn", handler)
    await sink.emit(_evt("turn"))
    assert len(seen) == 1
