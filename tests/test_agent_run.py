"""Agent.run + AgentResult + structured output (BEP 12)."""

from __future__ import annotations

import uuid

import pytest
from conftest import InMemChatStore, create_test_agent

from bos.core import LLMResponse, ToolCallRequest, ep_provider
from bos.core.agent import ABORTED_TURN_CONTENT, AbortTurn, AgentResult, StructuredOutputError
from bos.core.defaults.structured_validator import JsonSchemaValidator
from bos.core.registry import ToolRegistry

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


class _Scripted:
    """An LLM double that answers each call with the next scripted response, recording what it was sent."""

    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **kwargs) -> LLMResponse:
        self.calls.append(messages)
        return self.responses.pop(0)


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param({"reason": "user stop"}, id="a-reason-not-a-message"),
        pytest.param({"content": "more"}, id="no-role"),
        pytest.param({"role": "user"}, id="no-content"),
        pytest.param({"role": "user", "content": None}, id="content-not-message-content"),
        pytest.param({"role": "user", "content": [{"type": "bogus"}]}, id="unknown-content-part"),
        pytest.param("stop", id="not-a-dict"),
    ],
)
@pytest.mark.asyncio
async def test_run_raises_when_interrupt_returns_something_other_than_a_message(returned):
    """#113: the failure used to be caught inside the turn and returned as its answer — output
    "(error: 'role')", committed as the assistant's reply — so a host that returned a reason to
    mean "stop" saw turns that looked finished. The contract is the caller's to keep, so it raises,
    and the user's message is kept without an error reply after it."""
    store = InMemChatStore()
    agent = create_test_agent(chat_store=store, llm=_Scripted(LLMResponse(content="unused")))

    with pytest.raises(TypeError, match="AbortTurn"):
        await agent.run("c1", "hello", interrupt=lambda: returned)

    assert [m.llm_message for m in await store.get_messages("c1")] == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize(
    "content",
    ["also say banana", [{"type": "text", "text": "also say banana"}]],
    ids=["text", "parts"],
)
@pytest.mark.asyncio
async def test_a_message_interrupt_returns_is_merged_into_the_turn(content):
    llm = _Scripted(LLMResponse(content="done"))
    polls = iter([{"role": "user", "content": content}])
    agent = create_test_agent(llm=llm)

    result = await agent.run("c1", "hello", interrupt=lambda: next(polls, None))

    assert result.output == "done"
    assert llm.calls[0][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "also say banana"}],
    }


@pytest.mark.asyncio
async def test_an_aborted_turn_reports_finish_reason_aborted():
    """Aborted after a tool round, the result used to carry that round's own finish_reason
    ("tool_calls"), and None when aborted before any LLM call — so a host could not tell a stopped
    turn from a finished one by it. The external runtimes report "aborted" (BEP 19 §3.9)."""
    tools = ToolRegistry("_test_tools")

    @tools(name="Noop", description="Does nothing.", parameters={"type": "object", "properties": {}, "required": []})
    async def noop() -> str:
        return "ok"

    llm = _Scripted(
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="t1", name="Noop", arguments={})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="unused"),
    )
    polls = 0

    def interrupt() -> None:
        nonlocal polls
        polls += 1
        if polls == 2:  # the poll after the tool round
            raise AbortTurn()

    agent = create_test_agent(llm=llm, local_tools=tools, tools=["Noop"])
    result = await agent.run("c1", "hello", interrupt=interrupt)

    assert (result.output, result.finish_reason) == (ABORTED_TURN_CONTENT, "aborted")
