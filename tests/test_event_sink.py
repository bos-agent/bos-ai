import asyncio
import json
import uuid

import pytest

from bos.core import (
    AgentActor,
    AgentHarness,
    InMemMailRoute,
    LLMResponse,
    ToolCallRequest,
    ep_agent,
    ep_provider,
)
from bos.core.agent import ChainReactInterceptor, ReactAgent
from bos.core.defaults import FileSystemSkillsLoader, NaiveConsolidator
from conftest import InMemMemoryExtension, InMemMessageStore
from bos.protocol import MessageType


def create_test_agent(**kwargs):
    kwargs.setdefault("message_store", InMemMessageStore())
    kwargs.setdefault("memory", InMemMemoryExtension())
    kwargs.setdefault("consolidator", NaiveConsolidator())
    kwargs.setdefault("skills_loader", FileSystemSkillsLoader())
    kwargs.setdefault("interceptor", ChainReactInterceptor())
    return ReactAgent(**kwargs)


class CaptureSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_react_agent_emits_root_lifecycle_events():
    suffix = uuid.uuid4().hex
    provider_name = f"test_event_sink_root_{suffix}"

    @ep_provider(name=provider_name)
    async def root_provider(messages, model=None, **kwargs):
        return LLMResponse(content="done")

    try:
        sink = CaptureSink()
        agent = create_test_agent(model=f"{provider_name}/root")

        result = await agent.ask("root-chat", "Say something.", event_sink=sink)

        assert result == "done"
        assert [event.detail for event in sink.events] == ["start", "thinking", "response_ready", "final"]
        assert {event.chat_id for event in sink.events} == {"root-chat"}
        assert len({event.turn_id for event in sink.events}) == 1
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_react_agent_emits_tool_events_and_injects_event_sink():
    suffix = uuid.uuid4().hex
    provider_name = f"test_event_sink_tool_{suffix}"

    async def echo_with_context(
        text: str,
        chat_id: str,
        turn_id: str,
        event_sink=None,
    ) -> dict[str, str | bool]:
        return {
            "text": text,
            "chat_id": chat_id,
            "turn_id": turn_id,
            "has_event_sink": event_sink is not None,
        }

    @ep_provider(name=provider_name)
    async def tool_provider(messages, model=None, **kwargs):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if tool_messages:
            return LLMResponse(content=tool_messages[-1]["content"])
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="call_echo", name="EchoWithContext", arguments={"text": "hello"})],
        )

    try:
        sink = CaptureSink()
        agent = create_test_agent(model=f"{provider_name}/tool", tools=["EchoWithContext"])
        agent._local_tools(
            name="EchoWithContext",
            description="Echo the user text plus injected runtime identifiers.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )(echo_with_context)

        result = await agent.ask("tool-chat", "Use the tool.", event_sink=sink)

        assert '"has_event_sink": true' in result
        tool_events = [event for event in sink.events if event.event_type == "tool"]
        assert [event.detail for event in tool_events] == ["tool_call", "tool_result"]
        assert {event.tool_name for event in tool_events} == {"EchoWithContext"}
        assert len({event.turn_id for event in sink.events}) == 1
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_ask_subagent_emits_child_lineage_events(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"test_event_sink_subagent_{suffix}"
    manager_name = f"manager_{suffix}"
    researcher_name = f"researcher_{suffix}"

    @ep_provider(name=provider_name)
    async def scripted_provider(messages, model=None, **kwargs):
        if model == "manager":
            tool_messages = [message for message in messages if message.get("role") == "tool"]
            if tool_messages:
                return LLMResponse(content=f"Manager synthesized: {tool_messages[-1]['content']}")
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_ask_subagent",
                        name="AskSubagent",
                        arguments={"role": researcher_name, "message": "Summarize the event sink change."},
                    )
                ],
            )
        return LLMResponse(content="Researcher summary")

    try:
        ReactAgent.register(
            name=manager_name,
            description="Manager",
            model=f"{provider_name}/manager",
            tools=["AskSubagent"],
            subagents=[researcher_name],
        )
        ReactAgent.register(
            name=researcher_name,
            description="Researcher",
            model=f"{provider_name}/researcher",
            tools=[],
        )

        sink = CaptureSink()
        bos_dir = tmp_path / ".bos"
        bos_dir.mkdir()
        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
            subagent_defaults={"task_template": "--- Sub-agent Instructions ---\n{task}"},
        ) as harness:
            manager = harness.create_agent(manager_name)
            result = await manager.ask("parent-chat", "Explain the event sink refactor.", event_sink=sink)

        assert result == "Manager synthesized: Researcher summary"
        manager_turn = next(
            event for event in sink.events if event.agent_name == manager_name and event.detail == "start"
        )
        child_event = next(
            event for event in sink.events if event.agent_name == researcher_name and event.detail == "start"
        )
        assert child_event.turn_id != manager_turn.turn_id
        assert child_event.chat_id != "parent-chat"
        assert child_event.parent_turn_id == manager_turn.turn_id
        assert child_event.parent_chat_id == "parent-chat"
        assert child_event.parent_agent_name == manager_name
    finally:
        ep_provider._extensions.pop(provider_name, None)
        ep_agent._extensions.pop(manager_name, None)
        ep_agent._extensions.pop(researcher_name, None)


