"""EventBus — in-process, type-keyed pub/sub over platform Events (BEP 13 §2.10)."""

import logging

import pytest

from bos.core.actor import Event
from bos.core.contract import SessionEvent
from bos.core.defaults.eventbus import DefaultEventBus


def _event(kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload=None):
    return SessionEvent(
        kind=kind,
        chat_id=chat_id,
        actor_name=actor_name,
        base_revision=base_revision,
        payload=payload or {},
    )


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscriber_receives_its_event_type(self):
        bus = DefaultEventBus()
        seen = []

        async def handler(e):
            seen.append(e)

        bus.subscribe(SessionEvent, handler)
        await bus.emit(_event())
        assert len(seen) == 1
        assert seen[0].base_revision == 1

    @pytest.mark.asyncio
    async def test_subscriber_gets_all_events_of_its_type(self):
        # Type-keyed: a SessionEvent subscriber receives every SessionEvent
        # regardless of .kind; discriminating on kind is the subscriber's job.
        bus = DefaultEventBus()
        seen = []

        async def handler(e):
            seen.append(e.kind)

        bus.subscribe(SessionEvent, handler)
        await bus.emit(_event(kind="turn_complete"))
        await bus.emit(_event(kind="session_close"))
        assert seen == ["turn_complete", "session_close"]

    @pytest.mark.asyncio
    async def test_subscribing_to_base_event_taps_subclasses(self):
        bus = DefaultEventBus()
        seen = []

        async def handler(e):
            seen.append(e)

        bus.subscribe(Event, handler)  # base marker → receives any Event subclass
        await bus.emit(_event())
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_subscriber_failure_does_not_break_fanout(self, caplog):
        bus = DefaultEventBus()
        delivered = []

        async def angry(e):
            raise RuntimeError("boom")

        async def calm(e):
            delivered.append(e)

        bus.subscribe(SessionEvent, angry)
        bus.subscribe(SessionEvent, calm)
        with caplog.at_level(logging.ERROR):
            await bus.emit(_event())
        assert len(delivered) == 1
        # exception is logged via logger.exception — check exc_info, not the format string
        assert any(
            r.exc_info is not None and isinstance(r.exc_info[1], RuntimeError) and "boom" in str(r.exc_info[1])
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_subscribers_is_noop(self):
        bus = DefaultEventBus()
        await bus.emit(_event())
