import json

import pytest

from bos.gateway.actors.chat_state import ChatState, ChatStateError, normalize_alias


def test_resolve_for_client_creates_and_persists_cursor(tmp_path):
    path = tmp_path / "chats.json"
    state = ChatState(path=path)

    chat_id = state.resolve_for_client("tui:host")

    assert chat_id
    assert ChatState(path=path).get_cursor("tui:host") == chat_id


def test_resolve_for_client_reuses_existing_cursor(tmp_path):
    state = ChatState(path=tmp_path / "chats.json")
    chat_id = state.resolve_for_client("tui:host")

    assert state.resolve_for_client("tui:host") == chat_id


def test_supplied_chat_updates_cursor(tmp_path):
    state = ChatState(path=tmp_path / "chats.json")

    assert state.resolve_for_client("tui:host", "chat-a") == "chat-a"
    assert state.get_cursor("tui:host") == "chat-a"


def test_invalid_aliases_are_rejected():
    with pytest.raises(ChatStateError):
        normalize_alias("")
    with pytest.raises(ChatStateError):
        normalize_alias("bad/alias")


def test_malformed_state_file_raises_clear_error(tmp_path):
    path = tmp_path / "chats.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ChatStateError, match="Could not read chat state"):
        ChatState(path=path).get_cursor("tui:host")


def test_state_file_shape_is_normalized(tmp_path):
    path = tmp_path / "chats.json"
    path.write_text(json.dumps({"client_cursors": {"a": "chat-a", "b": 1}, "aliases": []}), encoding="utf-8")

    state = ChatState(path=path)

    assert state.get_cursor("a") == "chat-a"


def test_separate_instances_do_not_clobber_each_other(tmp_path):
    path = tmp_path / "chats.json"
    channel_state = ChatState(path=path)
    actor_state = ChatState(path=path)

    channel_state.set_cursor("tui:a", "chat-a")
    actor_state.set_cursor("tui:b", "chat-x")
    channel_state.set_cursor("tui:a", "chat-b")

    recovered = ChatState(path=path)
    assert recovered.get_cursor("tui:a") == "chat-b"
    assert recovered.get_cursor("tui:b") == "chat-x"
