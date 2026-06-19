"""Default BackgroundLLM — wraps LLMClient with local JSON-Schema validation.

BEP 11 §3: provider-native structured output is a hint; BackgroundLLM always
validates locally. Validation failure → bounded retry → surface."""

from __future__ import annotations

import json
import logging
from typing import Any

import jsonschema

from bos.core.contract import ReasoningEffort
from bos.core.llm import LLMResponse

logger = logging.getLogger(__name__)

# Provider finish_reason values that mean the model did not return a usable,
# complete answer. These are NOT schema problems: json.loads("") raises
# "Expecting value", and the "reply ONLY with JSON" retry hint cannot repair a
# truncation, content filter, or provider error.
_UNUSABLE_FINISH_REASONS = frozenset({"length", "content_filter", "error"})


class BackgroundLLMError(ValueError):
    """BackgroundLLM could not obtain a usable, schema-valid response.

    Subclasses ValueError so existing callers that catch ValueError from
    ``ask`` keep working, while giving an accurate, catchable type for empty/
    errored/truncated completions that are not schema-validation failures."""


class DefaultBackgroundLLM:
    def __init__(self, llm, *, max_retries: int = 1) -> None:
        self._llm = llm
        self._max_retries = max_retries

    async def ask(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "reasoning_effort": reasoning_effort,
            "tools": tools,
            "metadata": metadata,
        }
        if response_schema is not None:
            kwargs["response_schema"] = response_schema
        attempt = 0
        last_error: str | None = None
        while True:
            resp = await self._llm.complete(messages, **kwargs)
            if response_schema is None:
                return resp
            content = resp.content or ""
            if resp.finish_reason in _UNUSABLE_FINISH_REASONS or not content.strip():
                # Empty / truncated / errored completion — not a schema failure.
                # Surface the true cause (finish_reason + any error text) instead
                # of mislabeling it, and skip the retry: the JSON-only hint cannot
                # fix a length cutoff, content filter, or provider error.
                detail = content.strip() or "<empty content>"
                raise BackgroundLLMError(
                    f"BackgroundLLM received no usable completion "
                    f"(finish_reason={resp.finish_reason!r}): {detail[:300]}"
                )
            try:
                parsed = json.loads(content)
                jsonschema.validate(parsed, response_schema)
                return resp
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                last_error = str(e)
                logger.warning("BackgroundLLM schema validation failed (attempt %d): %s", attempt, last_error)
                if attempt >= self._max_retries:
                    raise ValueError(f"BackgroundLLM response failed schema validation: {last_error}")
                attempt += 1
                hint = (
                    f"Previous response failed schema validation: {last_error}. "
                    f"Reply ONLY with JSON matching the schema."
                )
                messages = [*messages, {"role": "user", "content": hint}]
