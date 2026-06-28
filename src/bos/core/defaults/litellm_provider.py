from typing import Any

from bos.core.agent import image_source_to_model_url

from .._utils import _litellm_response_to_llm_response
from ..agent import LLMResponse
from ..contract import ep_provider


def _normalize_litellm_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, list):
        return message

    normalized: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("Structured message parts must be objects.")
        part_type = part.get("type")
        if part_type == "text":
            normalized.append({"type": "text", "text": part.get("text", "")})
            continue
        if part_type == "image":
            source = part.get("source") or {}
            normalized.append({"type": "image_url", "image_url": {"url": image_source_to_model_url(source)}})
            continue
        if part_type == "file":
            # Non-image attachments are not sent to the model natively. We hand the
            # agent the absolute upload path and MIME type as text so it can resolve
            # the file with its filesystem tools (ReadFile/Grep/Glob).
            source = part.get("source") or {}
            path = source.get("value", "")
            mime_type = part.get("mime_type") or "application/octet-stream"
            normalized.append({"type": "text", "text": f"[attachment: {path} ({mime_type})]"})
            continue
        if part_type == "image_url":
            normalized.append(part)
            continue
        raise ValueError(f"Unsupported BOS content part for default provider: {part_type!r}")

    return {**message, "content": normalized}


@ep_provider(name="litellm")
async def litellm_complete(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    import litellm

    try:
        normalized_messages = [_normalize_litellm_message(message) for message in messages]
    except ValueError as exc:
        return LLMResponse(content=f"Error calling default provider: {exc}", finish_reason="error")

    raw = await litellm.acompletion(model=model, messages=normalized_messages, **kwargs)
    return _litellm_response_to_llm_response(raw)
