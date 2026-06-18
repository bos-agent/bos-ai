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
            Job, JobRecord, JobRunner, JobStatus, JobTrigger, ep_job_runner,
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
