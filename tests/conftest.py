"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

from typing import Any

from bos.core.agent import ChainInterceptor, ReActAgent
from bos.core.contract import Message
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute  # noqa: F401
from bos.extensions.memory_stores.in_memory import InMemMemoryExtension  # noqa: F401


def create_test_agent(*, plugins: list[Any] | None = None, **kwargs: Any) -> ReActAgent:
    kwargs.setdefault("kind", "test")
    kwargs.setdefault("agent_name", "test")
    kwargs.setdefault("chat_store", InMemChatStore())
    kwargs.setdefault("consolidator", MessageOnlyConsolidator())
    kwargs.setdefault("interceptor", ChainInterceptor())
    return ReActAgent(plugins=plugins or [], **kwargs)


class RecordingConsolidator:
    """Message-based consolidator double for tests that do not exercise summarization."""

    def __init__(self, summary: str = "recorded summary") -> None:
        self.summary = summary
        self.calls: list[tuple[list[Message], str | None]] = []

    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str:
        self.calls.append((messages, instruction))
        return self.summary


class MessageOnlyConsolidator(RecordingConsolidator):
    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str:
        assert all(isinstance(message, Message) for message in messages)
        return await super().consolidate(messages, instruction)


class CloseTrackingConsolidator(RecordingConsolidator):
    def __init__(self, summary: str = "recorded summary") -> None:
        super().__init__(summary)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
