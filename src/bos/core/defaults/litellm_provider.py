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


def _response_format(litellm: Any, model: str, schema: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    # The agent's structured-output hint (BEP 12) as the `response_format` litellm
    # maps to each vendor's native field. litellm has no `response_schema` param:
    # passed through, it reaches most vendors as an unknown body field.
    try:
        _, provider, _, _ = litellm.get_llm_provider(
            model=model, custom_llm_provider=kwargs.get("custom_llm_provider"), api_base=kwargs.get("api_base")
        )
    except litellm.BadRequestError:
        provider = None  # acompletion names the problem
    if provider == "deepseek":
        # DeepSeek's API refuses json_schema ("This response_format type is
        # unavailable now"), though litellm's model map says it supports it. It
        # takes JSON mode; the schema itself is in the request (BEP 12 §C).
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}


@ep_provider(name="litellm")
async def litellm_complete(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    try:
        import litellm
    except ModuleNotFoundError as exc:
        # This provider registers at import time — the decorator runs whether or
        # not litellm is installed — so a base install fails here, on the first
        # call, rather than at resolution. Name the remedy instead of surfacing
        # a bare ModuleNotFoundError from the middle of a turn.
        raise ValueError(
            "The built-in 'litellm' LLM provider needs the litellm package, which is not installed. "
            "Install bos-ai[litellm], or register your own provider with @ep_provider."
        ) from exc

    try:
        normalized_messages = [_normalize_litellm_message(message) for message in messages]
    except ValueError as exc:
        return LLMResponse(content=f"Error calling default provider: {exc}", finish_reason="error")

    schema = kwargs.pop("response_schema", None)
    if schema is not None and "response_format" not in kwargs:
        kwargs["response_format"] = _response_format(litellm, model, schema, kwargs)
    raw = await litellm.acompletion(model=model, messages=normalized_messages, **kwargs)
    return _litellm_response_to_llm_response(raw)
