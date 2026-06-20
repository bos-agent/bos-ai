"""Regression tests for tool-noise filtering.

The removed ``keep_signatures`` mode rewrote prior tool calls into
``[tool call: name(args) -> success]`` text appended to the *assistant*
role. The model then read that as its own prior speech and began emitting
the signatures as literal text in fresh replies (a self-reinforcing
contamination loop). The default must never inject such text.
"""

from typing import get_args

import pytest

from bos.core.contract import Message, ToolNoiseFilter
from bos.extensions.chat_stores.in_memory import InMemChatStore


def test_tool_noise_filter_modes_are_strip_all_and_keep_all():
    # keep_signatures is removed: it contaminated the assistant role.
    assert set(get_args(ToolNoiseFilter)) == {"strip_all", "keep_all"}


def _transcript_with_tool_calls() -> list[Message]:
    return [
        Message(llm_message={"role": "user", "content": "search the repo"}),
        Message(
            llm_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "GrepSearch", "arguments": {"query": "class Job"}},
                    }
                ],
            }
        ),
        Message(llm_message={"role": "tool", "tool_call_id": "c1", "content": "match found"}),
        Message(llm_message={"role": "assistant", "content": "Found it in jobs.py."}),
    ]


@pytest.mark.asyncio
async def test_default_store_filter_never_emits_tool_call_signatures():
    # No filter argument -> store uses its default mode.
    store = InMemChatStore()
    await store.commit_turn("c", _transcript_with_tool_calls(), turn_id="t1")

    result = await store.get_context("c")

    for message in result.messages:
        content = message.get("content", "")
        if isinstance(content, str):
            assert "[tool call:" not in content, f"signature leaked into: {content!r}"
        # tool traffic must not survive into the assistant-visible context
        assert message.get("role") != "tool"
