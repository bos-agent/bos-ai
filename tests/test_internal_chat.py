"""is_internal_chat — the read-time predicate that hides non-user chats."""

from __future__ import annotations

from bos.core import filter_internal_chats, is_internal_chat
from bos.core._chat_store_utils import make_internal_chat_id


def test_subagent_chat_ids_are_internal():
    sub = make_internal_chat_id("explorer", "conv-1")
    assert is_internal_chat(sub)


def test_plain_chat_ids_are_not_internal():
    for chat_id in ("conv-1", "telegram:12345", "chat-7", "main"):
        assert not is_internal_chat(chat_id)


def test_filter_internal_chats_drops_only_internal():
    sub = make_internal_chat_id("explorer", "conv-1")
    chats = {"conv-1": object(), sub: object(), "main": object()}
    kept = filter_internal_chats(chats)  # type: ignore[arg-type]  # filters by key
    assert set(kept) == {"conv-1", "main"}
