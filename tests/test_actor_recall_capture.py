"""End-to-end gateway-layer behavior for memory.recalled events.

The base AgentActor stays generic — it knows nothing about memory.recalled.
CoordinatedActor (gateway) extends _build_host_sink to register a handler
that buffers recalled ids per turn, then emits the turn_complete LifecycleEvent
with those ids. This file verifies:

  * AgentActor still forwards client-facing events to the mailbox.
  * AgentActor does NOT leak memory.recalled to client mailbox.
  * CoordinatedActor's turn_complete LifecycleEvent carries this turn's
    recalled ids and only this turn's, even when two chats overlap on the
    same Agent (the cross-chat leak regression).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from conftest import InMemMailRoute, InMemMemoryExtension
from test_event_sink import create_test_agent

from bos.core import AgentActor, LLMResponse, ep_provider
from bos.core.contract import LifecycleEvent
from bos.core.defaults.lifecycle import DefaultLifecycleBus
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import ChatCoordinator, CoordinatedActor
from bos.plugins.memory.plugin import MemoryAgentPlugin
from bos.protocol import MessageType


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
async def test_coordinated_actor_emits_recalled_ids_in_turn_complete(stub_provider):
    backend = InMemMemoryExtension()
    eid = await backend.ingest_memory("user prefers PostgreSQL 16", tags=["db"])

    chat_store = InMemChatStore()
    coordinator = ChatCoordinator(chat_store)
    bus = DefaultLifecycleBus()
    events: list[LifecycleEvent] = []
    bus.subscribe("turn_complete", lambda e: events.append(e) or asyncio.sleep(0))

    route = InMemMailRoute()
    actor_addr = f"agent@coord-{stub_provider}"
    sender_addr = f"channel@coord-{stub_provider}"
    actor_mb = route.bind(actor_addr)
    sender_mb = route.bind(sender_addr)

    plugin = MemoryAgentPlugin(backend, {"user"}, auto_recall=True, top_k=1)
    agent = create_test_agent(
        model=f"{stub_provider}/coord", agent_name="main", plugins=[plugin], chat_store=chat_store
    )
    actor = CoordinatedActor(agent, actor_mb, chat_coordinator=coordinator, lifecycle_bus=bus)

    task = asyncio.create_task(actor.run())
    try:
        await _send_with_coordinated_metadata(sender_mb, actor_addr, "postgresql?", chat_id="chat-coord")
        # Wait for the final response envelope
        for _ in range(20):
            env = await asyncio.wait_for(sender_mb.receive(), timeout=2)
            if env.content_type == MessageType.MESSAGE:
                break
        # Give the bus a tick to deliver subscribers (DefaultLifecycleBus may dispatch async)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    completed = [e for e in events if e.kind == "turn_complete"]
    assert completed, "no turn_complete event emitted"
    assert completed[-1].payload.get("recalled") == [eid]


@pytest.mark.asyncio
async def test_two_concurrent_chats_do_not_leak_recalled_ids_across_each_other(stub_provider):
    """Regression: one Agent serving two chats concurrently. Each chat's
    turn_complete must carry only its own recalled ids. Previously the agent
    held _current_context as an instance attribute; whichever ask() ran last
    clobbered it, so chat A's turn_complete could carry chat B's recalled ids
    and the recall flush touched the wrong memories' last_used.
    """

    @ep_provider(name=f"{stub_provider}_slow")
    async def _slow(messages, model=None, **kwargs):
        await asyncio.sleep(0.05)
        return LLMResponse(content="reply")

    try:
        backend = InMemMemoryExtension()
        eid_pg = await backend.ingest_memory("Alice loves PostgreSQL", tags=["db"])
        eid_rs = await backend.ingest_memory("Bob writes Rust daily", tags=["lang"])

        chat_store = InMemChatStore()
        coordinator = ChatCoordinator(chat_store)
        bus = DefaultLifecycleBus()
        events: list[LifecycleEvent] = []

        async def _collect(event: LifecycleEvent) -> None:
            events.append(event)

        bus.subscribe("turn_complete", _collect)

        route = InMemMailRoute()
        actor_addr = f"agent@two-{stub_provider}"
        sender_addr = f"channel@two-{stub_provider}"
        actor_mb = route.bind(actor_addr)
        sender_mb = route.bind(sender_addr)

        plugin = MemoryAgentPlugin(backend, {"user"}, auto_recall=True, top_k=1)
        agent = create_test_agent(
            model=f"{stub_provider}_slow/two", agent_name="main", plugins=[plugin], chat_store=chat_store
        )
        actor = CoordinatedActor(agent, actor_mb, chat_coordinator=coordinator, lifecycle_bus=bus)

        task = asyncio.create_task(actor.run())
        try:
            await _send_with_coordinated_metadata(sender_mb, actor_addr, "postgresql?", chat_id="chat-A")
            await _send_with_coordinated_metadata(sender_mb, actor_addr, "rust?", chat_id="chat-B")
            replies = 0
            for _ in range(60):
                env = await asyncio.wait_for(sender_mb.receive(), timeout=3)
                if env.content_type == MessageType.MESSAGE:
                    replies += 1
                    if replies == 2:
                        break
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        by_chat = {e.chat_id: e.payload.get("recalled", []) for e in events if e.kind == "turn_complete"}
        assert by_chat == {"chat-A": [eid_pg], "chat-B": [eid_rs]}, by_chat
    finally:
        ep_provider._extensions.pop(f"{stub_provider}_slow", None)
