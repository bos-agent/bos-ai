"""BEP 5 amendment: revision-window reads on ChatStore.

P0 locks current behavior: commit_turn returns ChatCommit with a per-chat
monotonically increasing revision starting at 1. P1 adds get_revision and
get_messages_since on the same module."""

import pytest
from conftest import InMemChatStore

from bos.core.contract import Message
from bos.core.defaults.jsonl_chat_store import JsonlChatStore


def _msg(role, content):
    return Message(llm_message={"role": role, "content": content})


@pytest.fixture(params=["jsonl", "inmem"])
def store(request, tmp_path):
    if request.param == "jsonl":
        return JsonlChatStore(bos_dir=tmp_path)
    return InMemChatStore()


class TestCommitRevisionMonotonic:
    @pytest.mark.asyncio
    async def test_first_commit_is_revision_1(self, store):
        commit = await store.commit_turn("c1", [_msg("user", "hi")], turn_id="t1")
        assert commit.revision == 1

    @pytest.mark.asyncio
    async def test_revisions_increment_per_chat(self, store):
        a = await store.commit_turn("c1", [_msg("user", "a")], turn_id="t1")
        b = await store.commit_turn("c1", [_msg("user", "b")], turn_id="t2")
        c = await store.commit_turn("c2", [_msg("user", "c")], turn_id="t3")
        assert (a.revision, b.revision, c.revision) == (1, 2, 1)
