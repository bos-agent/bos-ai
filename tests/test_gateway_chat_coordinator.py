import pytest

from bos.core import Message
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import ChannelConversationRef, ChatCoordinationError, ChatCoordinator
from bos.protocol import MessageType


@pytest.mark.asyncio
async def test_prepare_send_rejects_stale_ref_with_missing_messages():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    ref = ChannelConversationRef("tui-a", "default")
    await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "user", "content": "new message"})],
        turn_id="turn-1",
    )

    result = await coordinator.prepare_send(chat_id="chat-a", ref=ref, base_revision=0)

    assert result.ok is False
    assert result.stale is True
    assert result.current_revision == 1
    assert result.missing_messages
    assert result.missing_messages[0]["metadata"]["chat_revision"] == 1


@pytest.mark.asyncio
async def test_begin_turn_is_authoritative_race_guard():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    ref = ChannelConversationRef("tui-a", "default")
    await store.commit_turn(
        "chat-a",
        [Message(llm_message={"role": "user", "content": "already committed"})],
        turn_id="turn-1",
    )

    with pytest.raises(ChatCoordinationError, match="base revision 0"):
        await coordinator.begin_turn(chat_id="chat-a", ref=ref, actor="main", turn_id="turn-2", base_revision=0)


@pytest.mark.asyncio
async def test_active_turn_blocks_normal_messages_but_allows_up_to_date_interrupts():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    starter = ChannelConversationRef("tui-a", "default")
    other = ChannelConversationRef("telegram:daily", "tg_chat:42")
    coordinator.set_cursor(starter, "chat-a", observed_revision=0)
    coordinator.set_cursor(other, "chat-a", observed_revision=0)
    await coordinator.begin_turn(chat_id="chat-a", ref=starter, actor="main", turn_id="turn-1", base_revision=0)

    normal = await coordinator.prepare_send(chat_id="chat-a", ref=other, base_revision=0)
    interrupt = await coordinator.prepare_send(
        chat_id="chat-a",
        ref=other,
        base_revision=0,
        content_type=MessageType.INTERRUPT_ABORT,
    )

    assert normal.ok is False
    assert normal.active_turn is True
    assert interrupt.ok is True


@pytest.mark.asyncio
async def test_end_turn_clears_active_turn():
    store = InMemChatStore()
    coordinator = ChatCoordinator(store)
    ref = ChannelConversationRef("tui-a", "default")
    await coordinator.begin_turn(chat_id="chat-a", ref=ref, actor="main", turn_id="turn-1", base_revision=0)

    assert coordinator.active_turn_status("chat-a") is not None

    coordinator.end_turn(chat_id="chat-a", turn_id="turn-1", committed_revision=1)

    assert coordinator.active_turn_status("chat-a") is None
