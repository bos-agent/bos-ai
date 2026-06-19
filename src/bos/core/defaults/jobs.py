"""Default in-process JobRunner — BEP 11 §2.

v1: in-process, reliable with graceful drain. No persistent JobStore;
no cron; bounded asyncio.Queue for queued work; one in-flight per key."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from bos.core.contract import (
    Job,
    JobRecord,
    JobStatus,
    JobTrigger,
    LifecycleBus,
    LifecycleEvent,
    ep_job_runner,
)

logger = logging.getLogger(__name__)


def _parse_duration(value: str | int | float) -> float:
    """Accept '5m' / '30s' / '1h' / int seconds. Return seconds as float."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh])?\s*", value)
    if not m:
        raise ValueError(f"unrecognized duration: {value!r}")
    n, unit = float(m.group(1)), (m.group(2) or "s")
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


@ep_job_runner(name="_default")
class InProcJobRunner:
    def __init__(
        self,
        bus: LifecycleBus | None = None,
        *,
        max_concurrency: int = 2,
        idle_after: str | int | float = 300,
    ) -> None:
        self._bus = bus
        self._max_concurrency = max(1, int(max_concurrency))
        self._idle_after = _parse_duration(idle_after)
        self._queue: asyncio.Queue[tuple[str, Job]] = asyncio.Queue()
        self._records: dict[str, JobRecord] = {}
        self._inflight_by_key: dict[str, str] = {}  # key -> job_id
        self._workers: list[asyncio.Task] = []
        self._idle_timers: dict[str, asyncio.TimerHandle] = {}
        self._trigger_factories: dict[JobTrigger, Callable[[LifecycleEvent | None], Job | None]] = {}
        self._started = False
        self._draining = asyncio.Event()

    # ── lifecycle ──

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._draining.clear()
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"job-runner-worker-{i}") for i in range(self._max_concurrency)
        ]

    async def drain(self, *, timeout: float) -> None:
        """Graceful shutdown: wait up to `timeout` for the queue to drain naturally
        (queued jobs picked up, in-flight finished). At the deadline, set the
        draining flag, cancel workers — any still-queued jobs are dropped on the
        worker's next iteration and any in-flight task raises CancelledError."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while self._queue.qsize() > 0 or any(self._record_status_running(rec) for rec in self._records.values()):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.005, remaining))
        self._draining.set()
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()
        for timer in self._idle_timers.values():
            timer.cancel()
        self._idle_timers.clear()
        self._started = False

    # ── public API ──

    async def submit(self, job: Job) -> str:
        if job.key in self._inflight_by_key:
            return self._inflight_by_key[job.key]
        if self._draining.is_set():
            logger.info("submit during drain; job %s will likely be dropped", job.key)
        job_id = uuid.uuid4().hex
        self._records[job_id] = JobRecord(
            id=job_id,
            key=job.key,
            status="queued",
            error=None,
            submitted_at=datetime.now().isoformat(),
            finished_at=None,
        )
        self._inflight_by_key[job.key] = job_id
        await self._queue.put((job_id, job))
        return job_id

    def bind_trigger(
        self,
        trigger: JobTrigger,
        factory: Callable[[LifecycleEvent | None], Job | None],
    ) -> None:
        self._trigger_factories[trigger] = factory
        if self._bus is None:
            return
        if trigger == "session_close":
            self._bus.subscribe("session_close", self._on_session_close)
        elif trigger == "idle":
            # arm/refresh per-chat timer on each turn_complete
            self._bus.subscribe("turn_complete", self._on_turn_complete_for_idle)

    async def status(self, job_id: str) -> JobStatus:
        rec = self._records.get(job_id)
        return rec.status if rec else "cancelled"

    async def list(self, *, filter: dict | None = None) -> list[JobRecord]:
        records = list(self._records.values())
        if filter:
            for k, v in filter.items():
                records = [r for r in records if getattr(r, k, None) == v]
        return records

    async def retry(self, job_id: str) -> None:
        # Out of scope for v1 (no JobStore retain); a no-op stub kept for protocol parity
        logger.info("retry not implemented in v1 (job_id=%s)", job_id)

    async def cancel(self, job_id: str) -> None:
        rec = self._records.get(job_id)
        if rec and rec.status == "queued":
            self._records[job_id] = self._with(rec, status="cancelled", finished_at=datetime.now().isoformat())
            self._inflight_by_key.pop(rec.key, None)

    # ── internals ──

    @staticmethod
    def _record_status_running(rec: JobRecord) -> bool:
        return rec.status == "running"

    @staticmethod
    def _with(rec: JobRecord, **changes: Any) -> JobRecord:
        return JobRecord(
            id=rec.id,
            key=changes.get("key", rec.key),
            status=changes.get("status", rec.status),
            error=changes.get("error", rec.error),
            submitted_at=rec.submitted_at,
            finished_at=changes.get("finished_at", rec.finished_at),
        )

    async def _worker(self, n: int) -> None:
        while True:
            job_id, job = await self._queue.get()
            if self._records[job_id].status != "queued":
                # cancel() flips a still-queued record to "cancelled" but cannot
                # pull its tuple out of the queue; drop it here instead of running.
                self._inflight_by_key.pop(job.key, None)
                continue
            if self._draining.is_set():
                self._records[job_id] = self._with(
                    self._records[job_id],
                    status="cancelled",
                    finished_at=datetime.now().isoformat(),
                )
                self._inflight_by_key.pop(job.key, None)
                continue
            self._records[job_id] = self._with(self._records[job_id], status="running")
            try:
                await job.run()
                self._records[job_id] = self._with(
                    self._records[job_id],
                    status="succeeded",
                    finished_at=datetime.now().isoformat(),
                )
            except asyncio.CancelledError:
                # drain() cancels workers mid-run; CancelledError is a
                # BaseException, so it bypasses `except Exception` below. Mark
                # the record terminal before re-raising, otherwise it stays
                # "running" forever and every later drain() busy-waits on the
                # phantom until its own timeout.
                self._records[job_id] = self._with(
                    self._records[job_id],
                    status="cancelled",
                    finished_at=datetime.now().isoformat(),
                )
                raise
            except Exception as e:
                logger.exception("job %s (%s) failed", job_id, job.key)
                self._records[job_id] = self._with(
                    self._records[job_id],
                    status="failed",
                    error=str(e),
                    finished_at=datetime.now().isoformat(),
                )
            finally:
                self._inflight_by_key.pop(job.key, None)

    async def _on_session_close(self, event: LifecycleEvent) -> None:
        factory = self._trigger_factories.get("session_close")
        if factory is None:
            return
        job = factory(event)
        if job is not None:
            await self.submit(job)

    async def _on_turn_complete_for_idle(self, event: LifecycleEvent) -> None:
        if "idle" not in self._trigger_factories:
            return
        existing = self._idle_timers.pop(event.chat_id, None)
        if existing is not None:
            existing.cancel()
        loop = asyncio.get_event_loop()
        self._idle_timers[event.chat_id] = loop.call_later(
            self._idle_after,
            lambda: asyncio.create_task(self._fire_idle(event)),
        )

    async def _fire_idle(self, event: LifecycleEvent) -> None:
        self._idle_timers.pop(event.chat_id, None)
        factory = self._trigger_factories.get("idle")
        if factory is None:
            return
        job = factory(event)
        if job is not None:
            await self.submit(job)
