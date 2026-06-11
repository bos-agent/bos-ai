import asyncio
import json
import uuid

import pytest
from conftest import InMemChatStore, InMemMailRoute, MessageOnlyConsolidator

from bos.core import (
    AgentActor,
    LLMResponse,
    ToolCallRequest,
    ep_provider,
)
from bos.core.agent import Agent, ChainInterceptor
from bos.plugins.subagent import SubagentAgentPlugin  # noqa: F401  registers SubagentPlugin
from bos.protocol import MessageType


def create_test_agent(**kwargs):
    kwargs.setdefault("kind", "test")
    kwargs.setdefault("agent_name", "test")
    kwargs.setdefault("chat_store", InMemChatStore())
    kwargs.setdefault("consolidator", MessageOnlyConsolidator())
    kwargs.setdefault("interceptor", ChainInterceptor())
    return Agent(**kwargs)


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
async def test_llm_response_event_carries_usage_metadata():
    suffix = uuid.uuid4().hex
    provider_name = f"test_event_sink_usage_{suffix}"
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    @ep_provider(name=provider_name)
    async def usage_provider(messages, model=None, **kwargs):
        return LLMResponse(content="done", usage=dict(usage))

    try:
        sink = CaptureSink()
        agent = create_test_agent(model=f"{provider_name}/usage")

        await agent.ask("usage-chat", "Say something.", event_sink=sink)

        ready = next(event for event in sink.events if event.detail == "response_ready")
        assert ready.metadata["usage"] == usage
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
        agent = create_test_agent(model=f"{provider_name}/actor", agent_name="main")
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
async def test_actor_run_passes_inbound_envelope_to_metadata_hooks():
    suffix = uuid.uuid4().hex
    route = InMemMailRoute()
    actor_address = f"agent@hook-{suffix}"
    sender_address = f"channel@http-{suffix}"
    actor_mailbox = route.bind(actor_address)
    sender_mailbox = route.bind(sender_address)

    class RecordingAgent:
        def __init__(self):
            self.calls = []

        async def ask(self, chat_id, message, **kwargs):
            self.calls.append((chat_id, message, kwargs))
            return "done"

    class HookActor(AgentActor):
        def _turn_metadata(self, reply_recipient, inbound_env=None):
            metadata = super()._turn_metadata(reply_recipient, inbound_env)
            metadata["target"] = inbound_env.metadata["target"]
            return metadata

        def _reply_metadata(self, reply_recipient, inbound_env=None):
            return {"target": inbound_env.metadata["target"]}

    agent = RecordingAgent()
    actor = HookActor(agent, actor_mailbox)

    task = asyncio.create_task(actor.run())
    try:
        await sender_mailbox.send(actor_address, "hello", chat_id="hook-chat", metadata={"target": "researcher"})
        reply = await asyncio.wait_for(sender_mailbox.receive(), timeout=1)

        assert reply.content == "done"
        assert reply.metadata == {"target": "researcher"}
        assert agent.calls[0][2]["ctx_metadata"]["target"] == "researcher"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
        agent = create_test_agent(model=f"{provider_name}/actor-tool", agent_name="main", tools=["EchoWithContext"])
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
