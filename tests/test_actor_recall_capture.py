"""Recall capture/flush after the dependency inversion.

The base AgentActor stays generic — it knows nothing about memory.recalled and
emits a payload-free turn_complete LifecycleEvent on its bus. The memory plugin
owns the recall ids end to end: its auto-recall interceptor records the surfaced
ids into the per-agent bundle keyed by turn_id, and its turn_complete subscriber
drains that buffer to flush last_used. The gateway is no longer involved.

This file verifies:

  * AgentActor still forwards client-facing events but does NOT leak
    memory.recalled to the client mailbox.
  * AgentActor emits a generic turn_complete (chat_id + turn_id, no
    memory-specific payload).
  * The plugin records recalled ids per turn and the turn_complete flush touches
    only that turn's ids — even for two chats interleaved on one agent.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from conftest import InMemMailRoute, InMemMemoryExtension
from test_event_sink import create_test_agent

from bos.core import LLMResponse, ep_provider
from bos.core.actor import MessageType
from bos.core.contract import LifecycleEvent, Message, PluginServices
from bos.core.defaults.lifecycle import DefaultLifecycleBus
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import AgentActor, ChatCoordinator
from bos.plugins.memory.plugin import MemoryAgentPlugin, MemoryHarnessPlugin


@pytest.fixture
def provider_name():
    return f"test_actor_recall_{uuid.uuid4().hex}"


@pytest.fixture
def stub_provider(provider_name):
    @ep_provider(name=provider_name)
    async def _p(messages, model=None, **kwargs):
        return LLMResponse(content="reply")

    yield provider_name
    ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_base_actor_does_not_forward_memory_recalled_to_client(stub_provider):
    """Sanity: the base sink only registers client-facing event types.
    memory.recalled is internal — no handler in base AgentActor."""
    backend = InMemMemoryExtension()
    await backend.ingest_memory("user prefers PostgreSQL 16", tags=["db"])

    route = InMemMailRoute()
    actor_addr = f"agent@base-{stub_provider}"
    sender_addr = f"channel@base-{stub_provider}"
    actor_mb = route.bind(actor_addr)
    sender_mb = route.bind(sender_addr)

    plugin = MemoryAgentPlugin(backend, {"user"}, auto_recall=True, top_k=1)
    agent = create_test_agent(model=f"{stub_provider}/base", agent_name="main", plugins=[plugin])
    actor = AgentActor(agent, actor_mb)

    task = asyncio.create_task(actor.run())
    try:
        await sender_mb.send(actor_addr, "postgresql?", chat_id="chat-base")
        received = []
        for _ in range(8):
            env = await asyncio.wait_for(sender_mb.receive(), timeout=1)
            received.append(env)
            if env.content_type == MessageType.MESSAGE:
                break
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    turn_event_types = {
        json.loads(env.content)["event_type"] for env in received if env.content_type == MessageType.TURN_EVENT
    }
    assert {"turn", "llm", "response"}.issubset(turn_event_types)
    assert "memory.recalled" not in turn_event_types


async def _send_with_coordinated_metadata(client_mb, actor_addr: str, content: str, *, chat_id: str) -> None:
    await client_mb.send(
        actor_addr,
        content,
        chat_id=chat_id,
        metadata={
            "base_revision": 0,
            "channel": {"channel_id": "demo", "channel_conversation_id": chat_id},
        },
    )


@pytest.mark.asyncio
async def test_coordinated_actor_emits_generic_turn_complete(stub_provider):
    """turn_complete is a platform event now: it carries chat_id + turn_id and
    no memory-specific payload. The gateway names no plugin."""
    backend = InMemMemoryExtension()
    await backend.ingest_memory("user prefers PostgreSQL 16", tags=["db"])

    chat_store = InMemChatStore()
    coordinator = ChatCoordinator(chat_store)
    bus = DefaultLifecycleBus()
    events: list[LifecycleEvent] = []

    async def _collect(e: LifecycleEvent) -> None:
        events.append(e)

    bus.subscribe(LifecycleEvent, _collect)

    route = InMemMailRoute()
    actor_addr = f"agent@coord-{stub_provider}"
    sender_addr = f"channel@coord-{stub_provider}"
    actor_mb = route.bind(actor_addr)
    sender_mb = route.bind(sender_addr)

    plugin = MemoryAgentPlugin(backend, {"user"}, auto_recall=True, top_k=1)
    agent = create_test_agent(
        model=f"{stub_provider}/coord", agent_name="main", plugins=[plugin], chat_store=chat_store
    )
    actor = AgentActor(agent, actor_mb, chat_coordinator=coordinator, event_bus=bus)

    task = asyncio.create_task(actor.run())
    try:
        await _send_with_coordinated_metadata(sender_mb, actor_addr, "postgresql?", chat_id="chat-coord")
        for _ in range(20):
            env = await asyncio.wait_for(sender_mb.receive(), timeout=2)
            if env.content_type == MessageType.MESSAGE:
                break
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    completed = [e for e in events if e.kind == "turn_complete"]
    assert completed, "no turn_complete event emitted"
    last = completed[-1]
    assert last.chat_id == "chat-coord"
    assert last.turn_id
    assert "recalled" not in last.payload


async def _make_harness_plugin(tmp_path, bus) -> MemoryHarnessPlugin:
    services = PluginServices(
        bos_dir=tmp_path,
        workspace=tmp_path,
        llm=None,
        consolidator=None,
        subagents=None,
        chat_store=InMemChatStore(),
        events=bus,
        jobs=None,
        background_llm=None,
    )
    plugin = MemoryHarnessPlugin()
    plugin._cfg = {**plugin.default_config(), "backend": "in_memory"}
    await plugin.setup(services)
    return plugin


def _query_ctx(chat_id: str, turn_id: str, query: str):
    from bos.core.agent import TurnContext

    ctx = TurnContext(agent_name="main", chat_id=chat_id, turn_id=turn_id, event_sink=None)
    ctx.current = [Message(llm_message={"role": "user", "content": query})]
    return ctx


@pytest.mark.asyncio
async def test_plugin_records_recalled_per_turn_and_flush_touches_only_that_turn(tmp_path):
    """The interceptor records surfaced ids into the bundle keyed by turn_id;
    the turn_complete flush drains exactly that turn's ids. Two chats interleaved
    on one agent must not cross-contaminate (the prior _current_context leak)."""
    import bos.exts  # noqa: F401  — registers the in_memory backend

    bus = DefaultLifecycleBus()
    plugin = await _make_harness_plugin(tmp_path, bus)

    agent_plugin = plugin.bind({**plugin._cfg, "agent_name": "main"})
    bundle = plugin._for("main")
    eid_pg = await bundle.backend.ingest_memory("Alice loves PostgreSQL", tags=["db"])
    eid_rs = await bundle.backend.ingest_memory("Bob writes Rust daily", tags=["lang"])

    interceptor = agent_plugin.get_interceptors()[0]
    await asyncio.gather(
        interceptor.intercept("prepare", _query_ctx("chat-A", "t-A", "postgresql?")),
        interceptor.intercept("prepare", _query_ctx("chat-B", "t-B", "rust?")),
    )

    # Each turn's recall is isolated by turn_id.
    assert bundle.recalled_by_turn == {"t-A": [eid_pg], "t-B": [eid_rs]}

    # Flushing turn A touches only PostgreSQL and drains only A's buffer.
    await bus.emit(
        LifecycleEvent(kind="turn_complete", chat_id="chat-A", actor_name="main", base_revision=1, turn_id="t-A")
    )
    assert (await bundle.backend.get_memory(eid_pg)).metadata["last_used"] is not None
    assert (await bundle.backend.get_memory(eid_rs)).metadata["last_used"] is None
    assert "t-A" not in bundle.recalled_by_turn and "t-B" in bundle.recalled_by_turn

    # Flushing turn B touches Rust.
    await bus.emit(
        LifecycleEvent(kind="turn_complete", chat_id="chat-B", actor_name="main", base_revision=1, turn_id="t-B")
    )
    assert (await bundle.backend.get_memory(eid_rs)).metadata["last_used"] is not None
    assert bundle.recalled_by_turn == {}
