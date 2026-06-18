"""End-to-end BEP 10 off-turn consolidation:
commit turns → emit session_close → consolidator proposes ADD → operation
service applies (auto_apply=True) → fact is queryable in a fresh harness."""

import json

import pytest

import bos.exts  # noqa: F401


class _CannedLLM:
    """LLMClient stand-in that returns a pre-canned JSON payload for any complete()."""

    def __init__(self, payload):
        self._payload = payload

    async def complete(self, messages, **kwargs):
        from bos.core.llm import LLMResponse

        return LLMResponse(content=json.dumps(self._payload))


@pytest.mark.asyncio
async def test_mid_chat_fact_persists_into_next_session(tmp_path):
    from bos.core.contract import LifecycleEvent, Message, PluginServices
    from bos.core.defaults.background_llm import DefaultBackgroundLLM
    from bos.core.defaults.jobs import InProcJobRunner
    from bos.core.defaults.lifecycle import DefaultLifecycleBus
    from bos.extensions.chat_stores.in_memory import InMemChatStore
    from bos.plugins.memory.plugin import MemoryHarnessPlugin

    bus = DefaultLifecycleBus()
    runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
    await runner.start()
    chat_store = InMemChatStore()
    canned = _CannedLLM({
        "operations": [
            {"op": "ADD", "reason": "stable user preference", "content": "user prefers dark mode", "importance": 8},
        ]
    })
    blm = DefaultBackgroundLLM(canned)

    services = PluginServices(
        bos_dir=tmp_path,
        workspace=tmp_path,
        llm=canned,
        consolidator=None,
        subagents=None,
        chat_store=chat_store,
        events=bus,
        jobs=runner,
        background_llm=blm,
    )

    plugin = MemoryHarnessPlugin()
    plugin._cfg = {
        **plugin.default_config(),
        "backend": "in_memory",
        "consolidation": {"enabled": True, "retention_days": 30, "auto_apply": True},
    }
    await plugin.setup(services)

    try:
        await chat_store.commit_turn(
            "c1",
            [
                Message(llm_message={"role": "user", "content": "I always prefer dark mode"}),
            ],
            turn_id="t1",
        )
        head = await chat_store.get_revision("c1")
        await bus.emit(
            LifecycleEvent(
                kind="session_close",
                chat_id="c1",
                actor_name=None,
                base_revision=head,
                payload={},
            )
        )
        await runner.drain(timeout=2.0)

        hits = await plugin._backend.search_memories("dark mode")
        assert hits and hits[0].content == "user prefers dark mode"
        assert await plugin._watermarks.get("workspace", "c1") == head
    finally:
        await runner.drain(timeout=0.0)
