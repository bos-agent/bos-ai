import asyncio

import pytest

from bos.extensions.channels.telegram import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramChannel,
    _client_id_for_telegram_chat,
    _extract_inbound_message,
    _normalize_command,
    _render_turn_event,
    _split_message,
)
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
                sender="channel@telegram",
                recipient=recipient,
                content=content,
                content_type=content_type,
                chat_id=chat_id,
                metadata=metadata or {},
            )
        )


def test_normalize_command_strips_matching_bot_mention():
    assert _normalize_command("/history@BosBot details", "BosBot") == "/history details"


def test_normalize_command_keeps_other_bot_mention():
    assert _normalize_command("/history@OtherBot details", "BosBot") == "/history@OtherBot details"


def test_extract_inbound_message_builds_client_id_and_command_type():
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 12345},
            "text": "/history@BosBot recent",
        },
    }

    result = _extract_inbound_message(update, bot_username="BosBot")

    assert result == {
        "chat_id": 12345,
        "client_id": "telegram:12345",
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


def test_client_id_for_telegram_chat():
    assert _client_id_for_telegram_chat(987654321) == "telegram:987654321"


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
    channel = TelegramChannel(token="x")
    channel._chat_to_telegram_chat["telegram:42"] = "42"

    calls: list[tuple[str, dict]] = []

    async def fake_api_call(method: str, payload: dict):
        calls.append((method, payload))
        if method == "sendMessage" and payload["text"] == "main is thinking…":
            return {"ok": True, "result": {"message_id": 99}}
        return {"ok": True, "result": True}

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@telegram",
                content='{"event_type":"llm","phase":"start","chat_id":"telegram:42","turn_id":"turn-1","agent_name":"main","detail":"thinking","timestamp":"2026-04-20T00:00:00"}',
                content_type=MessageType.TURN_EVENT,
                chat_id="telegram:42",
            ),
            Envelope(
                sender="agent@main",
                recipient="channel@telegram",
                content="final answer",
                content_type=MessageType.MESSAGE,
                chat_id="telegram:42",
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
async def test_forward_replies_routes_by_chat_id_not_metadata():
    channel = TelegramChannel(token="x")

    calls: list[tuple[str, dict]] = []

    async def fake_api_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": True}

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@telegram",
                content="final answer",
                content_type=MessageType.MESSAGE,
                chat_id="telegram:42",
            ),
        ]
    )

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert calls == [("sendMessage", {"chat_id": "42", "text": "final answer"})]


@pytest.mark.asyncio
async def test_poll_updates_uses_server_cursor_for_telegram_client():
    channel = TelegramChannel(token="x")
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
        await channel._poll_updates(mailbox, "agent@main")

    first_chat = mailbox.sent[0].chat_id
    assert first_chat
    assert first_chat != "telegram:42"
    assert mailbox.sent[0].metadata["routing"] == {
        "client_id": "telegram:42",
        "chat_id": first_chat,
    }


@pytest.mark.asyncio
async def test_forward_replies_updates_telegram_cursor_from_new_result():
    channel = TelegramChannel(token="x")
    channel._client_to_telegram_chat["telegram:42"] = "42"
    channel._chat_state.set_cursor("telegram:42", "chat-a")
    channel._chat_to_telegram_chat["chat-a"] = "42"

    calls: list[tuple[str, dict]] = []

    async def fake_api_call(method: str, payload: dict):
        calls.append((method, payload))
        return {"ok": True, "result": True}

    channel._api_call = fake_api_call  # type: ignore[method-assign]
    mailbox = FakeMailbox(
        [
            Envelope(
                sender="agent@main",
                recipient="channel@telegram",
                content='{"name":"new","ok":true,"chat_id":"chat-b"}',
                content_type=MessageType.COMMAND_RESULT,
                chat_id="chat-a",
                metadata={"routing": {"client_id": "telegram:42", "chat_id": "chat-a"}},
            ),
        ]
    )

    task = asyncio.create_task(channel._forward_replies(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert channel._chat_state.get_cursor("telegram:42") == "chat-b"
    assert "chat-a" not in channel._chat_to_telegram_chat
    assert channel._chat_to_telegram_chat["chat-b"] == "42"
    assert calls == [
        (
            "sendMessage",
            {"chat_id": "42", "text": '{"name":"new","ok":true,"chat_id":"chat-b"}'},
        )
    ]
