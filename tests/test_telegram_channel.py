from bos.extensions.channels.telegram import (
    TELEGRAM_MESSAGE_LIMIT,
    _conversation_id_for_chat,
    _extract_inbound_message,
    _normalize_command,
    _split_message,
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
        "chat_id": 12345,
        "text": "/history recent",
        "conversation_id": "telegram:12345",
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


def test_conversation_id_for_chat():
    assert _conversation_id_for_chat(987654321) == "telegram:987654321"
