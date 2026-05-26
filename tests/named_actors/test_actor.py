import asyncio
import uuid
from datetime import datetime

import pytest
from conftest import InMemMailRoute

from bos.named_actors.actor import NamedActor
from bos.protocol import Envelope, MessageType


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def ask(self, chat_id, message, **kwargs):
        self.calls.append((chat_id, message, kwargs))
        return "done"


class BlockingFakeAgent:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def ask(self, chat_id, message, **kwargs):
        self.calls.append((chat_id, message, kwargs))
        self.started.set()
        await self.finish.wait()
        return "done"


class FakeMailBox:
    def __init__(self):
        self.address = "agent@bob"
        self.sent: list = []

    async def send(self, recipient, content, **kwargs):
        self.sent.append((recipient, content, kwargs))

    async def receive_nowait(self):
        return None


def test_named_actor_builds_turn_metadata():
    agent = FakeAgent()
    actor = NamedActor(agent, FakeMailBox(), actor_name="bob", display_name="Bob", agent_kind="architect")
    env = Envelope(
        sender="channel@http",
        recipient="agent@bob",
        content="review this",
        content_type=MessageType.MESSAGE,
        chat_id="abc123",
        timestamp=datetime.now(),
        metadata={"target_actor": "bob", "target_display": "Bob (architect)"},
    )

    metadata = actor._turn_metadata("channel@http", env)

    assert metadata["actor_name"] == "bob"
    assert metadata["actor_address"] == "agent@bob"
    assert metadata["actor_display"] == "Bob (architect)"
    assert metadata["user_message_metadata"]["to_actor"] == "bob"
    assert metadata["user_message_metadata"]["to_display"] == "Bob (architect)"
    assert metadata["assistant_message_metadata"]["from_actor"] == "bob"


@pytest.mark.asyncio
async def test_named_actor_rejects_message_during_active_turn_with_busy():
    agent = BlockingFakeAgent()
    route = InMemMailRoute()
    actor_address = f"agent@bob-{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    actor = NamedActor(agent, actor_mailbox, actor_name="bob", display_name="Bob", agent_kind="architect")
    chat_id = "busy-chat"

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(
            actor_address,
            "first",
            chat_id=chat_id,
            metadata={"target_actor": "bob", "target_display": "Bob (architect)"},
        )
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        await sender_mailbox.send(actor_address, "second", chat_id=chat_id)

        rejection = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert rejection.content == "(busy: a response is already in progress for this chat)"
        assert rejection.content_type == MessageType.SYSTEM
        assert rejection.chat_id == chat_id
        assert len(agent.calls) == 1
        assert agent.calls[0][1] == "first"
        assert agent.calls[0][2]["ctx_metadata"]["target_actor"] == "bob"

        agent.finish.set()
        reply = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert reply.content == "done"
        assert reply.metadata["from_actor"] == "bob"
    finally:
        agent.finish.set()
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_named_actor_accepts_message_after_turn_completes():
    agent = BlockingFakeAgent()
    route = InMemMailRoute()
    actor_address = f"agent@bob-{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    actor = NamedActor(agent, actor_mailbox, actor_name="bob", display_name="Bob", agent_kind="architect")
    chat_id = "sequential-chat"

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "first", chat_id=chat_id)
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        agent.finish.set()
        first_reply = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert first_reply.content == "done"

        agent.started.clear()
        agent.finish.clear()

        await sender_mailbox.send(actor_address, "second", chat_id=chat_id)
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        agent.finish.set()
        second_reply = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert second_reply.content == "done"
        assert [call[1] for call in agent.calls] == ["first", "second"]
    finally:
        agent.finish.set()
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_react_agent_persists_named_actor_message_metadata():
    from bos.core import ReActAgent

    class Store:
        def __init__(self):
            self.messages = []

        async def save_turn(self, chat_id, messages, *, turn_id=None):
            self.messages.extend(messages)

        async def get_context(self, chat_id, *, tokenizer_model=None, filter_mode=None):
            from bos.core.contract import ContextResult
            return ContextResult(
                messages=[],
                source_messages=[],
                estimated_tokens=0,
                tokenizer_model=tokenizer_model,
                estimation_source="fallback",
                filter_mode="keep_signatures",
                summary_applied=False,
                summary_message_count_excluded=0,
            )

        async def get_compaction_messages(self, chat_id, *, filter_mode=None):
            return []

        async def estimate_tokens(self, chat_id, *, tokenizer_model=None, filter_mode=None):
            from bos.core.contract import TokenEstimate
            return TokenEstimate(count=0, tokenizer_model=tokenizer_model, source="fallback")

        async def save_summary(self, chat_id, summary):
            pass

        async def get_summary(self, chat_id):
            return None

        async def get_messages(self, chat_id, *, active_only=True):
            return []

        async def list_chats(self):
            return {}

    class LLM:
        async def complete(self, messages, **kwargs):
            from bos.core import LLMResponse

            return LLMResponse(content="answer", finish_reason="stop")

    store = Store()
    agent = ReActAgent(
        kind="test",
        agent_name="test",
        chat_store=store,
        consolidator=None,
        llm=LLM(),
        tools=[],
    )
    await agent.ask(
        "chat",
        "hello",
        ctx_metadata={
            "user_message_metadata": {"speaker_type": "user", "to_actor": "bob"},
            "assistant_message_metadata": {"speaker_type": "actor", "from_actor": "bob"},
        },
    )
    assert store.messages[0].metadata == {"speaker_type": "user", "to_actor": "bob"}
    assert store.messages[1].metadata == {"speaker_type": "actor", "from_actor": "bob"}
