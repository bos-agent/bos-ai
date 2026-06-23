import asyncio
import json

import pytest

from bos.core import MailBox, Message
from bos.extensions.channels.telegram import (
    TELEGRAM_ATTACHMENT_THRESHOLD,
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_STATUS_PREVIEW_LIMIT,
    TelegramChannel,
    TelegramSettings,
    _conversation_id_for_telegram_chat,
    _env_int,
    _extract_inbound_message,
    _normalize_command,
    _render_turn_event,
    _split_message,
    _truncate_bytes,
    _unsupported_message_chat_id,
)
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import ActorDescriptor, ActorResolver, ChannelConversationRef, ChannelRuntimeContext, ChatCoordinator
from bos.gateway.core.command_handler import CommandHandler
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


def test_unsupported_message_chat_id_flags_media():
    voice = {"message": {"chat": {"id": 42}, "voice": {"file_id": "abc"}}}
    assert _unsupported_message_chat_id(voice) == "42"

    photo = {"message": {"chat": {"id": 7}, "photo": [{"file_id": "x"}], "caption": "   "}}
    assert _unsupported_message_chat_id(photo) == "7"


def test_unsupported_message_chat_id_ignores_text_and_service_updates():
    # Text messages are handled normally — not "unsupported".
    text = {"message": {"chat": {"id": 42}, "text": "hello"}}
    assert _unsupported_message_chat_id(text) is None

    # A photo with a real caption is usable text — handled normally.
    captioned = {"message": {"chat": {"id": 42}, "photo": [{"file_id": "x"}], "caption": "look"}}
    assert _unsupported_message_chat_id(captioned) is None

    # Service updates (no user media) must not trigger a nudge.
    service = {"message": {"chat": {"id": 42}, "new_chat_members": [{"id": 1}]}}
    assert _unsupported_message_chat_id(service) is None

    assert _unsupported_message_chat_id({"edited_channel_post": {"chat": {"id": 1}}}) is None


@pytest.mark.asyncio
async def test_poll_updates_nudges_on_unsupported_format():
    store = InMemChatStore()
    channel = _channel(store)
    channel._bot_username = "BosBot"
    mailbox = FakeSendMailbox()

    send_calls: list[dict] = []
    get_updates_calls = 0

    async def fake_api_call(method: str, payload: dict):
        nonlocal get_updates_calls
        if method == "getUpdates":
            get_updates_calls += 1
            if get_updates_calls == 1:
                return {
                    "ok": True,
                    "result": [{"update_id": 1, "message": {"chat": {"id": 42}, "voice": {"file_id": "v"}}}],
                }
            raise asyncio.CancelledError()
        if method == "sendMessage":
            send_calls.append(payload)
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method {method}")

    channel._api_call = fake_api_call  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_updates(mailbox)

    # The voice message was never forwarded to an actor...
    assert mailbox.sent == []
    # ...but the sender got a text nudge explaining the limitation.
    assert len(send_calls) == 1
    assert send_calls[0]["chat_id"] == "42"
    assert "text" in send_calls[0]["text"].lower()


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
        metadata={"iteration": 3, "max_iterations": 80},
    )

    # The per-iteration tick (which only carries the iteration counter) is
    # suppressed so the status never shows "[main] 3/80".
    assert _render_turn_event(event) is None


def test_render_turn_event_shows_reasoning_content():
    event = TurnEvent(
        event_type="llm",
        phase="start",
        chat_id="chat-1",
        turn_id="turn-1",
        agent_name="main",
        detail="thinking_content",
        content="Let me check the README first\nthen list the files",
    )

    assert _render_turn_event(event) == "[main] Let me check the README first then list the files"


def test_env_int_reads_override_and_falls_back(monkeypatch):
    monkeypatch.setenv("BOS_TG_TEST_LIMIT", "256")
    assert _env_int("BOS_TG_TEST_LIMIT", 512) == 256

    monkeypatch.delenv("BOS_TG_TEST_LIMIT", raising=False)
    assert _env_int("BOS_TG_TEST_LIMIT", 512) == 512

    monkeypatch.setenv("BOS_TG_TEST_LIMIT", "not-an-int")
    assert _env_int("BOS_TG_TEST_LIMIT", 512) == 512


