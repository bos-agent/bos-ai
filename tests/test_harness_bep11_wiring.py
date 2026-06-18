"""End-to-end wiring of BEP 11 services into the harness."""

import pytest

import bos.exts  # noqa: F401 — registers default ep impls (mail_route, chat_store, ...)
from bos.config.schema import HarnessConfig


class TestHarnessConfig:
    def test_default_job_runner_field(self):
        cfg = HarnessConfig()
        assert cfg.job_runner == "_default"

    def test_explicit_job_runner_field(self):
        cfg = HarnessConfig(job_runner="custom")
        assert cfg.job_runner == "custom"

    def test_unknown_field_still_rejected(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            HarnessConfig(unknown="x")


class TestHarnessServices:
    @pytest.mark.asyncio
    async def test_services_exposed_on_plugin_services(self, tmp_path):
        from bos.core.harness import AgentHarness

        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            svc = h._plugin_services
            assert svc.events is not None
            assert svc.jobs is not None
            assert svc.background_llm is not None
            assert h.jobs is svc.jobs

    @pytest.mark.asyncio
    async def test_drain_called_before_other_teardown(self, tmp_path):
        from bos.core.harness import AgentHarness

        order: list[str] = []

        class _SpyJobRunner:
            async def start(self): pass
            async def drain(self, *, timeout): order.append("drain")
            def bind_trigger(self, *a, **kw): pass
            async def submit(self, job): return "x"
            async def status(self, jid): return "queued"
            async def list(self, *, filter=None): return []
            async def retry(self, jid): pass
            async def cancel(self, jid): pass
            async def aclose(self): order.append("aclose")

        h = AgentHarness(bos_dir=tmp_path, workspace=tmp_path)
        await h.__aenter__()
        try:
            spy = _SpyJobRunner()
            h.jobs = spy
            h._owned.append(spy)
        finally:
            await h.__aexit__(None, None, None)
        assert order.index("drain") < order.index("aclose")


class TestTurnCompleteEmission:
    @pytest.mark.asyncio
    async def test_turn_complete_emits_with_base_revision(self):
        """When CoordinatedActor's _on_turn_finished sees status='completed',
        a LifecycleEvent fires on the injected bus with the committed revision."""
        from bos.core.actor import ActorTurnContext, ActorTurnResult
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.actor_manager import CoordinatedActor

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe("turn_complete", handler)

        actor = CoordinatedActor.__new__(CoordinatedActor)
        actor._lifecycle_bus = bus
        actor._chat_coordinator = _DummyCoordinator()
        actor._mailbox = None

        ctx = ActorTurnContext(
            chat_id="c1", actor_name="A", actor_address="A@local",
            turn_id="t1", reply_recipient="user",
        )
        await actor._on_turn_finished(ctx, ActorTurnResult(status="completed", committed_revision=4))
        assert len(seen) == 1
        assert seen[0].kind == "turn_complete"
        assert seen[0].chat_id == "c1"
        assert seen[0].actor_name == "A"
        assert seen[0].base_revision == 4

    @pytest.mark.asyncio
    async def test_turn_complete_skipped_when_not_completed(self):
        from bos.core.actor import ActorTurnContext, ActorTurnResult
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.actor_manager import CoordinatedActor

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe("turn_complete", handler)
        actor = CoordinatedActor.__new__(CoordinatedActor)
        actor._lifecycle_bus = bus
        actor._chat_coordinator = _DummyCoordinator()
        actor._mailbox = _NoopMailbox()
        ctx = ActorTurnContext(
            chat_id="c1", actor_name="A", actor_address="A@local",
            turn_id="t1", reply_recipient="",
        )
        await actor._on_turn_finished(ctx, ActorTurnResult(status="aborted"))
        await actor._on_turn_finished(ctx, ActorTurnResult(status="error"))
        await actor._on_turn_finished(ctx, ActorTurnResult(status="stale"))
        assert seen == []


class _DummyCoordinator:
    def end_turn(self, **kw): pass


class _NoopMailbox:
    async def send(self, *a, **kw): pass
