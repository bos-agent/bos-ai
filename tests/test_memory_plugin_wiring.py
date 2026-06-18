"""MemoryHarnessPlugin wiring tests — setup constructs op-service / watermarks
/ consolidator and binds session_close trigger when consolidation is enabled."""

import pytest

import bos.exts  # noqa: F401 — registers default extensions
from bos.core.contract import PluginServices
from bos.core.defaults.background_llm import DefaultBackgroundLLM
from bos.core.defaults.jobs import InProcJobRunner
from bos.core.defaults.lifecycle import DefaultLifecycleBus
from bos.plugins.memory.operation_service import DefaultMemoryOperationService
from bos.plugins.memory.plugin import MemoryHarnessPlugin


async def _setup_plugin(tmp_path, *, consolidation_enabled=False):
    bus = DefaultLifecycleBus()
    runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
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
    cfg = h.default_config()
    if consolidation_enabled:
        cfg = {**cfg, "consolidation": {"enabled": True, "retention_days": 30, "auto_apply": False}}
    h._cfg = cfg
    await h.setup(svc)
    return h, runner


@pytest.mark.asyncio
async def test_setup_constructs_operation_service_and_watermarks(tmp_path):
    h, runner = await _setup_plugin(tmp_path)
    try:
        assert h._backend is not None
        assert isinstance(h._operation_service, DefaultMemoryOperationService)
        assert h._watermarks is not None
        assert h._consolidator is not None
    finally:
        await runner.drain(timeout=0.5)


@pytest.mark.asyncio
async def test_consolidation_disabled_does_not_bind_trigger(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=False)
    try:
        from bos.core.contract import LifecycleEvent

        await runner._bus.emit(
            LifecycleEvent(
                kind="session_close",
                chat_id="c1",
                actor_name=None,
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
        from bos.core.contract import LifecycleEvent, Message

        await h._services.chat_store.commit_turn(
            "c1",
            [Message(llm_message={"role": "user", "content": "hi"})],
            turn_id="t1",
        )
        await runner._bus.emit(
            LifecycleEvent(
                kind="session_close",
                chat_id="c1",
                actor_name="A",
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
async def test_run_consolidation_now_returns_audit_records(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True)
    try:
        from bos.core.contract import Message

        await h._services.chat_store.commit_turn(
            "c1",
            [Message(llm_message={"role": "user", "content": "I prefer dark mode"})],
            turn_id="t1",
        )
        records = await h.run_consolidation_now("c1", dry_run=True)
        assert records == []
    finally:
        await runner.drain(timeout=0.0)
