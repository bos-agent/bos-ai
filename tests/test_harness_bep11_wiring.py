"""End-to-end wiring of BEP 11 services into the harness."""

import pytest

import bos.exts  # noqa: F401 — registers default ep impls (mail_route, chat_store, ...)
from bos.config.schema import HarnessConfig
from bos.core.contract import SessionEvent


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
            async def start(self):
                pass

            async def drain(self, *, timeout):
                order.append("drain")

            def bind_trigger(self, *a, **kw):
                pass

            async def submit(self, job):
                return "x"

            async def status(self, jid):
                return "queued"

            async def list(self, *, filter=None):
                return []

            async def retry(self, jid):
                pass

            async def cancel(self, jid):
                pass

            async def aclose(self):
                order.append("aclose")

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
        """When AgentActor's _on_turn_finished sees status='completed',
        a LifecycleEvent fires on the injected bus with the committed revision."""
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.agent_actor import ActorTurnContext, ActorTurnResult, AgentActor

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe(SessionEvent, handler)

        actor = AgentActor.__new__(AgentActor)
        actor._event_bus = bus
        actor._chat_coordinator = _DummyCoordinator()
        actor._mailbox = None

        ctx = ActorTurnContext(
            chat_id="c1",
            actor_name="A",
            actor_address="A@local",
            turn_id="t1",
            reply_recipient="user",
        )
        await actor._on_turn_finished(ctx, ActorTurnResult(status="completed", committed_revision=4))
        assert len(seen) == 1
        assert seen[0].kind == "turn_complete"
        assert seen[0].chat_id == "c1"
        assert seen[0].actor_name == "A"
        assert seen[0].base_revision == 4
        assert seen[0].turn_id == "t1"

    @pytest.mark.asyncio
    async def test_turn_complete_skipped_when_not_completed(self):
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.agent_actor import ActorTurnContext, ActorTurnResult, AgentActor

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe(SessionEvent, handler)
        actor = AgentActor.__new__(AgentActor)
        actor._event_bus = bus
        actor._chat_coordinator = _DummyCoordinator()
        actor._mailbox = _NoopMailbox()
        ctx = ActorTurnContext(
            chat_id="c1",
            actor_name="A",
            actor_address="A@local",
            turn_id="t1",
            reply_recipient="",
        )
        await actor._on_turn_finished(ctx, ActorTurnResult(status="aborted"))
        await actor._on_turn_finished(ctx, ActorTurnResult(status="error"))
        await actor._on_turn_finished(ctx, ActorTurnResult(status="stale"))
        assert seen == []


class _DummyCoordinator:
    def end_turn(self, **kw):
        pass


class _NoopMailbox:
    async def send(self, *a, **kw):
        pass


class _StubAgent:
    name = "A"


class TestSessionCloseEmission:
    @pytest.mark.asyncio
    async def test_retire_session_emits_session_close_with_none_revision_when_empty(self):
        """A session that never committed a turn closes with base_revision=None."""
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.agent_actor import AgentActor, SessionState

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe(SessionEvent, handler)

        actor = AgentActor.__new__(AgentActor)
        actor._sessions = {"c1": SessionState(chat_id="c1")}
        actor._event_bus = bus
        actor._agent = _StubAgent()
        actor._address = "agent@A"
        await actor.retire_session("c1")
        assert len(seen) == 1
        assert seen[0].kind == "session_close"
        assert (seen[0].chat_id, seen[0].actor_name, seen[0].base_revision) == ("c1", "A", None)
        assert seen[0].turn_id is None

    @pytest.mark.asyncio
    async def test_retire_session_forwards_last_committed_revision(self):
        """When the session has observed a commit, retire_session forwards that revision."""
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.agent_actor import AgentActor, SessionState

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe(SessionEvent, handler)

        actor = AgentActor.__new__(AgentActor)
        session = SessionState(chat_id="c1")
        session.execution.last_committed_revision = 7
        actor._sessions = {"c1": session}
        actor._event_bus = bus
        actor._agent = _StubAgent()
        actor._address = "agent@A"
        await actor.retire_session("c1")
        assert len(seen) == 1
        assert (seen[0].chat_id, seen[0].actor_name, seen[0].base_revision) == ("c1", "A", 7)

    @pytest.mark.asyncio
    async def test_retire_session_no_bus_is_silent(self):
        from bos.gateway.agent_actor import AgentActor, SessionState

        actor = AgentActor.__new__(AgentActor)
        actor._sessions = {"c1": SessionState(chat_id="c1")}
        actor._agent = _StubAgent()
        actor._address = "agent@A"
        actor._event_bus = None  # no bus → emit is a silent no-op
        await actor.retire_session("c1")


class TestSessionCloseFactoryGuard:
    @pytest.mark.asyncio
    async def test_factory_returns_none_when_event_has_no_revision(self, tmp_path):
        """An empty-session session_close (base_revision=None) → factory returns
        None → JobRunner does not enqueue a no-op consolidation job."""
        from bos.core.contract import LifecycleEvent
        from bos.core.harness import AgentHarness
        from bos.plugins.memory.plugin import MemoryHarnessPlugin

        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            plugin = MemoryHarnessPlugin()
            plugin._cfg = {
                **plugin.default_config(),
                "backend": "in_memory",
                "consolidation": {"enabled": True, "retention_days": 30},
            }
            await plugin.setup(h._plugin_services)
            await h.events.emit(
                LifecycleEvent(
                    kind="session_close",
                    chat_id="c-empty",
                    actor_name="A",
                    base_revision=None,
                    payload={},
                )
            )
            await h.jobs.drain(timeout=0.5)
            await h.jobs.start()
            # No job was enqueued for the empty session
            assert (await h.jobs.list()) == []


class TestE2E:
    @pytest.mark.asyncio
    async def test_plugin_can_bind_trigger_and_receive_event(self, tmp_path):
        """A consumer binds 'session_close' on the JobRunner, emits an event
        via the bus, the bound factory builds a Job, the runner runs it,
        the side effect is observable. This is the BEP 10 consolidation flow's
        contract."""
        from bos.core.contract import LifecycleEvent
        from bos.core.harness import AgentHarness

        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            log: list[int] = []

            class _RecJob:
                def __init__(self, key, rev):
                    self.key = key
                    self._rev = rev

                async def run(self):
                    log.append(self._rev)

            def factory(event):
                if event is None:
                    return None
                return _RecJob(key=f"{event.chat_id}:{event.base_revision}", rev=event.base_revision or 0)

            h.jobs.bind_trigger("session_close", factory)
            await h.events.emit(
                LifecycleEvent(
                    kind="session_close",
                    chat_id="c1",
                    actor_name="A",
                    base_revision=42,
                    payload={},
                )
            )
            await h.jobs.drain(timeout=1.0)
            # restart so harness __aexit__ can drain cleanly
            await h.jobs.start()
            assert log == [42]
