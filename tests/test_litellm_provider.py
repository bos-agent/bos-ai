"""The built-in litellm provider and the structured-output hint (BEP 12, #121).

``Agent.run(schema=...)`` hands providers the schema as a ``response_schema`` kwarg. litellm
has no such param: passed through, it reached most vendors as an unknown body field, the OpenAI
Responses path dropped it, and the schema never constrained the reply. So the provider sends it
as litellm's ``response_format``, which litellm maps to each vendor's native field.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import litellm
import pytest

from bos.core.agent import LLMResponse
from bos.core.defaults.litellm_provider import litellm_complete

# litellm's own types trip this pydantic warning the first time it builds a request.
pytestmark = pytest.mark.filterwarnings("ignore:Item 'summary' on TypedDict class 'ChatCompletionReasoningItem'")

_SCHEMA = {"type": "object", "properties": {"cause": {"type": "string"}}, "required": ["cause"]}
_JSON_SCHEMA = {"type": "json_schema", "json_schema": {"name": "response", "schema": _SCHEMA}}
_MESSAGES = [{"role": "user", "content": "Why did the build fail?"}]


def _capture_acompletion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> LLMResponse:
        sent.update(kwargs)
        return LLMResponse(content="{}")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return sent


@pytest.mark.parametrize(
    ("model", "response_format"),
    [
        pytest.param("anthropic/claude-sonnet-5", _JSON_SCHEMA, id="anthropic"),
        pytest.param("openai/responses/gpt-6-sol", _JSON_SCHEMA, id="openai-responses"),
        # DeepSeek's API answers json_schema with 400 "This response_format type is unavailable
        # now", though litellm's model map says it supports it.
        pytest.param("deepseek/deepseek-flash", {"type": "json_object"}, id="deepseek"),
        # Through OpenRouter, OpenRouter's API takes the request, not DeepSeek's.
        pytest.param("openrouter/deepseek/deepseek-v4.1-flash", _JSON_SCHEMA, id="openrouter-deepseek"),
    ],
)
@pytest.mark.asyncio
async def test_the_schema_hint_goes_to_litellm_as_response_format(monkeypatch, model, response_format):
    sent = _capture_acompletion(monkeypatch)

    await litellm_complete(_MESSAGES, model=model, response_schema=_SCHEMA)

    assert sent == {"model": model, "messages": _MESSAGES, "response_format": response_format}


@pytest.mark.asyncio
async def test_a_response_format_the_caller_passed_is_kept(monkeypatch):
    sent = _capture_acompletion(monkeypatch)
    explicit = {"type": "json_object"}

    await litellm_complete(
        _MESSAGES, model="anthropic/claude-sonnet-5", response_schema=_SCHEMA, response_format=explicit
    )

    assert sent == {"model": "anthropic/claude-sonnet-5", "messages": _MESSAGES, "response_format": explicit}


def _request_body(model: str, **kwargs: Any) -> dict[str, Any]:
    """The JSON body litellm sends for *model*, caught by a local server that answers 400."""
    bodies: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            bodies.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "captured"}}')

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    error: Exception | None = None
    try:
        asyncio.run(
            litellm_complete(
                _MESSAGES,
                model=model,
                api_base=f"http://127.0.0.1:{server.server_port}",
                api_key="test",
                num_retries=0,
                **kwargs,
            )
        )
    except Exception as exc:  # the 400, or whatever stopped litellm before it sent anything
        error = exc
    finally:
        server.shutdown()
        server.server_close()
    assert len(bodies) == 1, f"litellm sent {len(bodies)} requests and raised {error!r}"
    return bodies[0]


@pytest.mark.parametrize(
    ("model", "path", "expected"),
    [
        pytest.param("anthropic/claude-sonnet-5", ("output_format", "schema", "required"), ["cause"], id="anthropic"),
        pytest.param(
            "openai/responses/gpt-6-sol", ("text", "format", "schema", "required"), ["cause"], id="openai-responses"
        ),
        pytest.param(
            "gemini/gemini-3.8-flash", ("generationConfig", "response_json_schema", "required"), ["cause"], id="gemini"
        ),
        pytest.param("deepseek/deepseek-flash", ("response_format",), {"type": "json_object"}, id="deepseek"),
    ],
)
def test_litellm_puts_the_hint_where_each_vendor_reads_it(model, path, expected):
    """A vendor fact, measured on litellm 1.102.1: re-measure when a litellm bump fails it."""
    body = _request_body(model, response_schema=_SCHEMA)

    assert "response_schema" not in body
    value: Any = body
    for key in path:
        value = value[key]
    assert value == expected
