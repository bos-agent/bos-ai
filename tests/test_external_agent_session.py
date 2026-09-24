"""BEP 19 §3.6, §3.7: chat_id → native session, and what BOS persists."""

from __future__ import annotations

import pytest

from bos.core.agent import Message


async def _make_store(tmp_path):
    import bos.core.defaults  # noqa: F401  (registers JsonlChatStore)
    from bos.core.contract import ep_chat_store

    return await ep_chat_store.invoke(
        "JsonlChatStore", {"bos_dir": str(tmp_path), "workspace_dir": str(tmp_path)}
    )


@pytest.mark.asyncio
async def test_a_committed_turn_is_two_messages_carrying_the_session_id(tmp_path):
    from bos.extensions.runtimes._shared import commit_external_turn

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="do it", response="done",
        runtime="codex", native_session_id="thread_abc",
        native_turn_id="turn_1", usage={"input_tokens": 10},
    )

    messages = await store.get_messages("chat-1")
    assert [m.llm_message["role"] for m in messages] == ["user", "assistant"]
    assert messages[1].llm_message["content"] == "done"
    assert messages[1].metadata["external_runtime"] == "codex"
    assert messages[1].metadata["native_session_id"] == "thread_abc"
    assert messages[1].metadata["usage"] == {"input_tokens": 10}


@pytest.mark.asyncio
async def test_the_session_id_round_trips(tmp_path):
    from bos.extensions.runtimes._shared import commit_external_turn, read_native_session_id

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="a", response="b",
        runtime="codex", native_session_id="thread_abc",
    )
    assert await read_native_session_id(store, "chat-1", runtime="codex") == "thread_abc"


@pytest.mark.asyncio
async def test_the_newest_session_id_wins(tmp_path):
    from bos.extensions.runtimes._shared import commit_external_turn, read_native_session_id

    store = await _make_store(tmp_path)
    for turn, session in (("t1", "old"), ("t2", "new")):
        await commit_external_turn(
            store, "chat-1", turn_id=turn, user_content="a", response="b",
            runtime="codex", native_session_id=session,
        )
    assert await read_native_session_id(store, "chat-1", runtime="codex") == "new"


@pytest.mark.asyncio
async def test_an_unknown_chat_has_no_session(tmp_path):
    from bos.extensions.runtimes._shared import read_native_session_id

    store = await _make_store(tmp_path)
    assert await read_native_session_id(store, "never-seen", runtime="codex") is None


@pytest.mark.asyncio
async def test_a_chat_from_another_runtime_has_no_session_for_this_one(tmp_path):
    """Switching runtimes starts a new native session rather than reusing one."""
    from bos.extensions.runtimes._shared import commit_external_turn, read_native_session_id

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="a", response="b",
        runtime="codex", native_session_id="thread_abc",
    )
    assert await read_native_session_id(store, "chat-1", runtime="claude-code") is None


@pytest.mark.asyncio
async def test_malformed_metadata_returns_none_rather_than_raising(tmp_path):
    """Review Focus 2: a crash mid-commit, or a chat predating this feature."""
    from bos.extensions.runtimes._shared import read_native_session_id

    store = await _make_store(tmp_path)
    await store.commit_turn(
        "chat-1",
        [
            Message(llm_message={"role": "user", "content": "a"}, turn_id="t1"),
            Message(
                llm_message={"role": "assistant", "content": "b"},
                turn_id="t1",
                metadata={"external_runtime": "codex", "native_session_id": None},
            ),
        ],
        turn_id="t1",
    )
    assert await read_native_session_id(store, "chat-1", runtime="codex") is None


def test_agent_result_carries_usage_and_finish_reason():
    from bos.extensions.runtimes._shared import external_agent_result

    result = external_agent_result(
        output="done", turn_id="t1", usage={"input_tokens": 3}, finish_reason="completed"
    )
    assert result.output == "done"
    assert result.iterations == 1
    assert result.usage == {"input_tokens": 3}
    assert result.finish_reason == "completed"
    assert result.structured is False


# ── Branch-coverage sweep ────────────────────────────────────────────────
#
# The tests above are the brief's, verbatim. These five were added after
# walking every `if`/`try`/`for` in the three functions and checking whether
# a test would fail if the branch were deleted — it would not, for each of
# the cases below, without one of these.


@pytest.mark.asyncio
async def test_a_store_read_failure_returns_none_rather_than_raising(tmp_path):
    """The `except Exception` in read_native_session_id had no test: every
    other "no session" case reaches it via an empty or non-matching message
    list, never via the store itself raising. A corrupt chat file exercises
    the except clause directly."""
    from bos.extensions.runtimes._shared import read_native_session_id

    store = await _make_store(tmp_path)
    store._chat_path("chat-1").write_text("not valid json\n", encoding="utf-8")

    assert await read_native_session_id(store, "chat-1", runtime="codex") is None


