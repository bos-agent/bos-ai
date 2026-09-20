"""End-to-end BEP 10 off-turn consolidation:
commit turns → run consolidation → consolidator proposes ADD → operation
service applies → fact is queryable and the watermark has advanced."""

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
    from bos.core.contract import Message, PluginServices
    from bos.core.defaults.eventbus import DefaultEventBus
    from bos.extensions.chat_stores.in_memory import InMemChatStore
    from bos.plugins.memory.plugin import MemoryHarnessPlugin

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
        events=DefaultEventBus(),
        agent_runner=canned,
    )

    plugin = MemoryHarnessPlugin()
    plugin._cfg = {**plugin.default_config(), "backend": "in_memory"}
    await plugin.setup(services)

    await chat_store.commit_turn(
        "c1",
        [
            Message(llm_message={"role": "user", "content": "I always prefer dark mode"}),
        ],
        turn_id="t1",
    )
    head = await chat_store.get_revision("c1")

    records = await plugin.run_consolidation_now("c1", agent_name="alice")
    assert [r.op.op for r in records] == ["ADD"]

    bundle = plugin._for("alice")
    hits = await bundle.backend.search_memories("dark mode")
    assert hits and hits[0].content == "user prefers dark mode"
    assert await bundle.watermarks.get("c1") == head


@pytest.mark.asyncio
async def test_second_run_is_a_no_op_once_the_watermark_caught_up(tmp_path):
    """The watermark, not a queue, is what stops the same window being
    consolidated twice — so a repeat run proposes nothing."""
    from bos.core.contract import Message, PluginServices
    from bos.core.defaults.eventbus import DefaultEventBus
    from bos.extensions.chat_stores.in_memory import InMemChatStore
    from bos.plugins.memory.plugin import MemoryHarnessPlugin

    chat_store = InMemChatStore()
    calls: list[str] = []

    class _CountingRunner(_CannedAgentRunner):
        async def run(self, message, *args, **kwargs):
            calls.append(message)
            return await super().run(message, *args, **kwargs)

    services = PluginServices(
        bos_dir=tmp_path,
        workspace=tmp_path,
        llm=None,
        consolidator=None,
        chat_store=chat_store,
        events=DefaultEventBus(),
        agent_runner=_CountingRunner({
            "operations": [
                {"op": "ADD", "reason": "r", "content": "fact", "source_turn_ids": ["t1"]},
            ]
        }),
    )
    plugin = MemoryHarnessPlugin()
    plugin._cfg = {**plugin.default_config(), "backend": "in_memory"}
    await plugin.setup(services)

    await chat_store.commit_turn("c1", [Message(llm_message={"role": "user", "content": "hi"})], turn_id="t1")

    assert await plugin.run_consolidation_now("c1", agent_name="alice")
    assert len(calls) == 1
    assert await plugin.run_consolidation_now("c1", agent_name="alice") == []
    assert len(calls) == 1, "no new turns past the watermark — the consolidator must not be called again"
