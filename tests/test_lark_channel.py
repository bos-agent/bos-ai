import asyncio
import json
from types import SimpleNamespace

import pytest

from bos.core import MailBox, Message
from bos.extensions.channels.lark import (
    LARK_MESSAGE_LIMIT,
    LarkChannel,
    LarkSettings,
    _conversation_id_for_lark_chat,
    _extract_inbound_message,
    _selected_chat_from_command_result,
    _split_message,
    _strip_mentions,
    _unsupported_message_chat_id,
)
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import ActorDescriptor, ActorResolver, ChannelConversationRef, ChannelRuntimeContext, ChatCoordinator
from bos.protocol import Envelope, MessageType


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
        self.address = "channel@lark:daily"

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


def _channel(store: InMemChatStore | None = None) -> LarkChannel:
    return LarkChannel(
        channel_id="lark:daily",
        target_actor="main",
        settings=LarkSettings(app_id="cli_app", app_secret="secret"),
        runtime=_runtime(store),
    )


def _capture_deliver(channel: LarkChannel) -> list[tuple[str, str]]:
    """Replace _deliver_text with a capture that needs no lark-oapi/HTTP client."""
    delivered: list[tuple[str, str]] = []

    async def fake_deliver(lark_chat_id: str, text: str) -> None:
        delivered.append((lark_chat_id, text))

    channel._deliver_text = fake_deliver  # type: ignore[method-assign]
    return delivered


def _text_event(chat_id: int | str, text: str, *, event_id: str = "evt-1", mentions=None) -> dict:
    return {
        "event_id": event_id,
        "sender_type": "user",
        "sender_open_id": "ou_x",
        "message_id": "om_x",
        "chat_id": str(chat_id),
        "chat_type": "p2p",
        "message_type": "text",
        "content": json.dumps({"text": text}),
        "mentions": mentions or [],
    }


# ── pure helpers ───────────────────────────────────────────────────────────


def test_extract_inbound_message_text_is_message():
    result = _extract_inbound_message(_text_event("oc_1", "hello there"))
    assert result == {
        "lark_chat_id": "oc_1",
        "channel_conversation_id": "lark_chat:oc_1",
        "text": "hello there",
        "content_type": "message",
    }


def test_extract_inbound_message_slash_is_command():
    result = _extract_inbound_message(_text_event("oc_1", "/history recent"))
    assert result["content_type"] == "command"
    assert result["text"] == "/history recent"


def test_extract_inbound_message_strips_group_mention():
    event = _text_event(
        "oc_1",
        "@_user_1 what is the weather",
        mentions=[{"key": "@_user_1", "name": "Bot"}],
    )
    result = _extract_inbound_message(event)
    assert result["text"] == "what is the weather"


def test_extract_inbound_message_ignores_non_text():
    event = _text_event("oc_1", "")
    event["message_type"] = "image"
    event["content"] = json.dumps({"image_key": "img_x"})
    assert _extract_inbound_message(event) is None


def test_extract_inbound_message_ignores_empty_text():
    assert _extract_inbound_message(_text_event("oc_1", "   ")) is None


def test_strip_mentions_removes_all_keys():
    assert _strip_mentions("@_user_1 @_user_2 hi", [{"key": "@_user_1"}, {"key": "@_user_2"}]) == "hi"


def test_unsupported_message_chat_id_flags_non_text():
    event = _text_event("oc_9", "")
    event["message_type"] = "audio"
    assert _unsupported_message_chat_id(event) == "oc_9"


def test_unsupported_message_chat_id_ignores_text():
    assert _unsupported_message_chat_id(_text_event("oc_9", "hi")) is None


def test_split_message_respects_limit():
    text = ("a" * (LARK_MESSAGE_LIMIT - 10)) + "\n" + ("b" * 100)
    parts = _split_message(text)
    assert len(parts) == 2
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert all(len(part) <= LARK_MESSAGE_LIMIT for part in parts)


def test_conversation_id_for_lark_chat():
    assert _conversation_id_for_lark_chat("oc_abc") == "lark_chat:oc_abc"


def test_identity_key_uses_app_id():
    assert _channel().identity_key == "lark:app:cli_app"


def test_selected_chat_from_command_result():
    env = Envelope(
        sender="agent@main",
        recipient="channel@lark:daily",
        content='{"name":"new","ok":true,"chat_id":"chat-b"}',
        content_type=MessageType.COMMAND_RESULT,
    )
    assert _selected_chat_from_command_result(env) == "chat-b"


def test_event_to_dict_flattens_sdk_object():
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt-9"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_z")),
            message=SimpleNamespace(
                message_id="om_z",
                chat_id="oc_z",
                chat_type="group",
                message_type="text",
                content='{"text":"hi"}',
                mentions=[SimpleNamespace(key="@_user_1", name="Bot")],
            ),
        ),
    )
    out = LarkChannel._event_to_dict(data)
    assert out["event_id"] == "evt-9"
    assert out["chat_id"] == "oc_z"
    assert out["message_type"] == "text"
    assert out["content"] == '{"text":"hi"}'
    assert out["mentions"] == [{"key": "@_user_1", "name": "Bot"}]


# ── dedup ──────────────────────────────────────────────────────────────────


