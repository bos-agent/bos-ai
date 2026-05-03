from typing import Any

from bos.protocol.content import image_source_to_model_url

from .._utils import _litellm_response_to_llm_response
from ..contract import ep_provider
from ..llm import LLMResponse


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
            raise ValueError("File/PDF inputs are reserved in phase 1 and are not yet supported.")
        if part_type == "image_url":
            normalized.append(part)
            continue
        raise ValueError(f"Unsupported BOS content part for default provider: {part_type!r}")

    return {**message, "content": normalized}


@ep_provider(name="_default")
async def litellm_complete(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    import litellm

    try:
        normalized_messages = [_normalize_litellm_message(message) for message in messages]
    except ValueError as exc:
        return LLMResponse(content=f"Error calling default provider: {exc}", finish_reason="error")

    raw = await litellm.acompletion(model=model, messages=normalized_messages, **kwargs)
    return _litellm_response_to_llm_response(raw)
