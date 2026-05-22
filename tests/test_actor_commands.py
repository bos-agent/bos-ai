import asyncio
import json
import uuid

import pytest
from conftest import InMemMailRoute, InMemMemoryExtension, InMemMessageStore, MessageOnlyConsolidator

from bos.core.actor import AgentActor
from bos.core.chat_state import ChatState
from bos.core.contract import Message
from bos.core.history import HistoryProjection
from bos.extensions.actor_commands import system_cmd  # noqa: F401
from bos.protocol import Envelope, MessageType


class FakeMailbox:
    def __init__(self, address: str) -> None:
        self.address = address
        self.sent: list[Envelope] = []

    async def send(
        self,
        recipient: str,
        content: str,
        *,
        content_type: str = "message",
        chat_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.sent.append(
            Envelope(
                sender=self.address,
                recipient=recipient,
                content=content,
                content_type=content_type,
                chat_id=chat_id,
                metadata=metadata or {},
            )
        )

    async def receive_nowait(self) -> Envelope | None:
        return None


class StubAgent:
    def __init__(self) -> None:
        self._message_store = InMemMessageStore()
        self._memory = InMemMemoryExtension()
        self._consolidator = MessageOnlyConsolidator()
        self._model = "test/model"

    async def ask(self, *args, **kwargs):
        raise AssertionError("ask should not be used in direct command tests")

    async def _build_system_prompt(self) -> str:
        return "Rendered system prompt"


class SlowAgent(StubAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def ask(self, chat_id, content, **kwargs):
        self.started.set()
        await self.finish.wait()
        return f"reply for {chat_id}"


class CleanupSlowAgent(StubAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_done = asyncio.Event()

    async def ask(self, chat_id, content, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await self.cleanup_done.wait()
            raise


@pytest.mark.asyncio
async def test_new_command_returns_structured_payload():
    mailbox = FakeMailbox("agent@main")
    actor = AgentActor(StubAgent(), mailbox)
    env = Envelope(
        sender="channel@telegram",
        recipient="agent@main",
        content="/new",
        content_type=MessageType.COMMAND,
        chat_id="telegram:42",
    )

    await actor._handle_command(env)

    assert len(mailbox.sent) == 1
    result_env = mailbox.sent[0]
    assert result_env.recipient == "channel@telegram"
    assert result_env.content_type == MessageType.COMMAND_RESULT
    assert result_env.chat_id == "telegram:42"

    payload = json.loads(result_env.content)
    assert payload["name"] == "new"
    assert payload["ok"] is True
    assert payload["chat_id"]
    # Old session should have been popped
    assert "telegram:42" not in actor._sessions


@pytest.mark.asyncio
async def test_new_command_pops_old_session_and_returns_fresh_id():
    mailbox = FakeMailbox("agent@main")
    agent = StubAgent()
    actor = AgentActor(agent, mailbox)
    old_chat_id = "old-chat"
    actor._get_or_create_session(old_chat_id)
    await agent._message_store.save_messages(
        old_chat_id,
        [Message(llm_message={"role": "user", "content": "old message"})],
    )
    env = Envelope(
        sender="channel@http",
        recipient="agent@main",
        content="/new",
        content_type=MessageType.COMMAND,
        chat_id=old_chat_id,
        metadata={"routing": {"client_id": "client-1", "chat_id": "old-chat"}},
    )

    await actor._handle_command(env)

    payload = json.loads(mailbox.sent[-1].content)
    assert payload["name"] == "new"
    assert payload["ok"] is True
    assert payload["chat_id"] != old_chat_id
    assert old_chat_id not in actor._sessions

    # Old messages should still be retrievable from the store
    history_env = Envelope(
        sender="channel@http",
        recipient="agent@main",
        content=f"/history {old_chat_id}",
        content_type=MessageType.COMMAND,
        chat_id=old_chat_id,
        metadata={"routing": {"client_id": "client-1", "chat_id": "old-chat"}},
    )
    await actor._handle_command(history_env)

    history_payload = json.loads(mailbox.sent[-1].content)
    assert history_payload["result"][0]["content"] == "old message"


@pytest.mark.asyncio
async def test_new_command_updates_client_cursor():
    mailbox = FakeMailbox("agent@main")
    state = ChatState()
    state.set_cursor("tui:a", "old-chat")
    actor = AgentActor(StubAgent(), mailbox, chat_state=state)
    actor._get_or_create_session("old-chat")
    env = Envelope(
        sender="channel@http",
        recipient="agent@main",
        content="/new",
        content_type=MessageType.COMMAND,
        chat_id="old-chat",
        metadata={"routing": {"client_id": "tui:a", "chat_id": "old-chat"}},
    )

    await actor._handle_command(env)

    payload = json.loads(mailbox.sent[-1].content)
    assert payload["name"] == "new"
    assert payload["ok"] is True
    assert payload["chat_id"]
    assert state.get_cursor("tui:a") == payload["chat_id"]
    assert "old-chat" not in actor._sessions


@pytest.mark.asyncio
async def test_resume_alias_updates_client_cursor():
    mailbox = FakeMailbox("agent@main")
    state = ChatState()
    state.set_alias("Project X", "chat-a")
    actor = AgentActor(StubAgent(), mailbox, chat_state=state)
    env = Envelope(
        sender="channel@http",
        recipient="agent@main",
        content="/resume project-x",
        content_type=MessageType.COMMAND,
        chat_id="chat-old",
        metadata={"routing": {"client_id": "tui:a", "chat_id": "chat-old"}},
    )

    await actor._handle_command(env)

    payload = json.loads(mailbox.sent[-1].content)
    assert payload["name"] == "resume"
    assert payload["ok"] is True
    assert payload["chat_id"] == "chat-a"
    assert state.get_cursor("tui:a") == "chat-a"


@pytest.mark.asyncio
async def test_alias_and_unalias_commands_manage_current_chat():
    mailbox = FakeMailbox("agent@main")
    state = ChatState()
    actor = AgentActor(StubAgent(), mailbox, chat_state=state)

    await actor._handle_command(
        Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="/alias Project X",
            content_type=MessageType.COMMAND,
            chat_id="chat-a",
            metadata={"routing": {"client_id": "tui:a", "chat_id": "chat-a"}},
        )
    )
    await actor._handle_command(
        Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="/aliases",
            content_type=MessageType.COMMAND,
            chat_id="chat-a",
            metadata={"routing": {"client_id": "tui:a", "chat_id": "chat-a"}},
        )
    )
    await actor._handle_command(
        Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="/unalias project-x",
            content_type=MessageType.COMMAND,
            chat_id="chat-a",
            metadata={"routing": {"client_id": "tui:a", "chat_id": "chat-a"}},
        )
    )

    alias_payload = json.loads(mailbox.sent[-3].content)
    aliases_payload = json.loads(mailbox.sent[-2].content)
    unalias_payload = json.loads(mailbox.sent[-1].content)
    assert alias_payload["ok"] is True
    assert aliases_payload["result"] == {"project-x": "chat-a"}
    assert unalias_payload["result"] == "alias removed"
    assert state.list_aliases() == {}


@pytest.mark.asyncio
async def test_history_command_uses_envelope_chat_id():
    mailbox = FakeMailbox("agent@main")
    agent = StubAgent()
    actor = AgentActor(agent, mailbox)
    chat_id = "telegram:42"

    await agent._message_store.save_messages(
        chat_id,
        [Message(llm_message={"role": "user", "content": "saved under chat"})],
    )

    history_env = Envelope(
        sender="channel@telegram",
        recipient="agent@main",
        content="/history",
        content_type=MessageType.COMMAND,
        chat_id=chat_id,
    )
    await actor._handle_command(history_env)

    payload = json.loads(mailbox.sent[-1].content)
    assert payload["name"] == "history"
    assert payload["result"][0]["content"] == "saved under chat"


@pytest.mark.asyncio
async def test_compact_command_passes_message_objects_and_saves_summary():
    mailbox = FakeMailbox("agent@main")
    agent = StubAgent()
    actor = AgentActor(agent, mailbox)
    chat_id = "compact-chat"
    await agent._message_store.save_messages(
        chat_id,
        [Message(llm_message={"role": "user", "content": "history"})],
    )

    await actor._handle_command(
        Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="/compact",
            content_type=MessageType.COMMAND,
            chat_id=chat_id,
        )
    )

    payload = json.loads(mailbox.sent[-1].content)
    messages = await agent._message_store.get_messages(chat_id)
    assert payload["name"] == "compact"
    assert payload["ok"] is True
    assert agent._consolidator.calls
    assert all(isinstance(message, Message) for message in agent._consolidator.calls[0][0])
    assert messages[-1].is_summary is True
    assert messages[-1].llm_message["content"] == "Chat summary:\nrecorded summary"


