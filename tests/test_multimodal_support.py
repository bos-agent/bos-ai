from __future__ import annotations

import base64
import sys
import types

import pytest
from conftest import InMemChatStore, InMemMailRoute

from bos.core import (
    Envelope,
    LLMResponse,
    Message,
    TurnContext,
    _as_parts,
)
from bos.core.actor import MessageType
from bos.core.agent import content_length, content_preview
from bos.core.defaults.consolidator import LLMConsolidator
from bos.core.defaults.litellm_provider import _normalize_litellm_message, litellm_complete


def _structured_message_content() -> list[dict[str, object]]:
    return [
        {"type": "text", "text": "Describe this image."},
        {"type": "image", "source": {"kind": "url", "value": "https://example.test/cat.png"}},
    ]


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\nfake"


class _FakeUsage:
    prompt_tokens = 7
    completion_tokens = 5
    total_tokens = 12


class _FakeChoice:
    finish_reason = "stop"
    message = types.SimpleNamespace(
        content="ok",
        tool_calls=None,
        reasoning_content=None,
        thinking_blocks=None,
    )


class _FakeLiteLLMResponse:
    choices = [_FakeChoice()]
    usage = _FakeUsage()


def test_envelope_allows_structured_message_content():
    content = _structured_message_content()

    env = Envelope(
        sender="alice",
        recipient="bob",
        content=content,  # type: ignore[arg-type]
        content_type=MessageType.MESSAGE,
    )

    assert env.content_type == MessageType.MESSAGE
    assert env.content == content


def test_envelope_rejects_structured_non_message_content():
    with pytest.raises(TypeError, match="Non-message envelopes require string content."):
        Envelope(
            sender="alice",
            recipient="bob",
            content=_structured_message_content(),  # type: ignore[arg-type]
            content_type=MessageType.COMMAND,
        )


def test_envelope_allows_path_backed_image_content_for_uploaded_server_images():
    env = Envelope(
        sender="alice",
        recipient="bob",
        content=[
            {"type": "text", "text": "Inspect this image."},
            {"type": "image", "source": {"kind": "path", "value": "/tmp/image.png"}},
        ],
        content_type=MessageType.MESSAGE,
    )

    assert env.content_type == MessageType.MESSAGE
    assert env.content[1]["source"]["kind"] == "path"


@pytest.mark.asyncio
async def test_inmem_mail_route_round_trips_structured_message_content():
    route = InMemMailRoute()
    sender = route.bind("alice")
    receiver = route.bind("bob")
    content = _structured_message_content()

    await receiver.receive_nowait()
    await sender.send("bob", content, content_type=MessageType.MESSAGE)  # type: ignore[arg-type]

    received = await receiver.receive_nowait()

    assert received is not None
    assert received.sender == "alice"
    assert received.recipient == "bob"
    assert received.content_type == MessageType.MESSAGE
    assert received.content == content


def test_as_parts_keeps_structured_message_content_unchanged():
    content = _structured_message_content()

    parts = _as_parts(content)

    assert parts == content


def test_turn_context_merge_keeps_structured_message_parts_in_order():
    context = TurnContext(agent_name="test", chat_id="thread-1", turn_id="turn-1")

    context.add_message({"role": "user", "content": [{"type": "text", "text": "first"}]})
    context.add_message(
        {
            "role": "user",
            "content": [{"type": "image", "source": {"kind": "url", "value": "https://example.test/cat.png"}}],
        },
        merge=True,
    )

    assert context.current[0].llm_message["content"] == [
        {"type": "text", "text": "first"},
        {"type": "image", "source": {"kind": "url", "value": "https://example.test/cat.png"}},
    ]


def test_content_helpers_render_structured_message_content_for_previews_and_tokens():
    content = _structured_message_content()

    assert content_preview(content) == "Describe this image. [image]"
    assert content_length(content) == len("Describe this image. [image]")


@pytest.mark.asyncio
async def test_inmem_message_store_list_chats_uses_structured_content_preview():
    store = InMemChatStore()
    content = _structured_message_content()

    await store.commit_turn(
        "thread-1",
        [
            Message(llm_message={"role": "user", "content": content}),
            Message(llm_message={"role": "assistant", "content": "processed"}),
        ],
        turn_id="seed-turn",
    )

    chats = await store.list_chats()

    assert chats["thread-1"].description == "Describe this image. [image]"
    assert chats["thread-1"].message_count == 2


@pytest.mark.asyncio
async def test_default_consolidator_preserves_structured_message_content_in_prompt():
    captured: dict[str, object] = {}

    class FakeLLM:
        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return LLMResponse(content="Structured content summary.")

    summary = await LLMConsolidator(llm=FakeLLM(), model="test/consolidator").consolidate([
        Message(llm_message={"role": "user", "content": _structured_message_content()}),
        Message(llm_message={"role": "assistant", "content": "Processed."}),
    ])

    assert summary == "Structured content summary."
    assert captured["kwargs"] == {"model": "test/consolidator"}
    assert captured["messages"][1]["content"] == _structured_message_content()


@pytest.mark.asyncio
async def test_default_provider_forwards_structured_messages_to_litellm(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_acompletion(*, model, messages, **kwargs):
        captured["model"] = model
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return _FakeLiteLLMResponse()

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=fake_acompletion))

    content = _structured_message_content()
    response = await litellm_complete(
        model="openai/gpt-4.1-mini",
        messages=[{"role": "user", "content": content}],
        temperature=0,
    )

    assert captured["model"] == "openai/gpt-4.1-mini"
    assert captured["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image."},
                {"type": "image_url", "image_url": {"url": "https://example.test/cat.png"}},
            ],
        }
    ]
    assert captured["kwargs"] == {"temperature": 0}
    assert response.content == "ok"
    assert response.usage == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}


@pytest.mark.asyncio
async def test_default_provider_converts_path_backed_images_to_data_urls(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    async def fake_acompletion(*, model, messages, **kwargs):
        captured["messages"] = messages
        return _FakeLiteLLMResponse()

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=fake_acompletion))

    image_path = tmp_path / "cat.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    response = await litellm_complete(
        model="openai/gpt-4.1-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image."},
                    {"type": "image", "source": {"kind": "path", "value": str(image_path)}},
                ],
            }
        ],
    )

    assert response.content == "ok"
    assert captured["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgpmYWtl"},
                },
            ],
        }
    ]


def test_default_provider_normalization_accepts_path_backed_image_parts(tmp_path):
    image_path = tmp_path / "cat.png"
    image_bytes = _png_bytes()
    image_path.write_bytes(image_bytes)

    normalized = _normalize_litellm_message({
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image", "source": {"kind": "path", "value": str(image_path)}},
        ],
    })

    image_part = normalized["content"][1]

    assert normalized["content"][0] == {"type": "text", "text": "Describe this image."}
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1]) == image_bytes


def test_default_provider_renders_file_parts_as_path_reference():
    normalized = _normalize_litellm_message({
        "role": "user",
        "content": [
            {"type": "text", "text": "Summarize the attachment."},
            {
                "type": "file",
                "mime_type": "application/pdf",
                "source": {"kind": "path", "value": "/tmp/uploads/abc123.pdf"},
            },
        ],
    })

    # The file part is handed to the model as a path+mime text reference so the
    # agent can resolve it with its filesystem tools — never sent as bytes.
    assert normalized["content"] == [
        {"type": "text", "text": "Summarize the attachment."},
        {"type": "text", "text": "[attachment: /tmp/uploads/abc123.pdf (application/pdf)]"},
    ]
