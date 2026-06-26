"""Gateway CommandHandler — mailbox-free control plane over the ChatCoordinator."""

from datetime import datetime

import pytest

from bos.gateway.core.chat_coordinator import ChannelConversationRef
from bos.gateway.core.command_handler import CommandHandler


class FakeCoordinator:
    def __init__(self) -> None:
        self.cursors: dict[ChannelConversationRef, str] = {}
        self.revisions: dict[str, int] = {}
        self._n = 0

    def get_cursor(self, ref):
        return self.cursors.get(ref)

    def new_chat(self, ref):
        self._n += 1
        cid = f"chat-{self._n}"
        self.cursors[ref] = cid
        return cid

    def set_cursor(self, ref, chat_id, *, observed_revision):
        self.cursors[ref] = chat_id

    async def current_revision(self, chat_id):
        return self.revisions.get(chat_id, 0)


class FakeMeta:
    def __init__(self, chat_id, *, message_count=1, last_activity=None, description=""):
        self.chat_id = chat_id
        self.message_count = message_count
        self.last_activity = last_activity
        self.description = description


class FakeChatStore:
    def __init__(self, metas=()):
        self._metas = list(metas)

    async def list_chats(self):
        return {m.chat_id: m for m in self._metas}


@pytest.fixture
def ref():
    return ChannelConversationRef("ws", "conv-1")


def _handler(coord=None, store=None, retired=None):
    async def retire(actor, chat_id):
        (retired if retired is not None else []).append((actor, chat_id))

    return CommandHandler(coord or FakeCoordinator(), store or FakeChatStore(), retire)


@pytest.mark.asyncio
async def test_new_mints_chat_and_retires_old(ref):
    coord = FakeCoordinator()
    coord.cursors[ref] = "old-chat"
    retired: list[tuple[str, str]] = []
    res = await _handler(coord, retired=retired).run(ref, "/new", target_actor="main")
    assert res.ok and res.chat_id == "chat-1"
    assert coord.cursors[ref] == "chat-1"
    assert retired == [("main", "old-chat")]


@pytest.mark.asyncio
async def test_new_without_prior_cursor_does_not_retire(ref):
    coord = FakeCoordinator()
    retired: list[tuple[str, str]] = []
    res = await _handler(coord, retired=retired).run(ref, "/new", target_actor="main")
    assert res.ok and res.chat_id == "chat-1"
    assert retired == []


@pytest.mark.asyncio
async def test_resume_switches_cursor_and_retires_old(ref):
    coord = FakeCoordinator()
    coord.cursors[ref] = "old"
    coord.revisions["target"] = 5
    retired: list[tuple[str, str]] = []
    res = await _handler(coord, retired=retired).run(ref, "/resume target", target_actor="main")
    assert res.ok and res.chat_id == "target"
    assert coord.cursors[ref] == "target"
    assert retired == [("main", "old")]


@pytest.mark.asyncio
async def test_resume_requires_argument(ref):
    res = await _handler().run(ref, "/resume", target_actor="main")
    assert not res.ok and res.error and "Usage" in res.error


@pytest.mark.asyncio
async def test_chats_lists_most_recent_first(ref):
    store = FakeChatStore(
        [
            FakeMeta("old", last_activity=datetime(2026, 1, 1), description="older"),
            FakeMeta("new", last_activity=datetime(2026, 2, 1), description="newer"),
        ]
    )
    res = await _handler(store=store).run(ref, "/chats", target_actor="main")
    assert res.ok
    assert [c["chat_id"] for c in res.result] == ["new", "old"]
    assert res.result[0]["description"] == "newer"


@pytest.mark.asyncio
async def test_chats_hides_internal_subagent_chats(ref):
    from bos.core._chat_store_utils import make_internal_chat_id

    subagent_id = make_internal_chat_id("explorer", "conv-1")
    store = FakeChatStore(
        [
            FakeMeta("conv-1", last_activity=datetime(2026, 1, 1), description="user chat"),
            FakeMeta(subagent_id, last_activity=datetime(2026, 2, 1), description="subagent work"),
        ]
    )
    res = await _handler(store=store).run(ref, "/chats", target_actor="main")
    assert res.ok
    assert [c["chat_id"] for c in res.result] == ["conv-1"]


@pytest.mark.asyncio
async def test_unknown_command_is_rejected(ref):
    res = await _handler().run(ref, "/bogus x", target_actor="main")
    assert not res.ok and res.error and "Invalid command" in res.error
