from datetime import datetime

import pytest

from bos.named_actors.actor import NamedActor
from bos.protocol import Envelope, MessageType


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def ask(self, chat_id, message, **kwargs):
        self.calls.append((chat_id, message, kwargs))
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
async def test_react_agent_persists_named_actor_message_metadata():
    from bos.core import ReactAgent

    class Store:
        def __init__(self):
            self.messages = []

        async def get_messages(self, chat_id, original=False):
            return []

        async def save_messages(self, chat_id, messages):
            self.messages.extend(messages)

        async def save_summary(self, chat_id, summary):
            pass

        async def list_chats(self):
            return {}

    class LLM:
        async def complete(self, messages, **kwargs):
            from bos.core import LLMResponse

            return LLMResponse(content="answer", finish_reason="stop")

    class Skills:
        async def search_skills(self, query=None):
            return {}

        async def load_skill(self, name):
            return ""

    store = Store()
    agent = ReactAgent(
        message_store=store,
        memory=None,
        consolidator=None,
        skills_loader=Skills(),
        llm=LLM(),
        tools=[],
        skills=[],
        subagents=[],
        maxims={},
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
