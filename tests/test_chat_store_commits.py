import pytest

from bos.core import Message
from bos.core.defaults.jsonl_chat_store import JsonlChatStore
from bos.extensions.chat_stores.in_memory import InMemChatStore


@pytest.mark.asyncio
async def test_inmem_commit_turn_assigns_sequential_revisions():
    store = InMemChatStore()

    first = await store.commit_turn(
        "chat-a",
        [
            Message(llm_message={"role": "user", "content": "hello"}),
            Message(llm_message={"role": "assistant", "content": "hi"}),
        ],
        turn_id="turn-1",
    )
    second = await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "user", "content": "again"})],
        turn_id="turn-2",
    )

    assert first.revision == 1
    assert second.revision == 2
    assert {message.metadata["chat_revision"] for message in first.messages} == {1}
    assert {message.metadata["chat_revision"] for message in second.messages} == {2}
    assert {message.turn_id for message in first.messages} == {"turn-1"}
    assert {message.turn_id for message in second.messages} == {"turn-2"}

    stored = await store.get_messages("chat-a", active_only=False)
    assert [message.metadata["chat_revision"] for message in stored] == [1, 1, 2]


@pytest.mark.asyncio
async def test_jsonl_commit_turn_assigns_sequential_revisions(tmp_path):
    store = JsonlChatStore(bos_dir=tmp_path)

    first = await store.commit_turn(
        "chat-a",
        [
            Message(llm_message={"role": "user", "content": "hello"}),
            Message(llm_message={"role": "assistant", "content": "hi"}),
        ],
        turn_id="turn-1",
    )
    second = await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "user", "content": "again"})],
        turn_id="turn-2",
    )

    assert first.revision == 1
    assert second.revision == 2
    stored = await store.get_messages("chat-a", active_only=False)
    assert [message.metadata["chat_revision"] for message in stored] == [1, 1, 2]
    assert [message.turn_id for message in stored] == ["turn-1", "turn-1", "turn-2"]


@pytest.mark.asyncio
async def test_save_summary_does_not_advance_chat_revision(tmp_path):
    store = JsonlChatStore(bos_dir=tmp_path)
    first = await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "user", "content": "hello"})],
        turn_id="turn-1",
    )
    await store.save_summary("chat-a", "summary")
    second = await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "user", "content": "again"})],
        turn_id="turn-2",
    )

    assert first.revision == 1
    assert second.revision == 2


@pytest.mark.asyncio
async def test_commit_turn_rejects_empty_commit():
    store = InMemChatStore()

    with pytest.raises(ValueError, match="requires at least one message"):
        await store.commit_turn("chat-a", [], turn_id="empty")
