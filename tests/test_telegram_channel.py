import asyncio

import pytest

from bos.core import MailBox, Message
from bos.extensions.channels.telegram import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramChannel,
    TelegramSettings,
    _conversation_id_for_telegram_chat,
    _extract_inbound_message,
    _normalize_command,
    _render_turn_event,
    _split_message,
)
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import ActorDescriptor, ActorResolver, ChannelConversationRef, ChannelRuntimeContext, ChatCoordinator
from bos.protocol import Envelope, MessageType, TurnEvent


class FakeMailbox:
    def __init__(self, inbox: list[Envelope]) -> None:
        self._queue = asyncio.Queue()
        for env in inbox:
            self._queue.put_nowait(env)

    async def receive(self) -> Envelope:
        return await self._queue.get()


class FakeSendMailbox:
    def __init__(self) -> None:
        self.sent: list[Envelope] = []
        self.address = "channel@telegram:daily"

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


class FakeMailRoute:
    def bind(self, address: str) -> MailBox:
        return FakeSendMailbox()  # type: ignore[return-value]

    async def deliver(self, env: Envelope) -> None:
        raise AssertionError("not used")


def _runtime(store: InMemChatStore | None = None) -> ChannelRuntimeContext:
    return ChannelRuntimeContext(
        actor_resolver=ActorResolver(
            {"main": ActorDescriptor(name="main", address="agent@main")},
            default_actor="main",
        ),
        chat_coordinator=ChatCoordinator(store or InMemChatStore()),
        mail_route=FakeMailRoute(),
    )


def _channel(store: InMemChatStore | None = None) -> TelegramChannel:
    return TelegramChannel(
        channel_id="telegram:daily",
        target_actor="main",
        settings=TelegramSettings(token="x", bot_id="bot-1"),
        runtime=_runtime(store),
    )


def test_normalize_command_strips_matching_bot_mention():
    assert _normalize_command("/history@BosBot details", "BosBot") == "/history details"


def test_normalize_command_keeps_other_bot_mention():
    assert _normalize_command("/history@OtherBot details", "BosBot") == "/history@OtherBot details"


def test_extract_inbound_message_builds_conversation_id_and_command_type():
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 12345},
            "text": "/history@BosBot recent",
        },
    }

    result = _extract_inbound_message(update, bot_username="BosBot")

    assert result == {
        "telegram_chat_id": "12345",
        "channel_conversation_id": "tg_chat:12345",
        "text": "/history recent",
        "content_type": "command",
    }


def test_extract_inbound_message_ignores_non_text_updates():
    update = {"update_id": 1, "message": {"chat": {"id": 12345}, "photo": [{"file_id": "abc"}]}}
    assert _extract_inbound_message(update) is None


def test_split_message_respects_limit():
    text = ("a" * (TELEGRAM_MESSAGE_LIMIT - 10)) + "\n" + ("b" * 100)
    parts = _split_message(text)

    assert len(parts) == 2
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert all(len(part) <= TELEGRAM_MESSAGE_LIMIT for part in parts)


def test_conversation_id_for_telegram_chat():
    assert _conversation_id_for_telegram_chat(987654321) == "tg_chat:987654321"


def test_telegram_identity_key_uses_bot_id():
    assert _channel().identity_key == "telegram:bot:bot-1"


def test_render_turn_event_suppresses_thinking():
    event = TurnEvent(
        event_type="llm",
        phase="start",
        chat_id="chat-1",
        turn_id="turn-1",
        agent_name="main",
        detail="thinking",
    )

    assert _render_turn_event(event) == "main is thinking…"


def test_render_turn_event_formats_tool_result():
    event = TurnEvent(
        event_type="tool",
        phase="finish",
        chat_id="chat-1",
        turn_id="turn-1",
        agent_name="researcher",
        parent_agent_name="main",
        detail="tool_result",
        tool_name="SearchSkills",
        content="Found three matching skills in the workspace.",
    )

    assert (
        _render_turn_event(event)
        == "main -> researcher finished SearchSkills: Found three matching skills in the workspace."
    )


