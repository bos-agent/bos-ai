import asyncio
from typing import Any

import pytest

from bos.config import Workspace
from bos.core import Message
from bos.core.actor import Envelope, MessageType
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway import AgentActor, ChannelConversationRef, ChatCoordinator
from bos.gateway.actors.actor_manager import ActorManager


class CommitAgent:
    name = "main"

    def __init__(self, store: InMemChatStore) -> None:
        self.store = store

    async def ask(self, chat_id, content, *, turn_id, commit_observer=None, **kwargs):
        commit = await self.store.commit_turn(
            chat_id,
            [
                Message(llm_message={"role": "user", "content": content}),
                Message(llm_message={"role": "assistant", "content": "ok"}),
            ],
            turn_id=turn_id,
        )
        if commit_observer is not None:
            commit_observer(commit)
        return "ok"


@pytest.mark.asyncio
async def test_coordinated_actor_begins_and_ends_turn_with_revision_commit():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    actor_box = route.bind("agent@main")
    client_box = route.bind("channel@demo")
    actor = AgentActor(CommitAgent(store), actor_box, chat_coordinator=coordinator)
    task = asyncio.create_task(actor.run())
    try:
        await client_box.send(
            "agent@main",
            "hello",
            chat_id="chat-1",
            metadata={
                "base_revision": 0,
                "channel": {"channel_id": "demo", "channel_conversation_id": "default"},
            },
        )
        response = await asyncio.wait_for(client_box.receive(), timeout=2)

        assert response.content == "ok"
        assert await coordinator.current_revision("chat-1") == 1
        assert coordinator.active_turn_status("chat-1") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class AbortPersistAgent:
    """Blocks until cancelled, then commits abort-safe history like the real agent."""

    name = "main"

    def __init__(self, store: InMemChatStore) -> None:
        self.store = store
        self.started = asyncio.Event()

    async def ask(self, chat_id, content, *, turn_id, commit_observer=None, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            commit = await self.store.commit_turn(
                chat_id,
                [
                    Message(llm_message={"role": "user", "content": content}),
                    Message(llm_message={"role": "assistant", "content": "(turn aborted before completion)"}),
                ],
                turn_id=turn_id,
            )
            if commit_observer is not None:
                commit_observer(commit)
            raise
        return "unreachable"


@pytest.mark.asyncio
async def test_aborted_turn_notifies_channel_so_revision_cursor_resyncs():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    # InMemMailRoute queues are class-level and loop-bound on first use, so use
    # addresses unique to this test to avoid clashing with other tests' loops.
    actor_box = route.bind("agent@abort-main")
    client_box = route.bind("channel@abort-demo")
    agent = AbortPersistAgent(store)
    actor = AgentActor(agent, actor_box, chat_coordinator=coordinator)
    task = asyncio.create_task(actor.run())
    try:
        await client_box.send(
            "agent@abort-main",
            "hello",
            chat_id="chat-abort-1",
            metadata={
                "base_revision": 0,
                "channel": {"channel_id": "demo", "channel_conversation_id": "default"},
            },
        )
        await asyncio.wait_for(agent.started.wait(), timeout=2)
        await client_box.send(
            "agent@abort-main",
            "",
            content_type=MessageType.INTERRUPT_ABORT,
            chat_id="chat-abort-1",
        )

        notice = await asyncio.wait_for(client_box.receive(), timeout=2)
        assert notice.content_type == MessageType.SYSTEM
        assert notice.metadata.get("event") == "turn_aborted"
        # The abort-safe commit advanced the revision and the turn was closed.
        assert await coordinator.current_revision("chat-abort-1") == 1
        assert coordinator.active_turn_status("chat-abort-1") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ErrorPersistAgent:
    """Commits partial history, then fails the turn with an exception."""

    name = "main"

    def __init__(self, store: InMemChatStore) -> None:
        self.store = store

    async def ask(self, chat_id, content, *, turn_id, commit_observer=None, **kwargs):
        commit = await self.store.commit_turn(
            chat_id,
            [Message(llm_message={"role": "user", "content": content})],
            turn_id=turn_id,
        )
        if commit_observer is not None:
            commit_observer(commit)
        raise RuntimeError("provider exploded")


@pytest.mark.asyncio
async def test_errored_turn_notifies_channel_so_revision_cursor_resyncs():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    # Unique addresses: InMemMailRoute queues are class-level and loop-bound.
    actor_box = route.bind("agent@error-main")
    client_box = route.bind("channel@error-demo")
    actor = AgentActor(ErrorPersistAgent(store), actor_box, chat_coordinator=coordinator)
    task = asyncio.create_task(actor.run())
    try:
        await client_box.send(
            "agent@error-main",
            "hello",
            chat_id="chat-error-1",
            metadata={
                "base_revision": 0,
                "channel": {"channel_id": "demo", "channel_conversation_id": "default"},
            },
        )

        notice = await asyncio.wait_for(client_box.receive(), timeout=2)
        assert notice.content_type == MessageType.SYSTEM
        assert notice.metadata.get("event") == "turn_error"
        assert "provider exploded" in notice.content
        # The partial commit advanced the revision and the turn was closed.
        assert await coordinator.current_revision("chat-error-1") == 1
        assert coordinator.active_turn_status("chat-error-1") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_actor_turn_metadata_marks_user_target_and_assistant_actor():
    class LibaiCommitAgent(CommitAgent):
        name = "libai"

    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    actor = AgentActor(LibaiCommitAgent(store), route.bind("agent@libai"), chat_coordinator=coordinator)
    inbound = Envelope(
        sender="channel@demo",
        recipient="agent@libai",
        content="hello",
        chat_id="chat-1",
        metadata={
            "target_actor": "libai",
            "target_display": "Li Bai",
            "workdir": "/home/user/proj",
            "channel": {"channel_id": "demo", "channel_conversation_id": "default"},
        },
    )

    metadata = actor._turn_metadata("channel@demo", inbound)

    assert metadata["user_message_metadata"] == {
        "target_agent": "libai",
        "target_display": "Li Bai",
        "workdir": "/home/user/proj",
    }
    assert metadata["assistant_message_metadata"] == {
        "agent_name": "libai",
        "actor_address": "agent@libai",
        "agent_display": "Li Bai",
    }


def test_actor_turn_metadata_keeps_workdir_without_target():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    actor = AgentActor(CommitAgent(store), route.bind("agent@main"), chat_coordinator=coordinator)
    inbound = Envelope(
        sender="channel@demo",
        recipient="agent@main",
        content="hello",
        chat_id="chat-1",
        metadata={
            "workdir": "/home/user/proj",
            "channel": {"channel_id": "demo", "channel_conversation_id": "default"},
        },
    )

    metadata = actor._turn_metadata("channel@demo", inbound)

    assert metadata["user_message_metadata"] == {"workdir": "/home/user/proj"}


@pytest.mark.asyncio
async def test_coordinated_actor_rejects_missing_channel_metadata():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    actor_box = route.bind("agent@main")
    client_box = route.bind("client@direct")
    actor = AgentActor(CommitAgent(store), actor_box, chat_coordinator=coordinator)
    task = asyncio.create_task(actor.run())
    try:
        await client_box.send("agent@main", "hello", chat_id="chat-1", metadata={"base_revision": 0})
        response = await asyncio.wait_for(client_box.receive(), timeout=2)

        assert response.content_type == MessageType.SYSTEM
        assert "missing channel metadata" in response.content
        assert await coordinator.current_revision("chat-1") == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _FakeHarness:
    def __init__(self) -> None:
        self.mail_route = InMemMailRoute()
        self.create_calls: list[dict[str, Any]] = []

    async def create_agent(self, kind: str | None = None, agent_cfg: dict[str, Any] | None = None):
        self.create_calls.append({"kind": kind, "agent_cfg": dict(agent_cfg or {})})
        return object()


@pytest.mark.asyncio
async def test_actor_manager_enables_history_attribution_only_for_multi_agent_runtime():
    multi_ws = Workspace(
        ".",
        ".bos",
        {
            "runtime": {
                "main_actor": "main",
                "actors": {
                    "main": {"agent": "main"},
                    "libai": {"agent": "poet"},
                },
            }
        },
    )
    multi_harness = _FakeHarness()
    multi_manager = ActorManager(
        actors=multi_ws.resolve_gateway_actors(),
        harness=multi_harness,
        chat_coordinator=ChatCoordinator(InMemChatStore()),
    )

    await multi_manager.start_all()
    await multi_manager.stop_all()

    assert {call["agent_cfg"]["history_attribution"] for call in multi_harness.create_calls} == {True}

    single_ws = Workspace(
        ".",
        ".bos",
        {"runtime": {"main_actor": "main", "actors": {"main": {"agent": "main"}}}},
    )
    single_harness = _FakeHarness()
    single_manager = ActorManager(
        actors=single_ws.resolve_gateway_actors(),
        harness=single_harness,
        chat_coordinator=ChatCoordinator(InMemChatStore()),
    )

    await single_manager.start_all()
    await single_manager.stop_all()

    assert single_harness.create_calls[0]["agent_cfg"]["history_attribution"] is False


@pytest.mark.asyncio
async def test_actor_manager_passes_explicit_actor_agent_cfg_to_harness():
    ws = Workspace(
        ".",
        ".bos",
        {
            "runtime": {
                "main_actor": "main",
                "actors": {
                    "main": {
                        "agent": "main",
                        "agent_cfg": {
                            "tools": {
                                "enabled": ["ReadFile"],
                                "disabled": ["WriteFile"],
                                "usages": {"ReadFile": "Read only what you need."},
                            },
                            "plugin-bindings": {"SubagentPlugin": {"enabled": ["*"]}},
                        },
                    }
                },
            }
        },
    )
    harness = _FakeHarness()
    manager = ActorManager(
        actors=ws.resolve_gateway_actors(),
        harness=harness,
        chat_coordinator=ChatCoordinator(InMemChatStore()),
    )

    await manager.start_all()
    await manager.stop_all()

    assert harness.create_calls == [
        {
            "kind": "main",
            "agent_cfg": {
                "tools": ["ReadFile"],
                "exclude_tools": ["WriteFile"],
                "tools_usage": {"ReadFile": "Read only what you need."},
                "plugin-bindings": {"SubagentPlugin": {"enabled": ["*"]}},
                "agent_name": "main",
                "history_attribution": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_actor_manager_clears_active_turns_on_actor_task_failure(monkeypatch):
    class ExplodingActor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            raise RuntimeError("boom")

    import bos.gateway.actors.actor_manager as actor_manager_module

    monkeypatch.setattr(actor_manager_module, "AgentActor", ExplodingActor)
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    ref = ChannelConversationRef("demo", "default")
    await coordinator.begin_turn(chat_id="chat-1", ref=ref, actor="main", turn_id="turn-1", base_revision=0)
    ws = Workspace(
        ".",
        ".bos",
        {
            "runtime": {
                "main_actor": "main",
                "actors": {"main": {"agent": "main", "restart_on_error": False, "max_restarts": 0}},
            }
        },
    )
    notifications = 0

    async def state_changed() -> None:
        nonlocal notifications
        notifications += 1

    manager = ActorManager(
        actors=ws.resolve_gateway_actors(),
        harness=_FakeHarness(),
        chat_coordinator=coordinator,
        state_changed=state_changed,
    )

    await manager.start_all()
    await asyncio.sleep(0.05)

    assert coordinator.active_turn_status("chat-1") is None
    assert manager.status_payload()["main"]["status"] == "error"
    assert "boom" in manager.status_payload()["main"]["error"]
    assert notifications >= 2