@pytest.mark.asyncio
async def test_a_non_string_session_id_is_treated_as_malformed(tmp_path):
    """The brief's malformed-metadata test only tries native_session_id=None,
    which a plain truthiness check (no isinstance) would also turn into None
    by coincidence. A truthy non-string value is the only input that actually
    proves the isinstance(str) guard does something."""
    from bos.extensions.runtimes._shared import read_native_session_id

    store = await _make_store(tmp_path)
    await store.commit_turn(
        "chat-1",
        [
            Message(llm_message={"role": "user", "content": "a"}, turn_id="t1"),
            Message(
                llm_message={"role": "assistant", "content": "b"},
                turn_id="t1",
                metadata={"external_runtime": "codex", "native_session_id": 12345},
            ),
        ],
        turn_id="t1",
    )
    assert await read_native_session_id(store, "chat-1", runtime="codex") is None


@pytest.mark.asyncio
async def test_an_empty_session_id_is_treated_as_malformed(tmp_path):
    """Fix round 1, item 1: the guard's two halves are independently
    falsifiable. test_a_non_string_session_id_is_treated_as_malformed proves
    the isinstance(str) half; this proves the truthiness half. Dropping
    `and session_id` (old code) / the `.strip() or None` (current code) would
    let "" read back as a session id instead of None — exactly the
    silent-failure shape this function exists to prevent."""
    from bos.extensions.runtimes._shared import commit_external_turn, read_native_session_id

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="a", response="b",
        runtime="codex", native_session_id="",
    )
    assert await read_native_session_id(store, "chat-1", runtime="codex") is None


@pytest.mark.asyncio
async def test_a_whitespace_only_session_id_is_treated_as_malformed(tmp_path):
    """Fix round 1, item 3: a whitespace-only value can never be a real
    vendor session id and must be treated as absent, same as "" — proves the
    `.strip()` is load-bearing and not just cosmetic."""
    from bos.extensions.runtimes._shared import commit_external_turn, read_native_session_id

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="a", response="b",
        runtime="codex", native_session_id="   ",
    )
    assert await read_native_session_id(store, "chat-1", runtime="codex") is None


@pytest.mark.asyncio
async def test_the_session_id_survives_a_summary_written_after_the_turn(tmp_path):
    """Fix round 1, item 2: get_messages(active_only=True) (the old default)
    trims to the latest is_summary boundary. JsonlChatStore.save_summary
    appends a new, newest message with is_summary=True and no
    external_runtime metadata, so under the old default a single save_summary
    call anywhere after the turn made the assistant message carrying
    native_session_id fall outside the active window — read_native_session_id
    would wrongly return None and the caller would abandon a live vendor
    session. active_only=False keeps it recoverable."""
    from bos.extensions.runtimes._shared import commit_external_turn, read_native_session_id

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="a", response="b",
        runtime="codex", native_session_id="thread_abc",
    )
    await store.save_summary("chat-1", "summary of the conversation so far")

    assert await read_native_session_id(store, "chat-1", runtime="codex") == "thread_abc"


@pytest.mark.asyncio
async def test_omitted_native_turn_id_and_usage_are_not_recorded(tmp_path):
    """commit_external_turn's two `if` guards must withhold the keys entirely
    when the caller supplies nothing — the brief's own tests never assert
    absence, so an unconditional `metadata["usage"] = dict(usage or {})`
    would pass them just as well."""
    from bos.extensions.runtimes._shared import commit_external_turn

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content="a", response="b",
        runtime="codex", native_session_id="thread_abc",
    )
    messages = await store.get_messages("chat-1")
    assert "native_turn_id" not in messages[1].metadata
    assert "usage" not in messages[1].metadata


@pytest.mark.asyncio
async def test_falsy_user_content_is_stored_as_empty_string(tmp_path):
    """`user_content or ""` in commit_external_turn is untouched by every
    brief test, which all pass non-empty strings. An empty list is valid
    MessageContent (str | list[MessageContentPart]) and is falsy, so it is
    the realistic case for a turn with no user text (e.g. an automated
    trigger) to prove the fallback actually runs."""
    from bos.extensions.runtimes._shared import commit_external_turn

    store = await _make_store(tmp_path)
    await commit_external_turn(
        store, "chat-1", turn_id="t1", user_content=[], response="b",
        runtime="codex", native_session_id="thread_abc",
    )
    messages = await store.get_messages("chat-1")
    assert messages[0].llm_message["content"] == ""


@pytest.mark.asyncio
async def test_a_legacy_row_with_null_metadata_returns_none_rather_than_raising(tmp_path):
    """`message.metadata or {}` guards against a raw store row with an explicit
    `"metadata": null`, which JsonlChatStore's reader passes straight through:
    `raw.get("metadata", {})` only falls back to {} when the key is *absent*,
    not when it is present with value null. Without the `or {}` here, this
    would raise AttributeError on `.get` instead of returning None."""
    from bos.extensions.runtimes._shared import read_native_session_id

    store = await _make_store(tmp_path)
    store._chat_path("chat-1").write_text(
        '{"llm_message": {"role": "assistant", "content": "b"}, "turn_id": "t1", "metadata": null}\n',
        encoding="utf-8",
    )
    assert await read_native_session_id(store, "chat-1", runtime="codex") is None


def test_agent_result_defaults_usage_to_empty_dict_when_none():
    """usage is typed `dict[str, int] | None`; external_agent_result's own
    test never passes None, but `dict(usage or {})` exists specifically
    because a bare `dict(None)` raises TypeError."""
    from bos.extensions.runtimes._shared import external_agent_result

    result = external_agent_result(output="done", turn_id="t1", usage=None, finish_reason=None)
    assert result.usage == {}