@pytest.mark.asyncio
async def test_tokens_command_returns_estimate_metadata(monkeypatch):
    mailbox = FakeMailbox("agent@main")
    agent = StubAgent()
    actor = AgentActor(agent, mailbox)
    chat_id = "tokens-chat"
    await agent._message_store.save_messages(
        chat_id,
        [Message(llm_message={"role": "user", "content": "history"})],
    )

    def fake_estimate(messages, *, budget_model):
        assert all(isinstance(message, Message) for message in messages)
        return HistoryProjection(
            messages=[message.llm_message for message in messages],
            estimated_tokens=123,
            model=budget_model,
            source="fallback",
        )

    monkeypatch.setattr(system_cmd, "estimate_message_history_tokens", fake_estimate)
    await actor._handle_command(
        Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="/tokens",
            content_type=MessageType.COMMAND,
            chat_id=chat_id,
        )
    )

    payload = json.loads(mailbox.sent[-1].content)
    assert payload["name"] == "tokens"
    assert payload["ok"] is True
    assert payload["estimated_tokens"] == 123
    assert payload["model"] == "test/model"
    assert payload["source"] == "fallback"
    assert "Estimated tokens: 123" in payload["result"]


@pytest.mark.asyncio
async def test_prompt_command_returns_current_agent_system_prompt():
    mailbox = FakeMailbox("agent@main")
    actor = AgentActor(StubAgent(), mailbox)
    env = Envelope(
        sender="channel@telegram",
        recipient="agent@main",
        content="/prompt",
        content_type=MessageType.COMMAND,
        chat_id="telegram:42",
    )

    await actor._handle_command(env)

    payload = json.loads(mailbox.sent[-1].content)
    assert payload["name"] == "prompt"
    assert payload["ok"] is True
    assert payload["result"] == "Rendered system prompt"


