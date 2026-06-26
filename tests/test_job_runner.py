"""InProcJobRunner — submit, dedup, drain, bind_trigger, idle timer."""

import asyncio
from dataclasses import dataclass

import pytest

from bos.core.contract import SessionEvent
from bos.core.defaults.eventbus import DefaultEventBus
from bos.core.defaults.job_runner import InProcJobRunner


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
        rec = JobRecord(
            id="x", key="k", status="queued", error=None, submitted_at="2026-06-17T00:00:00", finished_at=None
        )
        assert rec.id == "x"


class TestSubmitAndDrain:
    @pytest.mark.asyncio
    async def test_submit_runs_job(self, tmp_path):
        bus = DefaultEventBus()
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
        bus = DefaultEventBus()
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
        bus = DefaultEventBus()
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
    async def test_cancel_before_pickup_prevents_run(self):
        """Regression: cancel() on a still-queued job must keep the worker from
        running it. The cancel flips the record to 'cancelled' but leaves the
        tuple in the queue; the worker must drop it on dequeue rather than
        overwrite the status with 'running' and execute job.run()."""
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            # 'hog' occupies the single worker so 'target' stays queued.
            await runner.submit(_RecJob(key="hog", log=log, delay=0.20))
            target = await runner.submit(_RecJob(key="target", log=log))
            await runner.cancel(target)
            assert await runner.status(target) == "cancelled"
            await runner.drain(timeout=1.0)
            assert "target" not in log
            assert await runner.status(target) == "cancelled"
            # the cancelled key is freed so a later submit of the same key works
            assert "target" not in runner._inflight_by_key
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_cancelled_job_does_not_evict_resubmitted_key(self):
        """Regression: dropping a cancelled job must not release a *newer* job's
        key reservation. Sequence: submit A (key=k) -> cancel A (frees k) ->
        submit B (key=k, takes k). When the worker later drops the cancelled A,
        a blind pop(k) would evict B's reservation, breaking dedup and letting a
        third same-key submit run concurrently with B."""
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        gate = asyncio.Event()
        try:
            log: list[str] = []

            class _Gated:
                key = "hog"

                async def run(self):
                    await gate.wait()

            # Occupy the single worker so A and B both stay queued.
            await runner.submit(_Gated())
            id_a = await runner.submit(_RecJob(key="same", log=log))
            await runner.cancel(id_a)
            id_b = await runner.submit(_RecJob(key="same", log=log, delay=0.20))
            assert id_a != id_b
            assert runner._inflight_by_key["same"] == id_b

            # Release the hog: worker drops cancelled A, then runs B.
            gate.set()
            await asyncio.sleep(0.05)  # land inside B's run window
            # B's reservation must survive A's drop.
            assert runner._inflight_by_key.get("same") == id_b
            # so a same-key submit while B runs dedups to B (no concurrent run).
            id_c = await runner.submit(_RecJob(key="same", log=log))
            assert id_c == id_b

            await runner.drain(timeout=1.0)
            assert log == ["same"]  # B ran exactly once; no concurrent duplicate
        finally:
            gate.set()
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_drain_mid_job_marks_record_cancelled_not_stuck_running(self):
        """Regression: when drain cancels a worker mid-run, CancelledError must
        not leave the JobRecord stuck at 'running'. Otherwise the phantom keeps
        drain's wait-loop condition true and every subsequent drain busy-waits
        to its own timeout."""
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            jid = await runner.submit(_RecJob(key="slow", log=log, delay=0.50))
            await asyncio.sleep(0.02)  # let the worker pick it up -> "running"
            await runner.drain(timeout=0.05)  # deadline elapses, worker cancelled mid-run
            assert await runner.status(jid) == "cancelled"

            # A second drain on the same instance must return promptly, not
            # busy-wait on a phantom 'running' record.
            loop = asyncio.get_event_loop()
            t0 = loop.time()
            await runner.drain(timeout=0.50)
            assert loop.time() - t0 < 0.25
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_drain_propagates_external_cancellation(self):
        """Regression: if the drain() coroutine is itself cancelled (cooperative
        shutdown) while awaiting workers, that cancellation must propagate out —
        not be swallowed by the worker-reaping await and silently complete."""
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:

            class _SlowToCancel:
                key = "slow"

                async def run(self):
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        # linger so drain stays suspended awaiting this worker,
                        # giving the test a window to cancel the drain coroutine.
                        await asyncio.sleep(0.20)
                        raise

            await runner.submit(_SlowToCancel())
            await asyncio.sleep(0.02)  # worker picks it up -> running
            drain_task = asyncio.create_task(runner.drain(timeout=0.0))
            await asyncio.sleep(0.02)  # drain past deadline, now awaiting the worker
            drain_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain_task
        finally:
            await runner.drain(timeout=1.0)

    @pytest.mark.asyncio
    async def test_failed_job_records_error(self):
        class _Boom:
            key = "boom"

            async def run(self):
                raise RuntimeError("kaboom")

        bus = DefaultEventBus()
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
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []

            def factory(event):
                return _RecJob(key=f"closed:{event.chat_id}:r{event.base_revision}", log=log)

            runner.bind_trigger("session_close", factory)
            await bus.emit(
                SessionEvent(
                    kind="session_close",
                    chat_id="c1",
                    actor_name="A",
                    base_revision=7,
                    payload={},
                )
            )
            await runner.drain(timeout=1.0)
            assert log == ["closed:c1:r7"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_timer_fires_after_idle_after(self):
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=0.05)
        await runner.start()
        try:
            log: list[str] = []

            def factory(event):
                return _RecJob(key=f"idle:{event.chat_id}", log=log)

            runner.bind_trigger("idle", factory)
            await bus.emit(
                SessionEvent(
                    kind="turn_complete",
                    chat_id="c1",
                    actor_name="A",
                    base_revision=1,
                    payload={},
                )
            )
            await asyncio.sleep(0.20)
            await runner.drain(timeout=0.5)
            assert log == ["idle:c1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_timer_resets_on_subsequent_turn(self):
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=0.10)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("idle", lambda e: _RecJob(key="idle1", log=log))
            await bus.emit(
                SessionEvent(
                    kind="turn_complete",
                    chat_id="c1",
                    actor_name="A",
                    base_revision=1,
                    payload={},
                )
            )
            await asyncio.sleep(0.05)
            await bus.emit(
                SessionEvent(
                    kind="turn_complete",
                    chat_id="c1",
                    actor_name="A",
                    base_revision=2,
                    payload={},
                )
            )
            await asyncio.sleep(0.05)
            assert log == []
            await asyncio.sleep(0.15)
            await runner.drain(timeout=0.5)
            assert log == ["idle1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_fire_task_is_retained_and_runs(self):
        """Regression: the idle-fire task spawned by the timer must be strongly
        retained. asyncio keeps only a weak reference to a bare create_task()
        result, so without a strong ref a GC pass before it runs could collect
        it, silently dropping the idle consolidation."""
        import gc

        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("idle", lambda e: _RecJob(key=f"idle:{e.chat_id}", log=log))
            event = SessionEvent(
                kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload={}
            )
            runner._spawn_idle(event)  # what the call_later timer callback invokes
            # Pending fire-task is retained and survives a GC pass before it runs.
            assert len(runner._idle_tasks) == 1
            gc.collect()
            assert len(runner._idle_tasks) == 1
            await asyncio.sleep(0.02)  # let the fire-task run -> submit the job
            assert runner._idle_tasks == set()  # done-callback cleaned it up
            await runner.drain(timeout=1.0)
            assert log == ["idle:c1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_factory_returning_none_is_skipped(self):
        bus = DefaultEventBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("session_close", lambda e: None)
            await bus.emit(
                SessionEvent(
                    kind="session_close",
                    chat_id="c1",
                    actor_name="A",
                    base_revision=1,
                    payload={},
                )
            )
            await runner.drain(timeout=0.2)
            assert log == []
        finally:
            await runner.drain(timeout=0.0)
