"""MemoryHarnessPlugin wiring tests — lazy per-agent registry, session_close
factory dispatches by event.actor_name, isolation between agents."""

import pytest

import bos.exts  # noqa: F401 — registers default extensions
from bos.core.contract import PluginServices
from bos.core.defaults.background_llm import DefaultBackgroundLLM
from bos.core.defaults.eventbus import DefaultEventBus
from bos.core.defaults.jobs import InProcJobRunner
from bos.plugins.memory.operation_service import DefaultMemoryOperationService
from bos.plugins.memory.plugin import MemoryHarnessPlugin


async def _setup_plugin(tmp_path, *, consolidation_enabled=False, idle_after=300):
    bus = DefaultEventBus()
    runner = InProcJobRunner(bus, max_concurrency=1, idle_after=idle_after)
    await runner.start()
    from bos.extensions.chat_stores.in_memory import InMemChatStore

    class _StubLLM:
        async def complete(self, messages, **kwargs):
            from bos.core.llm import LLMResponse

            return LLMResponse(content='{"operations": []}')

    blm = DefaultBackgroundLLM(_StubLLM())
    chat_store = InMemChatStore()
    svc = PluginServices(
        bos_dir=tmp_path,
        workspace=tmp_path,
        llm=_StubLLM(),
        consolidator=None,
        subagents=None,
        chat_store=chat_store,
        events=bus,
        jobs=runner,
        background_llm=blm,
    )
    h = MemoryHarnessPlugin()
    cfg = {**h.default_config(), "backend": "in_memory"}
    if consolidation_enabled:
        cfg = {**cfg, "consolidation": {"enabled": True, "retention_days": 30}}
    h._cfg = cfg
    await h.setup(svc)
    return h, runner


@pytest.mark.asyncio
async def test_setup_does_not_build_per_agent_eagerly(tmp_path):
    h, runner = await _setup_plugin(tmp_path)
    try:
        # No agent bound yet → no per-agent state
        assert h._per_agent == {}
        # After explicit bind for "alice", the bundle exists and is the right shape
        h.bind({**h._cfg, "agent_name": "alice"})
        bundle = h._for("alice")
        assert bundle.backend is not None
        assert isinstance(bundle.op_service, DefaultMemoryOperationService)
        assert bundle.watermarks is not None
        assert bundle.consolidator is not None
    finally:
        await runner.drain(timeout=0.5)


@pytest.mark.asyncio
async def test_validate_config_rejects_scope_key(tmp_path):
    h, runner = await _setup_plugin(tmp_path)
    try:
        with pytest.raises(ValueError, match="scope"):
            h.validate_config({"scope": "alice"})
    finally:
        await runner.drain(timeout=0.5)


@pytest.mark.asyncio
async def test_bind_requires_agent_name(tmp_path):
    h, runner = await _setup_plugin(tmp_path)
    try:
        with pytest.raises(ValueError, match="agent_name"):
            h.bind({**h._cfg})  # missing agent_name
    finally:
        await runner.drain(timeout=0.5)


@pytest.mark.asyncio
async def test_two_agents_get_isolated_bundles(tmp_path):
    h, runner = await _setup_plugin(tmp_path)
    try:
        h.bind({**h._cfg, "agent_name": "alice"})
        h.bind({**h._cfg, "agent_name": "bob"})
        alice = h._for("alice")
        bob = h._for("bob")
        assert alice is not bob
        assert alice.backend is not bob.backend
        assert alice.op_service is not bob.op_service
        # Writes through alice's op_service don't appear in bob's
        from bos.plugins.memory.operation_service import MemoryOperation

        await alice.op_service.apply(
            [
                MemoryOperation(op="ADD", reason="alice fact", content="alice loves Python"),
            ],
            window_turn_ids=[],
        )
        assert (await alice.backend.search_memories("Python")) != []
        assert (await bob.backend.search_memories("Python")) == []
    finally:
        await runner.drain(timeout=0.5)


@pytest.mark.asyncio
async def test_consolidation_disabled_does_not_bind_trigger(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=False)
    try:
        from bos.core.contract import SessionEvent

        await runner._bus.emit(
            SessionEvent(
                kind="session_close",
                chat_id="c1",
                actor_name="alice",
                base_revision=1,
                payload={},
            )
        )
        await runner.drain(timeout=0.2)
        assert (await runner.list()) == []
    finally:
        await runner.drain(timeout=0.0)


@pytest.mark.asyncio
async def test_consolidation_enabled_binds_session_close(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True)
    try:
        from bos.core.contract import Message, SessionEvent

        # Bind the agent so the factory finds a bundle
        h.bind({**h._cfg, "agent_name": "alice"})
        await h._services.chat_store.commit_turn(
            "c1",
            [Message(llm_message={"role": "user", "content": "hi"})],
            turn_id="t1",
        )
        await runner._bus.emit(
            SessionEvent(
                kind="session_close",
                chat_id="c1",
                actor_name="alice",
                base_revision=1,
                payload={},
            )
        )
        await runner.drain(timeout=1.0)
        recs = await runner.list()
        assert len(recs) == 1
        assert recs[0].status == "succeeded"
    finally:
        await runner.drain(timeout=0.0)


@pytest.mark.asyncio
async def test_consolidation_enabled_binds_idle(tmp_path):
    import asyncio

    # Tiny idle window so the per-chat timer fires within the test.
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True, idle_after=0.05)
    try:
        from bos.core.contract import Message, SessionEvent

        h.bind({**h._cfg, "agent_name": "alice"})
        await h._services.chat_store.commit_turn(
            "c1",
            [Message(llm_message={"role": "user", "content": "hi"})],
            turn_id="t1",
        )
        # A completed turn arms the per-chat idle timer; no further turns means
        # it fires after idle_after and spawns an idle-triggered consolidation.
        await runner._bus.emit(
            SessionEvent(
                kind="turn_complete",
                chat_id="c1",
                actor_name="alice",
                base_revision=1,
                payload={},
            )
        )
        await asyncio.sleep(0.15)  # let the idle timer fire and submit
        await runner.drain(timeout=1.0)
        recs = await runner.list()
        assert len(recs) == 1
        assert recs[0].status == "succeeded"
        # The job carries the real trigger, not session_close.
        assert recs[0].key.endswith(":idle")
    finally:
        await runner.drain(timeout=0.0)


@pytest.mark.asyncio
async def test_factory_returns_none_for_unbound_actor(tmp_path):
    """An event for an actor we never bound is dropped (no bundle to dispatch against)."""
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True)
    try:
        from bos.core.contract import SessionEvent

        await runner._bus.emit(
            SessionEvent(
                kind="session_close",
                chat_id="c1",
                actor_name="ghost-actor",
                base_revision=1,
                payload={},
            )
        )
        await runner.drain(timeout=0.5)
        assert (await runner.list()) == []
    finally:
        await runner.drain(timeout=0.0)


@pytest.mark.asyncio
async def test_run_consolidation_now_returns_audit_records(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True)
    try:
        from bos.core.contract import Message

        await h._services.chat_store.commit_turn(
            "c1",
            [Message(llm_message={"role": "user", "content": "I prefer dark mode"})],
            turn_id="t1",
        )
        records = await h.run_consolidation_now("c1", agent_name="alice")
        assert records == []
    finally:
        await runner.drain(timeout=0.0)