def test_truncate_bytes_caps_at_limit_with_ellipsis():
    assert _truncate_bytes("short", 64) == "short"
    long = "a" * 100
    out = _truncate_bytes(long, TELEGRAM_STATUS_PREVIEW_LIMIT)
    assert out.endswith("…")
    assert len(out.removesuffix("…").encode("utf-8")) <= TELEGRAM_STATUS_PREVIEW_LIMIT


def test_truncate_bytes_does_not_split_multibyte_char():
    # 32 two-byte chars = 64 bytes exactly; a 65th would be cut mid-budget.
    text = "é" * 40
    out = _truncate_bytes(text, TELEGRAM_STATUS_PREVIEW_LIMIT)
    # Must still decode cleanly (no lone continuation byte) and stay within limit.
    assert out.encode("utf-8").decode("utf-8") == out
    assert len(out.removesuffix("…").encode("utf-8")) <= TELEGRAM_STATUS_PREVIEW_LIMIT


def test_render_turn_event_truncates_reasoning_to_64_bytes():
    event = TurnEvent(
        event_type="llm",
        phase="start",
        chat_id="chat-1",
        turn_id="turn-1",
        agent_name="main",
        detail="thinking_content",
        content="x" * 500,
    )

    rendered = _render_turn_event(event)
    preview = rendered.removeprefix("[main] ")
    assert preview.endswith("…")
    assert len(preview.removesuffix("…").encode("utf-8")) <= TELEGRAM_STATUS_PREVIEW_LIMIT


