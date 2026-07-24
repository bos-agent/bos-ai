"""Max-iteration turns close with a context handoff, not just a static marker."""

import uuid

import pytest
from conftest import InMemChatStore, RecordingConsolidator, create_test_agent

from bos.core import LLMResponse, ToolCallRequest, ep_provider
from bos.core.agent.agent import MAX_ITERATION_CONTENT, MAX_ITERATION_HANDOFF_INSTRUCTION
from bos.core.registry import ToolRegistry


class CaptureSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _looping_tools() -> ToolRegistry:
    """A tool the fake provider calls forever, so the turn burns its budget."""
    tools = ToolRegistry(f"_loop_tools:{uuid.uuid4().hex}")

    @tools(
        name="LoopStep",
        description="Do one step of unfinished work.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def loop_step() -> str:
        return "checked docs/BEP/0001.md, still incomplete"

    return tools


def _register_looping_provider() -> str:
    provider_name = f"test_max_iteration_provider_{uuid.uuid4().hex}"

    @ep_provider(name=provider_name)
    async def looping_provider(messages, model=None, **kwargs):
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"call_{uuid.uuid4().hex}", name="LoopStep", arguments={})],
            finish_reason="tool_calls",
        )

    return provider_name


@pytest.mark.asyncio
async def test_max_iteration_response_carries_context_handoff():
    provider_name = _register_looping_provider()
    consolidator = RecordingConsolidator("**Goal** — finish BEP 1.\n**Left** — write section 3.")
    store = InMemChatStore()
    sink = CaptureSink()
    tools = _looping_tools()

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=tools,
            tools=["LoopStep"],
            chat_store=store,
            consolidator=consolidator,
            max_iterations=2,
        )
        result = await agent.ask("handoff-chat", "Finish BEP 1.", event_sink=sink)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    # The response leads with the marker, then hands off what the turn established.
    assert result.startswith(MAX_ITERATION_CONTENT)
    assert consolidator.summary in result

    # The handoff was consolidated from the turn's own context, under the
    # handoff instruction rather than the compaction default.
    assert len(consolidator.calls) == 1
    messages, instruction = consolidator.calls[0]
    assert instruction == MAX_ITERATION_HANDOFF_INSTRUCTION
    assert any(m.llm_message.get("content") == "Finish BEP 1." for m in messages)
    assert any(m.llm_message.get("role") == "tool" for m in messages)

    # It is the persisted assistant turn, so the next turn inherits the handoff.
    persisted = await store.get_messages("handoff-chat")
    assert persisted[-1].llm_message["role"] == "assistant"
    assert persisted[-1].llm_message["content"] == result

    max_iteration_events = [e for e in sink.events if e.detail == "max_iteration"]
    assert len(max_iteration_events) == 1
    event = max_iteration_events[0]
    assert event.content == result
    assert event.metadata["handoff"] is True
    assert event.metadata["iteration"] == 2
    assert event.metadata["max_iterations"] == 2


@pytest.mark.asyncio
async def test_max_iteration_falls_back_to_marker_when_consolidation_fails():
    provider_name = _register_looping_provider()
    tools = _looping_tools()
    sink = CaptureSink()

    class FailingConsolidator:
        async def consolidate(self, messages, instruction=None):
            raise RuntimeError("consolidator model unavailable")

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=tools,
            tools=["LoopStep"],
            consolidator=FailingConsolidator(),
            max_iterations=1,
        )
        result = await agent.ask("handoff-fail-chat", "Finish BEP 1.", event_sink=sink)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result == MAX_ITERATION_CONTENT
    assert [e for e in sink.events if e.detail == "max_iteration"][0].metadata["handoff"] is False


@pytest.mark.asyncio
async def test_max_iteration_handoff_can_be_disabled():
    provider_name = _register_looping_provider()
    consolidator = RecordingConsolidator("should not be used")
    tools = _looping_tools()

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=tools,
            tools=["LoopStep"],
            consolidator=consolidator,
            max_iterations=1,
            max_iteration_handoff=False,
        )
        result = await agent.ask("handoff-off-chat", "Finish BEP 1.", event_sink=None)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result == MAX_ITERATION_CONTENT
    assert consolidator.calls == []


@pytest.mark.asyncio
async def test_max_iteration_handoff_config_key_reaches_agent_kwargs():
    from bos.config.schema import AgentConfig, _agent_config_to_core_kwargs

    cfg = AgentConfig.model_validate({"max_iterations": 3, "max_iteration_handoff": False})
    kwargs = _agent_config_to_core_kwargs(cfg)

    assert kwargs["max_iterations"] == 3
    assert kwargs["max_iteration_handoff"] is False
    assert AgentConfig().max_iteration_handoff is True