@pytest.mark.asyncio
async def test_memory_command_is_not_registered():
    mailbox = FakeMailbox("agent@main")
    actor = AgentActor(StubAgent(), mailbox)
    env = Envelope(
        sender="channel@telegram",
        recipient="agent@main",
        content="/memory",
        content_type=MessageType.COMMAND,
        chat_id="telegram:42",
    )

    await actor._handle_command(env)

    assert mailbox.sent[-1].content == "Invalid command `memory`"


@pytest.mark.asyncio
async def test_new_cancels_or_fences_in_flight_reply_and_drops_stale_result():
    route = InMemMailRoute()
    actor_mailbox = route.bind("agent@main")
    sender_mailbox = route.bind("channel@http")
    agent = SlowAgent()
    actor = AgentActor(agent, actor_mailbox)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send("agent@main", "hello", chat_id="legacy-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        await sender_mailbox.send(
            "agent@main",
            "/new",
            content_type=MessageType.COMMAND,
            chat_id="legacy-chat",
        )

        command_result = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        payload = json.loads(command_result.content)
        assert command_result.content_type == MessageType.COMMAND_RESULT
        assert payload["name"] == "new"
        assert payload["ok"] is True

        agent.finish.set()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sender_mailbox.receive(), timeout=0.2)
    finally:
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_abort_cancels_in_flight_turn_without_reply():
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    agent = SlowAgent()
    actor = AgentActor(agent, actor_mailbox)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "hello", chat_id="interrupt-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        await sender_mailbox.send(
            actor_address,
            "",
            content_type=MessageType.INTERRUPT_ABORT,
            chat_id="interrupt-chat",
        )

        for _ in range(20):
            session = actor._sessions.get("interrupt-chat")
            if session is not None and session.execution.task is None:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("in-flight turn was not interrupted")

        agent.finish.set()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sender_mailbox.receive(), timeout=0.2)
    finally:
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_abort_keeps_session_busy_until_cancel_cleanup_finishes():
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    agent = CleanupSlowAgent()
    actor = AgentActor(agent, actor_mailbox)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "hello", chat_id="cleanup-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        await sender_mailbox.send(
            actor_address,
            "",
            content_type=MessageType.INTERRUPT_ABORT,
            chat_id="cleanup-chat",
        )
        await asyncio.wait_for(agent.cleanup_started.wait(), timeout=1)

        await sender_mailbox.send(actor_address, "continue", chat_id="cleanup-chat")
        rejection = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert rejection.content == "(busy: a response is already in progress for this chat)"
        assert rejection.content_type == MessageType.SYSTEM

        agent.cleanup_done.set()
        for _ in range(20):
            session = actor._sessions.get("cleanup-chat")
            if session is not None and session.execution.task is None:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("in-flight turn cleanup did not finish")
    finally:
        agent.cleanup_done.set()
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resume_retires_previous_in_flight_session():
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    agent = SlowAgent()
    state = ChatState()
    state.set_cursor("tui:a", "legacy-chat")
    actor = AgentActor(agent, actor_mailbox, chat_state=state)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(
            actor_address,
            "hello",
            chat_id="legacy-chat",
            metadata={"routing": {"client_id": "tui:a", "chat_id": "legacy-chat"}},
        )
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        await sender_mailbox.send(
            actor_address,
            "/resume next-chat",
            content_type=MessageType.COMMAND,
            chat_id="legacy-chat",
            metadata={"routing": {"client_id": "tui:a", "chat_id": "legacy-chat"}},
        )

        command_result = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        payload = json.loads(command_result.content)
        assert payload["name"] == "resume"
        assert payload["ok"] is True
        assert state.get_cursor("tui:a") == "next-chat"

        agent.finish.set()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sender_mailbox.receive(), timeout=0.2)
    finally:
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_message_during_active_turn_is_rejected_with_busy():
    """Normal messages arriving while a turn is in-flight get an immediate
    SYSTEM rejection so the sender can decide its own retry strategy."""
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    agent = SlowAgent()
    actor = AgentActor(agent, actor_mailbox)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "hello", chat_id="busy-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        # Second message while turn is still in-flight
        await sender_mailbox.send(actor_address, "ping", chat_id="busy-chat")

        rejection = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert rejection.content == "(busy: a response is already in progress for this chat)"
        assert rejection.content_type == MessageType.SYSTEM
        assert rejection.chat_id == "busy-chat"
    finally:
        agent.finish.set()
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_message_after_turn_completes_is_accepted():
    """After the in-flight turn finishes, a new message starts a fresh turn."""
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    agent = SlowAgent()
    actor = AgentActor(agent, actor_mailbox)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "first", chat_id="seq-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        # Let the turn finish
        agent.finish.set()
        reply = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert reply.content == "reply for seq-chat"

        # Reset for the next turn
        agent.started.clear()
        agent.finish.clear()

        # Next message should be accepted and start a new turn
        await sender_mailbox.send(actor_address, "second", chat_id="seq-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        agent.finish.set()
        reply2 = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
        assert reply2.content == "reply for seq-chat"
    finally:
        agent.finish.set()
        actor_task.cancel()
        await asyncio.gather(actor_task, return_exceptions=True)