def test_is_duplicate_tracks_event_ids():
    channel = _channel()
    assert channel._is_duplicate("evt-1") is False
    assert channel._is_duplicate("evt-1") is True
    assert channel._is_duplicate("evt-2") is False
    # Missing event id is never treated as a duplicate.
    assert channel._is_duplicate(None) is False
    assert channel._is_duplicate(None) is False


# ── inbound ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_inbound_uses_coordinator_cursor_and_channel_metadata():
    store = InMemChatStore()
    channel = _channel(store)
    mailbox = FakeSendMailbox()

    await channel._handle_inbound(mailbox, _text_event("oc_42", "hello"))

    assert len(mailbox.sent) == 1
    sent = mailbox.sent[0]
    assert sent.recipient == "agent@main"
    assert sent.content == "hello"
    assert sent.metadata["base_revision"] == 0
    assert sent.metadata["channel"] == {
        "channel_id": "lark:daily",
        "channel_conversation_id": "lark_chat:oc_42",
    }


@pytest.mark.asyncio
async def test_handle_inbound_drops_duplicate_event():
    channel = _channel()
    mailbox = FakeSendMailbox()
    event = _text_event("oc_42", "hello", event_id="evt-dup")

    await channel._handle_inbound(mailbox, event)
    await channel._handle_inbound(mailbox, event)

    assert len(mailbox.sent) == 1


@pytest.mark.asyncio
async def test_handle_inbound_nudges_on_unsupported_format():
    channel = _channel()
    delivered = _capture_deliver(channel)
    mailbox = FakeSendMailbox()
    event = _text_event("oc_42", "")
    event["message_type"] = "image"

    await channel._handle_inbound(mailbox, event)

    assert mailbox.sent == []
    assert len(delivered) == 1
    assert delivered[0][0] == "oc_42"
    assert "text" in delivered[0][1].lower()


@pytest.mark.asyncio
async def test_handle_inbound_respects_allowed_chat_ids():
    channel = LarkChannel(
        channel_id="lark:daily",
        target_actor="main",
        settings=LarkSettings(app_id="cli_app", app_secret="secret", allowed_chat_ids=["oc_ok"]),
        runtime=_runtime(),
    )
    mailbox = FakeSendMailbox()

    await channel._handle_inbound(mailbox, _text_event("oc_blocked", "hi", event_id="evt-a"))
    assert mailbox.sent == []

    await channel._handle_inbound(mailbox, _text_event("oc_ok", "hi", event_id="evt-b"))
    assert len(mailbox.sent) == 1


@pytest.mark.asyncio
async def test_handle_inbound_catches_up_stale_cursor_then_forwards():
    store = InMemChatStore()
    channel = _channel(store)
    delivered = _capture_deliver(channel)
    mailbox = FakeSendMailbox()
    ref = ChannelConversationRef("lark:daily", "lark_chat:oc_42")
    channel._runtime.chat_coordinator.set_cursor(ref, "chat-a", observed_revision=0)
    channel._conversation_to_lark_chat["lark_chat:oc_42"] = "oc_42"
    await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "assistant", "content": "new elsewhere"})],
        turn_id="turn-1",
    )

    await channel._handle_inbound(mailbox, _text_event("oc_42", "hello"))

    # The missed assistant reply was pushed to the client.
    assert delivered == [("oc_42", "new elsewhere")]
    # The user's message was forwarded with the resynced base revision.
    assert len(mailbox.sent) == 1
    assert mailbox.sent[0].content == "hello"
    assert mailbox.sent[0].metadata["base_revision"] == 1


# ── outbound ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_replies_delivers_final_reply():
    channel = _channel()
    delivered = _capture_deliver(channel)
    channel._conversation_to_lark_chat["lark_chat:oc_42"] = "oc_42"
    channel._chat_to_lark_chat["chat-a"] = "oc_42"

    metadata = {"channel": {"channel_id": "lark:daily", "channel_conversation_id": "lark_chat:oc_42"}}
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@lark:daily",
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

    assert delivered == [("oc_42", "final answer")]


@pytest.mark.asyncio
async def test_forward_replies_skips_turn_events():
    channel = _channel()
    delivered = _capture_deliver(channel)
    channel._conversation_to_lark_chat["lark_chat:oc_42"] = "oc_42"
    channel._chat_to_lark_chat["chat-a"] = "oc_42"

    metadata = {"channel": {"channel_id": "lark:daily", "channel_conversation_id": "lark_chat:oc_42"}}
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@lark:daily",
                content=json.dumps({"event_type": "llm", "detail": "thinking_content", "content": "x"}),
                content_type=MessageType.TURN_EVENT,
                chat_id="chat-a",
                metadata=metadata,
            ),
        ]
    )

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert delivered == []


@pytest.mark.asyncio
async def test_forward_replies_updates_cursor_from_new_result():
    channel = _channel()
    _capture_deliver(channel)
    channel._conversation_to_lark_chat["lark_chat:oc_42"] = "oc_42"
    channel._chat_to_lark_chat["chat-a"] = "oc_42"

    metadata = {"channel": {"channel_id": "lark:daily", "channel_conversation_id": "lark_chat:oc_42"}}
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@lark:daily",
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

    ref = ChannelConversationRef("lark:daily", "lark_chat:oc_42")
    assert channel._runtime.chat_coordinator.get_cursor(ref) == "chat-b"
    assert "chat-a" not in channel._chat_to_lark_chat
    assert channel._chat_to_lark_chat["chat-b"] == "oc_42"