@pytest.mark.asyncio
async def test_actor_serializes_turn_events_to_mailbox():
    suffix = uuid.uuid4().hex
    provider_name = f"test_event_sink_actor_{suffix}"

    @ep_provider(name=provider_name)
    async def actor_provider(messages, model=None, **kwargs):
        return LLMResponse(content="actor reply")

    try:
        route = InMemMailRoute()
        actor_address = f"agent@main-{suffix}"
        sender_address = f"channel@http-{suffix}"
        actor_mailbox = route.bind(actor_address)
        sender_mailbox = route.bind(sender_address)
        agent = create_test_agent(model=f"{provider_name}/actor", name="main")
        actor = AgentActor(agent, actor_mailbox)

        task = asyncio.create_task(actor.run())
        await sender_mailbox.send(actor_address, "hello", chat_id="actor-chat")

        received = []
        for _ in range(6):
            env = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
            received.append(env)
            if env.content_type == MessageType.MESSAGE:
                break

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        turn_event_payloads = [
            json.loads(env.content) for env in received if env.content_type == MessageType.TURN_EVENT
        ]
        assert any(
            payload.get("event_type") == "llm" and payload.get("detail") == "thinking"
            for payload in turn_event_payloads
        )
        assert any(
            payload.get("event_type") == "response" and payload.get("detail") == "final"
            for payload in turn_event_payloads
        )
        assert received[-1].content == "actor reply"
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_actor_turn_event_tool_payload_uses_canonical_shape():
    suffix = uuid.uuid4().hex
    provider_name = f"test_event_sink_actor_tool_{suffix}"
    long_result = "x" * 250

    @ep_provider(name=provider_name)
    async def actor_tool_provider(messages, model=None, **kwargs):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if tool_messages:
            return LLMResponse(content="done")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_echo",
                    name="EchoWithContext",
                    arguments={"text": "hello"},
                )
            ],
        )

    async def echo_with_context(text: str, **kwargs) -> str:
        return long_result

    try:
        route = InMemMailRoute()
        actor_address = f"agent@main-{suffix}"
        sender_address = f"channel@http-{suffix}"
        actor_mailbox = route.bind(actor_address)
        sender_mailbox = route.bind(sender_address)
        agent = create_test_agent(model=f"{provider_name}/actor-tool", name="main", tools=["EchoWithContext"])
        agent._local_tools(
            name="EchoWithContext",
            description="Return a long string for tool result previews.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )(echo_with_context)
        actor = AgentActor(agent, actor_mailbox)

        task = asyncio.create_task(actor.run())
        await sender_mailbox.send(actor_address, "hello", chat_id="actor-chat")

        received = []
        for _ in range(8):
            env = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)
            received.append(env)
            if env.content_type == MessageType.MESSAGE:
                break

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        turn_event_payloads = [
            json.loads(env.content) for env in received if env.content_type == MessageType.TURN_EVENT
        ]
        tool_calls_payload = next(payload for payload in turn_event_payloads if payload.get("detail") == "tool_calls")
        tool_result_payload = next(payload for payload in turn_event_payloads if payload.get("detail") == "tool_result")

        assert tool_calls_payload["tool_calls"] == [{"name": "EchoWithContext", "arguments": {"text": "hello"}}]
        assert tool_result_payload["tool_name"] == "EchoWithContext"
        assert tool_result_payload["content"] == long_result
        assert "tool_result" not in tool_result_payload
    finally:
        ep_provider._extensions.pop(provider_name, None)
