"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

from typing import Any

from bos.core.agent import Agent, TurnContext
from bos.core.contract import Message, TurnInterceptor, ep_consolidator, ep_tool
from bos.core.harness import ChainInterceptor, ResolvedToolSet, _CompositePluginInterceptor, _PluginPromptProvider
from bos.core.llm import LLMClient
from bos.core.registry import ToolRegistry
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute  # noqa: F401
from bos.extensions.memory_stores.in_memory import InMemMemoryExtension  # noqa: F401


def resolve_test_tools(
    *,
    plugins: list[Any] | None = None,
    local_tools: ToolRegistry | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[ToolRegistry, ResolvedToolSet]:
    """Mirror the harness tool resolution for direct-construction tests:
    register plugin tools into a local registry and expose a filtered view
    over [local, global]. Returns (local_registry, resolved) so tests can
    still inspect/extend the local registry."""
    local = local_tools or ToolRegistry("_local_tools:test", "Agent-scoped local tools.")
    for plugin in plugins or []:
        plugin.register_tools(local)
    return local, ResolvedToolSet([local, ep_tool], include=include, exclude=exclude)


def dummy_turn_context() -> TurnContext:
    """A throwaway TurnContext for introspection-style calls — e.g. building the
    system prompt outside a real turn."""
    return TurnContext(agent_name="test", chat_id="test", turn_id="test")


def compose_test_interceptors(
    plugins: list[Any] | None = None, fallback: TurnInterceptor | None = None
) -> _CompositePluginInterceptor:
    """Mirror the harness interceptor assembly for direct-construction tests:
    plugin interceptors (best-effort) ahead of a fallback chain."""
    plugin_interceptors = [i for plugin in plugins or [] for i in plugin.get_interceptors()]
    return _CompositePluginInterceptor(plugin_interceptors, fallback or ChainInterceptor())


def create_test_agent(
    *,
    plugins: list[Any] | None = None,
    local_tools: ToolRegistry | None = None,
    tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
    interceptor: TurnInterceptor | None = None,
    **kwargs: Any,
) -> Agent:
    plugins = plugins or []
    _, resolved = resolve_test_tools(plugins=plugins, local_tools=local_tools, include=tools, exclude=exclude_tools)
    kwargs.setdefault("kind", "test")
    kwargs.setdefault("agent_name", "test")
    kwargs.setdefault("chat_store", InMemChatStore())
    kwargs.setdefault("consolidator", MessageOnlyConsolidator())
    kwargs.setdefault("llm", LLMClient())
    return Agent(
        tools=resolved,
        interceptor=compose_test_interceptors(plugins, interceptor),
        prompt_provider=_PluginPromptProvider(plugins),
        **kwargs,
    )


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


@ep_consolidator(name="_default")
def _default_test_consolidator(model=None, llm=None, **kwargs):
    """Default test consolidator factory — returns a MessageOnlyConsolidator."""
    return MessageOnlyConsolidator()