@pytest.mark.asyncio
async def test_forward_replies_uses_transient_status_message_then_final_reply():
    channel = _channel()
    channel._chat_to_telegram_chat["chat-a"] = "42"
    channel._conversation_to_telegram_chat["tg_chat:42"] = "42"

    calls: list[tuple[str, dict]] = []

    async def fake_api_call(method: str, payload: dict):
        calls.append((method, payload))
        if method == "sendMessage" and payload["text"] == "main is thinking…":
            return {"ok": True, "result": {"message_id": 99}}
        return {"ok": True, "result": True}

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    metadata = {"channel": {"channel_id": "telegram:daily", "channel_conversation_id": "tg_chat:42"}}
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@telegram:daily",
                content='{"event_type":"llm","phase":"start","chat_id":"chat-a","turn_id":"turn-1","agent_name":"main","detail":"thinking","timestamp":"2026-04-20T00:00:00"}',
                content_type=MessageType.TURN_EVENT,
                chat_id="chat-a",
                metadata=metadata,
            ),
            Envelope(
                sender="agent@main",
                recipient="channel@telegram:daily",
                content="final answer",
                content_type=MessageType.MESSAGE,
                chat_id="chat-a",
                metadata=metadata,
            ),
        ]
    )

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls == [
        ("sendMessage", {"chat_id": "42", "text": "main is thinking…"}),
        ("deleteMessage", {"chat_id": "42", "message_id": 99}),
        ("sendMessage", {"chat_id": "42", "text": "final answer"}),
    ]


@pytest.mark.asyncio
async def test_poll_updates_uses_chat_coordinator_cursor_and_channel_metadata():
    store = InMemChatStore()
    channel = _channel(store)
    channel._bot_username = "BosBot"
    mailbox = FakeSendMailbox()

    calls = 0

    async def fake_api_call(method: str, payload: dict):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 42}, "text": "hello"},
                    }
                ],
            }
        raise asyncio.CancelledError()

    channel._api_call = fake_api_call  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_updates(mailbox)

    first_chat = mailbox.sent[0].chat_id
    assert first_chat
    assert first_chat != "telegram:42"
    assert mailbox.sent[0].recipient == "agent@main"
    assert mailbox.sent[0].metadata["base_revision"] == 0
    assert mailbox.sent[0].metadata["channel"] == {
        "channel_id": "telegram:daily",
        "channel_conversation_id": "tg_chat:42",
    }


@pytest.mark.asyncio
async def test_poll_updates_rejects_stale_telegram_cursor_instead_of_spoofing_current_revision():
    store = InMemChatStore()
    channel = _channel(store)
    channel._bot_username = "BosBot"
    mailbox = FakeSendMailbox()
    ref = ChannelConversationRef("telegram:daily", "tg_chat:42")
    channel._runtime.chat_coordinator.set_cursor(ref, "chat-a", observed_revision=0)
    await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "assistant", "content": "new elsewhere"})],
        turn_id="turn-1",
    )
    get_updates_calls = 0
    send_message_calls: list[dict] = []

    async def fake_api_call(method: str, payload: dict):
        nonlocal get_updates_calls
        if method == "getUpdates":
            get_updates_calls += 1
            if get_updates_calls == 1:
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {"chat": {"id": 42}, "text": "hello"},
                        }
                    ],
                }
            raise asyncio.CancelledError()
        if method == "sendMessage":
            send_message_calls.append(payload)
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method {method}")

    channel._api_call = fake_api_call  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_updates(mailbox)

    assert mailbox.sent == []
    assert send_message_calls
    assert send_message_calls[0]["chat_id"] == "42"
    assert "new messages" in send_message_calls[0]["text"]


@pytest.mark.asyncio
async def test_forward_replies_updates_telegram_cursor_from_new_result():
    channel = _channel()
    channel._conversation_to_telegram_chat["tg_chat:42"] = "42"
    channel._chat_to_telegram_chat["chat-a"] = "42"

    calls: list[tuple[str, dict]] = []

    async def fake_api_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": True}

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    metadata = {"channel": {"channel_id": "telegram:daily", "channel_conversation_id": "tg_chat:42"}}
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@telegram:daily",
                content='{"name":"new","ok":true,"chat_id":"chat-b"}',
                content_type=MessageType.COMMAND_RESULT,
                chat_id="chat-a",
                metadata=metadata,
            ),
        ]
    )

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    ref = channel._ref_from_env(mailbox._queue._queue[0]) if False else None
    assert ref is None
    assert channel._runtime.chat_coordinator.get_cursor(
        channel._ref_from_env(
            Envelope(
                sender="agent@main",
                recipient="channel@telegram:daily",
                content="",
                content_type=MessageType.MESSAGE,
                chat_id="chat-b",
                metadata=metadata,
            )
        )
    ) == "chat-b"
    assert "chat-a" not in channel._chat_to_telegram_chat
    assert channel._chat_to_telegram_chat["chat-b"] == "42"
    assert calls == [
        (
            "sendMessage",
            {"chat_id": "42", "text": '{"name":"new","ok":true,"chat_id":"chat-b"}'},
        )
    ]
