# tests/squad/test_actor.py
from datetime import datetime

from bos.protocol import Envelope, MessageType
from bos.squad.actor import SquadActor


class FakeAgent:
    async def ask(self, chat_id, message, **kwargs):
        return f"echo: {message}"


class FakeMailBox:
    def __init__(self):
        self.address = "agent@test"
        self.sent: list = []

    async def send(self, recipient, content, **kwargs):
        self.sent.append((recipient, content, kwargs))

    async def receive_nowait(self):
        return None


class TestMergePendingMessages:
    def test_annotates_with_target_actor(self):
        actor = SquadActor(
            FakeAgent(), FakeMailBox(), actor_name="researcher"
        )
        env1 = Envelope(
            sender="channel@http",
            recipient="agent@researcher",
            content="find papers",
            content_type=MessageType.MESSAGE,
            chat_id="abc123",
            timestamp=datetime.now(),
            metadata={"target_actor": "researcher"},
        )
        result = actor._merge_pending_messages([env1])
        parts = result if isinstance(result, list) else [{"type": "text", "text": str(result)}]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        assert "[user → @researcher]" in text
        assert "find papers" in text

    def test_no_actor_name_no_annotation(self):
        actor = SquadActor(
            FakeAgent(), FakeMailBox(), actor_name=None
        )
        env1 = Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="hello",
            content_type=MessageType.MESSAGE,
            chat_id="abc123",
            timestamp=datetime.now(),
        )
        result = actor._merge_pending_messages([env1])
        text = str(result)
        assert "[user →" not in text
        assert "hello" in text

