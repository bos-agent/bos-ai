"""End-to-end BEP 10 off-turn consolidation:
commit turns → emit session_close → consolidator proposes ADD → operation
service applies → fact is queryable in a fresh harness."""

import pytest

import bos.exts  # noqa: F401


class _CannedAgentRunner:
    """AgentRunner stand-in returning a pre-canned validated structured payload —
    stands in for the disposable consolidation agent (BEP 12)."""

    def __init__(self, payload):
        self._payload = payload

    async def run(self, message, *, kind=None, agent_cfg=None, schema=None, parent=None, model=None):
        from bos.core import AgentResult

        return AgentResult(output=self._payload, structured=True)


@pytest.mark.asyncio
async def test_mid_chat_fact_persists_into_next_session(tmp_path):
    from bos.core.contract import Message, PluginServices, SessionEvent
    from bos.core.defaults.eventbus import DefaultEventBus
    from bos.core.defaults.job_runner import InProcJobRunner
    from bos.extensions.chat_stores.in_memory import InMemChatStore
    from bos.plugins.memory.plugin import MemoryHarnessPlugin

    bus = DefaultEventBus()
    runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
    await runner.start()
    chat_store = InMemChatStore()
    canned = _CannedAgentRunner({
        "operations": [
            {
                "op": "ADD",
                "reason": "stable user preference",
                "content": "user prefers dark mode",
                "importance": 8,
                "source_turn_ids": ["t1"],
            },
        ]
    })

    services = PluginServices(
        bos_dir=tmp_path,
        workspace=tmp_path,
        llm=None,
        consolidator=None,
        chat_store=chat_store,
        events=bus,
        jobs=runner,
        agent_runner=canned,
    )

    plugin = MemoryHarnessPlugin()
    plugin._cfg = {
        **plugin.default_config(),
        "backend": "in_memory",
        "consolidation": {"enabled": True, "retention_days": 30},
    }
    await plugin.setup(services)

    try:
        # Pre-bind the agent so the plugin has its bundle ready when session_close fires.
        plugin.bind({**plugin._cfg, "agent_name": "alice"})

        await chat_store.commit_turn(
            "c1",
            [
                Message(llm_message={"role": "user", "content": "I always prefer dark mode"}),
            ],
            turn_id="t1",
        )
        head = await chat_store.get_revision("c1")
        await bus.emit(
            SessionEvent(
                kind="session_close",
                chat_id="c1",
                actor_name="alice",
                base_revision=head,
                payload={},
            )
        )
        await runner.drain(timeout=2.0)

        bundle = plugin._for("alice")
        hits = await bundle.backend.search_memories("dark mode")
        assert hits and hits[0].content == "user prefers dark mode"
        assert await bundle.watermarks.get("c1") == head
    finally:
        await runner.drain(timeout=0.0)
