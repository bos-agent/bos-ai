import asyncio
import uuid

import pytest
from conftest import InMemChatStore, InMemMailRoute, InMemMemoryExtension, MessageOnlyConsolidator

from bos.core.actor import Envelope, MessageType
from bos.gateway.actors.agent_actor import AgentActor


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
        self.name = "stub"
        self._chat_store = InMemChatStore()
        self._memory = InMemMemoryExtension()
        self._consolidator = MessageOnlyConsolidator()
        self._model = "test/model"

    async def ask(self, *args, **kwargs):
        raise AssertionError("ask should not be used in direct command tests")

    async def _build_system_prompt(self, ctx=None) -> str:
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
async def test_actor_turn_hooks_receive_context_and_finish_result():
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    observed: dict[str, object] = {}

    class HookAgent:
        async def ask(self, chat_id, content, **kwargs):
            observed["ask_turn_id"] = kwargs["turn_id"]
            return "done"

    class HookActor(AgentActor):
        async def _on_turn_started(self, ctx):
            observed["started"] = ctx

        async def _on_turn_finished(self, ctx, result):
            observed["finished"] = ctx
            observed["result"] = result

    actor = HookActor(HookAgent(), actor_mailbox)
    task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(
            actor_address,
            "hello",
            chat_id="hook-chat",
            metadata={
                "base_revision": 7,
                "channel": {"channel_id": "tui-a", "channel_conversation_id": "default"},
            },
        )
        reply = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)

        assert reply.content == "done"
        assert reply.metadata["channel"] == {"channel_id": "tui-a", "channel_conversation_id": "default"}
        assert observed["started"].turn_id == observed["ask_turn_id"]
        assert observed["started"].base_revision == 7
        assert observed["started"].channel_ref == {"channel_id": "tui-a", "channel_conversation_id": "default"}
        assert observed["finished"].turn_id == observed["started"].turn_id
        assert observed["result"].status == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
async def test_retire_session_cancels_in_flight_turn_and_drops_stale_result():
    """retire_session — invoked by the gateway control plane on /new and /resume —
    cancels the chat's in-flight turn, so its reply never reaches the client."""
    route = InMemMailRoute()
    actor_address = f"agent@{uuid.uuid4().hex}"
    sender_address = f"channel@{uuid.uuid4().hex}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)
    agent = SlowAgent()
    actor = AgentActor(agent, actor_mailbox)

    actor_task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "hello", chat_id="legacy-chat")
        await asyncio.wait_for(agent.started.wait(), timeout=1)

        await actor.retire_session("legacy-chat")
        assert "legacy-chat" not in actor._sessions

        agent.finish.set()
        # The cancelled turn's reply must not be delivered.
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
