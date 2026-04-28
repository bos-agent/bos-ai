import json

import pytest

from bos.core.chat_state import ChatState, ChatStateError, normalize_alias


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


def test_task_owned_chat_ids_cannot_be_client_cursors(tmp_path):
    state = ChatState(path=tmp_path / "chats.json")

    with pytest.raises(ChatStateError, match="Task-owned chat ids"):
        state.resolve_for_client("tui:host", "task:task-a:worker:abc")

    with pytest.raises(ChatStateError, match="Task-owned chat ids"):
        state.set_alias("task-a", "task:task-a:worker:abc")


def test_aliases_normalize_resolve_and_delete(tmp_path):
    state = ChatState(path=tmp_path / "chats.json")

    alias = state.set_alias(" Project X ", "chat-a")

    assert alias == "project-x"
    assert state.resolve_alias_or_id("project-x") == "chat-a"
    assert state.list_aliases() == {"project-x": "chat-a"}
    assert state.delete_alias("project x") is True
    assert state.list_aliases() == {}


def test_alias_overwrite_requires_force(tmp_path):
    state = ChatState(path=tmp_path / "chats.json")
    state.set_alias("project", "chat-a")

    with pytest.raises(ChatStateError):
        state.set_alias("project", "chat-b")

    state.set_alias("project", "chat-b", force=True)
    assert state.resolve_alias_or_id("project") == "chat-b"


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
    assert state.list_aliases() == {}


def test_separate_instances_do_not_clobber_each_other(tmp_path):
    path = tmp_path / "chats.json"
    channel_state = ChatState(path=path)
    actor_state = ChatState(path=path)

    channel_state.set_cursor("tui:a", "chat-a")
    actor_state.set_alias("project", "chat-a")
    channel_state.set_cursor("tui:a", "chat-b")

    recovered = ChatState(path=path)
    assert recovered.get_cursor("tui:a") == "chat-b"
    assert recovered.resolve_alias_or_id("project") == "chat-a"
