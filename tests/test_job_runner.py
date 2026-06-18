"""InProcJobRunner — submit, dedup, drain, bind_trigger, idle timer."""

import asyncio
from dataclasses import dataclass

import pytest

from bos.core.contract import LifecycleEvent
from bos.core.defaults.jobs import InProcJobRunner
from bos.core.defaults.lifecycle import DefaultLifecycleBus


@dataclass
class _RecJob:
    key: str
    log: list[str]
    delay: float = 0.0

    async def run(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.log.append(self.key)


class TestStructural:
    def test_types_exist(self):
        from bos.core.contract import (
            Job,
            JobRecord,
            JobRunner,
            JobStatus,
            JobTrigger,
            ep_job_runner,
        )
        assert "session_close" in JobTrigger.__args__
        assert "manual" in JobTrigger.__args__
        assert "queued" in JobStatus.__args__
        assert hasattr(Job, "run")
        assert hasattr(JobRunner, "submit")
        assert hasattr(JobRunner, "drain")
        assert ep_job_runner.name == "ep_job_runner"
        rec = JobRecord(id="x", key="k", status="queued", error=None,
                        submitted_at="2026-06-17T00:00:00", finished_at=None)
        assert rec.id == "x"


class TestSubmitAndDrain:
    @pytest.mark.asyncio
    async def test_submit_runs_job(self, tmp_path):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=2, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            jid = await runner.submit(_RecJob(key="k1", log=log))
            await runner.drain(timeout=1.0)
            assert log == ["k1"]
            assert await runner.status(jid) == "succeeded"
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_dedup_on_key_in_flight(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            id1 = await runner.submit(_RecJob(key="same", log=log, delay=0.05))
            id2 = await runner.submit(_RecJob(key="same", log=log, delay=0.05))
            assert id1 == id2
            await runner.drain(timeout=1.0)
            assert log == ["same"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_drain_drops_queued_when_timeout_exhausted(self):
        """When drain's timeout elapses with work still queued behind an in-flight
        job, the queued job is dropped — never starts."""
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            # 'hog' takes 0.50s; drain's window is much shorter
            await runner.submit(_RecJob(key="hog", log=log, delay=0.50))
            await runner.submit(_RecJob(key="behind", log=log))
            await runner.drain(timeout=0.05)
            assert "behind" not in log
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_failed_job_records_error(self):
        class _Boom:
            key = "boom"
            async def run(self):
                raise RuntimeError("kaboom")

        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            jid = await runner.submit(_Boom())
            await runner.drain(timeout=1.0)
            assert await runner.status(jid) == "failed"
            records = await runner.list(filter={"status": "failed"})
            assert records and "kaboom" in (records[0].error or "")
        finally:
            await runner.drain(timeout=0.0)


class TestTriggers:
    @pytest.mark.asyncio
    async def test_session_close_factory_enqueues_a_job(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []

            def factory(event):
                return _RecJob(key=f"closed:{event.chat_id}:r{event.base_revision}", log=log)

            runner.bind_trigger("session_close", factory)
            await bus.emit(LifecycleEvent(
                kind="session_close", chat_id="c1", actor_name="A",
                base_revision=7, payload={},
            ))
            await runner.drain(timeout=1.0)
            assert log == ["closed:c1:r7"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_timer_fires_after_idle_after(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=0.05)
        await runner.start()
        try:
            log: list[str] = []

            def factory(event):
                return _RecJob(key=f"idle:{event.chat_id}", log=log)

            runner.bind_trigger("idle", factory)
            await bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id="c1", actor_name="A",
                base_revision=1, payload={},
            ))
            await asyncio.sleep(0.20)
            await runner.drain(timeout=0.5)
            assert log == ["idle:c1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_timer_resets_on_subsequent_turn(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=0.10)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("idle", lambda e: _RecJob(key="idle1", log=log))
            await bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload={},
            ))
            await asyncio.sleep(0.05)
            await bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id="c1", actor_name="A", base_revision=2, payload={},
            ))
            await asyncio.sleep(0.05)
            assert log == []
            await asyncio.sleep(0.15)
            await runner.drain(timeout=0.5)
            assert log == ["idle1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_factory_returning_none_is_skipped(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("session_close", lambda e: None)
            await bus.emit(LifecycleEvent(
                kind="session_close", chat_id="c1", actor_name="A", base_revision=1, payload={},
            ))
            await runner.drain(timeout=0.2)
            assert log == []
        finally:
            await runner.drain(timeout=0.0)
