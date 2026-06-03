import asyncio
from typing import Any

import pytest

from bos.config import Workspace
from bos.core import Message
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway import ChannelConversationRef, ChatCoordinator, CoordinatedActor
from bos.gateway.actor_manager import ActorManager
from bos.protocol import Envelope, MessageType


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
    actor = CoordinatedActor(CommitAgent(store), actor_box, chat_coordinator=coordinator)
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


def test_actor_turn_metadata_marks_user_target_and_assistant_actor():
    class LibaiCommitAgent(CommitAgent):
        name = "libai"

    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    actor = CoordinatedActor(LibaiCommitAgent(store), route.bind("agent@libai"), chat_coordinator=coordinator)
    inbound = Envelope(
        sender="channel@demo",
        recipient="agent@libai",
        content="hello",
        chat_id="chat-1",
        metadata={
            "target_actor": "libai",
            "target_display": "Li Bai",
            "channel": {"channel_id": "demo", "channel_conversation_id": "default"},
        },
    )

    metadata = actor._turn_metadata("channel@demo", inbound)

    assert metadata["user_message_metadata"] == {"target_actor": "libai", "target_display": "Li Bai"}
    assert metadata["assistant_message_metadata"] == {
        "actor": "libai",
        "actor_address": "agent@libai",
        "actor_display": "Li Bai",
    }


@pytest.mark.asyncio
async def test_coordinated_actor_rejects_missing_channel_metadata():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    route = InMemMailRoute()
    actor_box = route.bind("agent@main")
    client_box = route.bind("client@direct")
    actor = CoordinatedActor(CommitAgent(store), actor_box, chat_coordinator=coordinator)
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

    async def create_agent(self, kind: str | None = None, agent_cfg: dict[str, Any] | None = None):
        return object()


@pytest.mark.asyncio
async def test_actor_manager_clears_active_turns_on_actor_task_failure(monkeypatch):
    class ExplodingActor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            raise RuntimeError("boom")

    import bos.gateway.actor_manager as actor_manager_module

    monkeypatch.setattr(actor_manager_module, "CoordinatedActor", ExplodingActor)
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    ref = ChannelConversationRef("demo", "default")
    await coordinator.begin_turn(chat_id="chat-1", ref=ref, actor="main", turn_id="turn-1", base_revision=0)
    ws = Workspace(
        ".",
        ".bos",
        {
            "runtime": {
                "default_actor": "main",
                "actors": {"main": {"agent": "main", "restart_on_error": False, "max_restarts": 0}},
            }
        },
    )
    notifications = 0

    async def state_changed() -> None:
        nonlocal notifications
        notifications += 1

    manager = ActorManager(
        workspace=ws,
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
