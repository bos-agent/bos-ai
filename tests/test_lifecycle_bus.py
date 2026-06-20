"""LifecycleBus — in-process pub/sub for turn_complete / session_close."""

import logging

import pytest

from bos.core.contract import LifecycleEvent
from bos.core.defaults.lifecycle import DefaultLifecycleBus


def _event(kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload=None):
    return LifecycleEvent(
        kind=kind,
        chat_id=chat_id,
        actor_name=actor_name,
        base_revision=base_revision,
        payload=payload or {},
    )


class TestLifecycleBus:
    @pytest.mark.asyncio
    async def test_subscriber_receives_event(self):
        bus = DefaultLifecycleBus()
        seen = []

        async def handler(e):
            seen.append(e)

        bus.subscribe("turn_complete", handler)
        await bus.emit(_event())
        assert len(seen) == 1
        assert seen[0].base_revision == 1

    @pytest.mark.asyncio
    async def test_subscribers_only_get_their_kind(self):
        bus = DefaultLifecycleBus()
        turns, closes = [], []

        async def t_handler(e):
            turns.append(e)

        async def c_handler(e):
            closes.append(e)

        bus.subscribe("turn_complete", t_handler)
        bus.subscribe("session_close", c_handler)
        await bus.emit(_event(kind="session_close"))
        assert closes and not turns

    @pytest.mark.asyncio
    async def test_subscriber_failure_does_not_break_fanout(self, caplog):
        bus = DefaultLifecycleBus()
        delivered = []

        async def angry(e):
            raise RuntimeError("boom")

        async def calm(e):
            delivered.append(e)

        bus.subscribe("turn_complete", angry)
        bus.subscribe("turn_complete", calm)
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
        bus = DefaultLifecycleBus()
        await bus.emit(_event())
