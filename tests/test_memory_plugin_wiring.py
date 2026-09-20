"""MemoryHarnessPlugin wiring tests — lazy per-agent registry, turn_complete
recall flush dispatching by event.actor_name, isolation between agents."""

import pytest

import bos.exts  # noqa: F401 — registers default extensions
from bos.core.contract import PluginServices
from bos.core.defaults.eventbus import DefaultEventBus
from bos.plugins.memory.operation_service import DefaultMemoryOperationService
from bos.plugins.memory.plugin import MemoryHarnessPlugin


async def _setup_plugin(tmp_path):
    from bos.extensions.chat_stores.in_memory import InMemChatStore

    class _StubLLM:
        async def complete(self, messages, **kwargs):
            from bos.core.agent import LLMResponse

            return LLMResponse(content='{"operations": []}')

    class _StubAgentRunner:
        async def run(self, message, *, kind=None, agent_cfg=None, schema=None, parent=None, model=None):
            from bos.core import AgentResult

            return AgentResult(output={"operations": []}, structured=True)

    svc = PluginServices(
        bos_dir=tmp_path,
        workspace=tmp_path,
        llm=_StubLLM(),
        consolidator=None,
        chat_store=InMemChatStore(),
        events=DefaultEventBus(),
        agent_runner=_StubAgentRunner(),
    )
    h = MemoryHarnessPlugin()
    h._cfg = {**h.default_config(), "backend": "in_memory"}
    await h.setup(svc)
    return h


@pytest.mark.asyncio
async def test_setup_does_not_build_per_agent_eagerly(tmp_path):
    h = await _setup_plugin(tmp_path)
    # No agent bound yet → no per-agent state
    assert h._per_agent == {}
    # After explicit bind for "alice", the bundle exists and is the right shape
    h.bind({**h._cfg, "agent_name": "alice"})
    bundle = h._for("alice")
    assert bundle.backend is not None
    assert isinstance(bundle.op_service, DefaultMemoryOperationService)
    assert bundle.watermarks is not None
    assert bundle.consolidator is not None


@pytest.mark.asyncio
async def test_consolidation_model_config_reaches_consolidator(tmp_path):
    h = await _setup_plugin(tmp_path)
    h._cfg = {**h._cfg, "consolidation": {"model": "cfg/model"}}
    await h.setup(h._services)
    h.bind({**h._cfg, "agent_name": "alice"})
    bundle = h._for("alice")
    assert bundle.consolidator is not None
    assert bundle.consolidator._model == "cfg/model"


@pytest.mark.asyncio
async def test_validate_config_rejects_scope_key(tmp_path):
    h = await _setup_plugin(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        h.validate_config({"scope": "alice"})


@pytest.mark.asyncio
async def test_bind_requires_agent_name(tmp_path):
    h = await _setup_plugin(tmp_path)
    with pytest.raises(ValueError, match="agent_name"):
        h.bind({**h._cfg})  # missing agent_name


@pytest.mark.asyncio
async def test_two_agents_get_isolated_bundles(tmp_path):
    h = await _setup_plugin(tmp_path)
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


@pytest.mark.asyncio
async def test_turn_complete_for_unbound_actor_is_dropped(tmp_path):
    """An event for an actor we never bound has no bundle to flush against; the
    subscriber must drop it rather than raise into the bus."""
    from bos.core.contract import SessionEvent

    h = await _setup_plugin(tmp_path)
    await h._services.events.emit(
        SessionEvent(
            kind="turn_complete",
            chat_id="c1",
            actor_name="ghost-actor",
            base_revision=1,
            turn_id="t1",
            payload={},
        )
    )
    assert h._per_agent == {}


@pytest.mark.asyncio
async def test_run_consolidation_now_returns_audit_records(tmp_path):
    from bos.core.contract import Message

    h = await _setup_plugin(tmp_path)
    await h._services.chat_store.commit_turn(
        "c1",
        [Message(llm_message={"role": "user", "content": "I prefer dark mode"})],
        turn_id="t1",
    )
    records = await h.run_consolidation_now("c1", agent_name="alice")
    assert records == []
