"""Agent.run + AgentResult + structured output (BEP 12)."""

from __future__ import annotations

import uuid

import pytest
from conftest import create_test_agent

from bos.core import LLMResponse, ToolCallRequest, ep_provider
from bos.core.agent import AgentResult, StructuredOutputError
from bos.core.defaults.structured_validator import JsonSchemaValidator

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _provider(fn) -> str:
    name = f"run_test_provider_{uuid.uuid4().hex}"
    ep_provider(name=name)(fn)
    return name


@pytest.mark.asyncio
async def test_run_returns_result_with_usage_and_iterations():
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content="hi there", usage={"total_tokens": 7, "prompt_tokens": 5})

    name = _provider(provider)
    try:
        agent = create_test_agent(model=f"{name}/x")
        result = await agent.run("c1", "hello")
        assert isinstance(result, AgentResult)
        assert result.output == "hi there"
        assert result.structured is False
        assert result.iterations == 1
        assert result.usage.get("total_tokens") == 7
        assert result.turn_id
    finally:
        ep_provider._extensions.pop(name, None)


@pytest.mark.asyncio
async def test_ask_still_returns_text():
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content="plain text")

    name = _provider(provider)
    try:
        agent = create_test_agent(model=f"{name}/x")
        assert await agent.ask("c1", "hello") == "plain text"
    finally:
        ep_provider._extensions.pop(name, None)


@pytest.mark.asyncio
async def test_run_usage_sums_across_iterations():
    async def provider(messages, model=None, **kwargs):
        if any(m.get("role") == "tool" for m in messages):
            return LLMResponse(content="done", usage={"total_tokens": 3})
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="t1", name="noop", arguments={})],
            finish_reason="tool_calls",
            usage={"total_tokens": 4},
        )

    name = _provider(provider)
    from bos.core.contract import ep_tool

    @ep_tool(name="noop", description="noop", parameters={"type": "object", "properties": {}})
    async def _noop(**kwargs):
        return "ok"

    try:
        agent = create_test_agent(model=f"{name}/x", tools=["noop"])
        result = await agent.run("c1", "use the tool")
        assert result.iterations == 2
        assert result.usage.get("total_tokens") == 7  # 4 + 3
    finally:
        ep_provider._extensions.pop(name, None)
        ep_tool._extensions.pop("noop", None)


@pytest.mark.asyncio
async def test_run_structured_output_validates_and_parses():
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content='{"answer": "42"}')

    name = _provider(provider)
    try:
        agent = create_test_agent(model=f"{name}/x", structured_validator=JsonSchemaValidator())
        result = await agent.run("c1", "q", schema=_SCHEMA)
        assert result.structured is True
        assert result.output == {"answer": "42"}
    finally:
        ep_provider._extensions.pop(name, None)


@pytest.mark.asyncio
async def test_run_structured_retries_then_succeeds():
    calls = {"n": 0}

    async def provider(messages, model=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(content="not json at all")
        return LLMResponse(content='{"answer": "ok"}')

    name = _provider(provider)
    try:
        agent = create_test_agent(model=f"{name}/x", structured_validator=JsonSchemaValidator())
        result = await agent.run("c1", "q", schema=_SCHEMA, max_schema_retries=1)
        assert result.structured is True
        assert result.output == {"answer": "ok"}
        assert calls["n"] == 2  # retried once
    finally:
        ep_provider._extensions.pop(name, None)


@pytest.mark.asyncio
async def test_run_structured_raises_after_retries_exhausted():
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content="never valid")

    name = _provider(provider)
    try:
        agent = create_test_agent(model=f"{name}/x", structured_validator=JsonSchemaValidator())
        with pytest.raises(StructuredOutputError):
            await agent.run("c1", "q", schema=_SCHEMA, max_schema_retries=1)
    finally:
        ep_provider._extensions.pop(name, None)
