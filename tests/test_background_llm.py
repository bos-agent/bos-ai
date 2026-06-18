"""BackgroundLLM — side-effect-free LLM call with local schema validation."""

import json

import pytest

from bos.core.llm import LLMResponse
from bos.core.defaults.background_llm import DefaultBackgroundLLM


class _StubLLM:
    """Stand-in for LLMClient that records calls and returns canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._responses.pop(0)


def _r(content):
    return LLMResponse(content=content)


SCHEMA = {
    "type": "object",
    "properties": {"op": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["op", "reason"],
}


class TestBackgroundLLM:
    @pytest.mark.asyncio
    async def test_passes_through_when_no_schema(self):
        stub = _StubLLM([_r("hello")])
        blm = DefaultBackgroundLLM(stub)
        resp = await blm.ask(messages=[{"role": "user", "content": "hi"}])
        assert resp.content == "hello"
        assert stub.calls[0]["kwargs"].get("tools") is None

    @pytest.mark.asyncio
    async def test_validates_against_schema_and_returns(self):
        good = _r(json.dumps({"op": "ADD", "reason": "ok"}))
        stub = _StubLLM([good])
        blm = DefaultBackgroundLLM(stub)
        resp = await blm.ask(messages=[{"role": "user", "content": "x"}], response_schema=SCHEMA)
        assert json.loads(resp.content)["op"] == "ADD"

    @pytest.mark.asyncio
    async def test_retries_once_on_schema_failure_then_succeeds(self):
        bad = _r("{not-json")
        good = _r(json.dumps({"op": "ADD", "reason": "ok"}))
        stub = _StubLLM([bad, good])
        blm = DefaultBackgroundLLM(stub, max_retries=1)
        resp = await blm.ask(messages=[{"role": "user", "content": "x"}], response_schema=SCHEMA)
        assert "ADD" in resp.content
        assert len(stub.calls) == 2

    @pytest.mark.asyncio
    async def test_raises_after_retries_exhausted(self):
        bad = _r("{not-json")
        stub = _StubLLM([bad, bad])
        blm = DefaultBackgroundLLM(stub, max_retries=1)
        with pytest.raises(ValueError, match="schema"):
            await blm.ask(messages=[{"role": "user", "content": "x"}], response_schema=SCHEMA)

    @pytest.mark.asyncio
    async def test_passes_model_and_kwargs_through(self):
        stub = _StubLLM([_r("ok")])
        blm = DefaultBackgroundLLM(stub)
        await blm.ask(
            messages=[{"role": "user", "content": "x"}],
            model="anthropic/claude-3", reasoning_effort="low",
            tools=[{"name": "t"}], metadata={"k": "v"},
        )
        kwargs = stub.calls[0]["kwargs"]
        assert kwargs["model"] == "anthropic/claude-3"
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["tools"] == [{"name": "t"}]
        assert kwargs["metadata"] == {"k": "v"}