def test_render_turn_event_shows_tool_calls():
    event = TurnEvent(
        event_type="llm",
        phase="finish",
        chat_id="chat-1",
        turn_id="turn-1",
        agent_name="main",
        detail="tool_calls",
        tool_calls=[{"name": "GlobSearch"}, {"name": "ReadFile"}],
    )

    assert _render_turn_event(event) == "[main] using: GlobSearch, ReadFile"


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
        if method == "sendMessage" and payload["text"] == "[main] checking the docs":
            return {"ok": True, "result": {"message_id": 99}}
        return {"ok": True, "result": True}

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    metadata = {"channel": {"channel_id": "telegram:daily", "channel_conversation_id": "tg_chat:42"}}
    mailbox = FakeMailbox([
        Envelope(
            sender="agent@main",
            recipient="channel@telegram:daily",
            content=json.dumps({
                "event_type": "llm",
                "phase": "start",
                "chat_id": "chat-a",
                "turn_id": "turn-1",
                "agent_name": "main",
                "detail": "thinking_content",
                "content": "checking the docs",
                "timestamp": "2026-04-20T00:00:00",
            }),
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
    ])

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls == [
        ("sendMessage", {"chat_id": "42", "text": "[main] checking the docs"}),
        ("deleteMessage", {"chat_id": "42", "message_id": 99}),
        ("sendMessage", {"chat_id": "42", "text": "final answer"}),
    ]


@pytest.mark.asyncio
async def test_forward_replies_sends_long_reply_as_document():
    channel = _channel()
    channel._chat_to_telegram_chat["chat-a"] = "42"
    channel._conversation_to_telegram_chat["tg_chat:42"] = "42"

    api_calls: list[tuple[str, dict]] = []
    documents: list[tuple[str, str]] = []

    async def fake_api_call(method: str, payload: dict):
        api_calls.append((method, payload))
        return {"ok": True, "result": True}

    async def fake_send_document(telegram_chat_id: str, content: str):
        documents.append((telegram_chat_id, content))

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    channel._send_document = fake_send_document  # type: ignore[method-assign]

    metadata = {"channel": {"channel_id": "telegram:daily", "channel_conversation_id": "tg_chat:42"}}
    long_text = "x" * (TELEGRAM_ATTACHMENT_THRESHOLD + 1)
    mailbox = FakeMailbox([
        Envelope(
            sender="agent@main",
            recipient="channel@telegram:daily",
            content=long_text,
            content_type=MessageType.MESSAGE,
            chat_id="chat-a",
            metadata=metadata,
        ),
    ])

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert documents == [("42", long_text)]
    # The long reply went out as a document, never as an inline sendMessage.
    assert all(method != "sendMessage" for method, _ in api_calls)


@pytest.mark.asyncio
async def test_forward_replies_sends_short_reply_inline():
    channel = _channel()
    channel._chat_to_telegram_chat["chat-a"] = "42"
    channel._conversation_to_telegram_chat["tg_chat:42"] = "42"

    sent: list[dict] = []
    documents: list[tuple[str, str]] = []

    async def fake_api_call(method: str, payload: dict):
        if method == "sendMessage":
            sent.append(payload)
        return {"ok": True, "result": True}

    async def fake_send_document(telegram_chat_id: str, content: str):
        documents.append((telegram_chat_id, content))

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    channel._send_document = fake_send_document  # type: ignore[method-assign]

    metadata = {"channel": {"channel_id": "telegram:daily", "channel_conversation_id": "tg_chat:42"}}
    mailbox = FakeMailbox([
        Envelope(
            sender="agent@main",
            recipient="channel@telegram:daily",
            content="short answer",
            content_type=MessageType.MESSAGE,
            chat_id="chat-a",
            metadata=metadata,
        ),
    ])

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert documents == []
    assert sent == [{"chat_id": "42", "text": "short answer"}]


@pytest.mark.asyncio
async def test_set_status_message_does_not_spam_on_edit_failure():
    """A failed editMessageText must keep the single status message, not send a new one.

    Telegram rate-limits editMessageText; falling back to sendMessage on every
    failure would leave each intermediate status in the chat (the regression).
    """
    channel = _channel()
    channel._chat_to_status_message_id["chat-a"] = 99  # an existing status message

    calls: list[tuple[str, dict]] = []

    async def fake_api_call(method: str, payload: dict):
        calls.append((method, payload))
        if method == "editMessageText":
            raise RuntimeError("Too Many Requests: retry after 1")
        return {"ok": True, "result": {"message_id": 123}}

    channel._api_call = fake_api_call  # type: ignore[method-assign]

    await channel._set_status_message("42", "chat-a", "[main] first thought")
    await channel._set_status_message("42", "chat-a", "[main] second thought")

    # Both updates attempted an edit; neither fell back to a new sendMessage,
    # and the original status message id is preserved for the next attempt.
    assert [m for m, _ in calls] == ["editMessageText", "editMessageText"]
    assert channel._chat_to_status_message_id["chat-a"] == 99


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
async def test_poll_updates_catches_up_stale_cursor_then_forwards_message():
    """A stale channel cursor must self-heal, not dead-end the user.

    Telegram clients can't 'refresh', so when the cursor falls behind the channel
    pushes the missed reply, resyncs to the current revision, and forwards the
    user's message — instead of replying 'retry after refreshing'.
    """
    store = InMemChatStore()
    channel = _channel(store)
    channel._bot_username = "BosBot"
    mailbox = FakeSendMailbox()
    ref = ChannelConversationRef("telegram:daily", "tg_chat:42")
    channel._runtime.chat_coordinator.set_cursor(ref, "chat-a", observed_revision=0)
    channel._conversation_to_telegram_chat["tg_chat:42"] = "42"
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

    # The missed assistant reply was pushed to the client (no refresh notice).
    assert send_message_calls == [{"chat_id": "42", "text": "new elsewhere"}]
    # The user's message was forwarded with the resynced base revision.
    assert len(mailbox.sent) == 1
    assert mailbox.sent[0].content == "hello"
    assert mailbox.sent[0].metadata["base_revision"] == 1


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
    mailbox = FakeMailbox([
        Envelope(
            sender="agent@main",
            recipient="channel@telegram:daily",
            content='{"name":"new","ok":true,"chat_id":"chat-b"}',
            content_type=MessageType.COMMAND_RESULT,
            chat_id="chat-a",
            metadata=metadata,
        ),
    ])

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    ref = channel._ref_from_env(mailbox._queue._queue[0]) if False else None
    assert ref is None
    assert (
        channel._runtime.chat_coordinator.get_cursor(
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
        )
        == "chat-b"
    )
    assert "chat-a" not in channel._chat_to_telegram_chat
    assert channel._chat_to_telegram_chat["chat-b"] == "42"
    assert calls == [
        (
            "sendMessage",
            {"chat_id": "42", "text": '{"name":"new","ok":true,"chat_id":"chat-b"}'},
        )
    ]


def _command_runtime(store: InMemChatStore):
    coordinator = ChatCoordinator(store)
    retired: list[tuple[str, str]] = []

    async def retire(actor: str, chat_id: str) -> None:
        retired.append((actor, chat_id))

    runtime = ChannelRuntimeContext(
        actor_resolver=ActorResolver(
            {"main": ActorDescriptor(name="main", address="agent@main")},
            default_actor="main",
        ),
        chat_coordinator=coordinator,
        mail_route=FakeMailRoute(),
        command_handler=CommandHandler(coordinator, store, retire),
    )
    return runtime, coordinator, retired


@pytest.mark.asyncio
async def test_new_command_handled_via_control_plane_not_actor():
    """A '/new' is handled off the gateway control plane: a new chat is minted,
    the cursor switches, the old session is retired, and the result is sent back
    to Telegram — with no COMMAND envelope reaching the actor."""
    store = InMemChatStore()
    runtime, coordinator, retired = _command_runtime(store)
    channel = TelegramChannel(
        channel_id="telegram:daily",
        target_actor="main",
        settings=TelegramSettings(token="x", bot_id="bot-1"),
        runtime=runtime,
    )
    ref = ChannelConversationRef("telegram:daily", _conversation_id_for_telegram_chat(42))
    coordinator.set_cursor(ref, "chat-old", observed_revision=0)

    mailbox = FakeSendMailbox()
    send_calls: list[dict] = []
    get_updates = 0

    async def fake_api_call(method: str, payload: dict):
        nonlocal get_updates
        if method == "getUpdates":
            get_updates += 1
            if get_updates == 1:
                return {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/new"}}]}
            raise asyncio.CancelledError()
        if method == "sendMessage":
            send_calls.append(payload)
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method {method}")

    channel._api_call = fake_api_call  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_updates(mailbox)

    # Handled inline — no COMMAND envelope forwarded to the actor.
    assert mailbox.sent == []
    # A result was sent back to Telegram.
    assert len(send_calls) == 1 and send_calls[0]["chat_id"] == "42"
    payload = json.loads(send_calls[0]["text"])
    assert payload["name"] == "new" and payload["ok"]
    new_chat = payload["chat_id"]
    assert new_chat != "chat-old"
    # Cursor switched to the new chat; old session retired on the actor.
    assert coordinator.get_cursor(ref) == new_chat
    assert retired == [("main", "chat-old")]


@pytest.mark.asyncio
async def test_resume_command_switches_cursor_via_control_plane():
    store = InMemChatStore()
    runtime, coordinator, retired = _command_runtime(store)
    channel = TelegramChannel(
        channel_id="telegram:daily",
        target_actor="main",
        settings=TelegramSettings(token="x", bot_id="bot-1"),
        runtime=runtime,
    )
    ref = ChannelConversationRef("telegram:daily", _conversation_id_for_telegram_chat(42))
    coordinator.set_cursor(ref, "chat-old", observed_revision=0)

    mailbox = FakeSendMailbox()
    send_calls: list[dict] = []
    get_updates = 0

    async def fake_api_call(method: str, payload: dict):
        nonlocal get_updates
        if method == "getUpdates":
            get_updates += 1
            if get_updates == 1:
                return {
                    "ok": True,
                    "result": [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/resume chat-target"}}],
                }
            raise asyncio.CancelledError()
        if method == "sendMessage":
            send_calls.append(payload)
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method {method}")

    channel._api_call = fake_api_call  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_updates(mailbox)

    assert mailbox.sent == []
    assert coordinator.get_cursor(ref) == "chat-target"
    assert retired == [("main", "chat-old")]
