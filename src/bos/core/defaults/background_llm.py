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
            try:
                parsed = json.loads(resp.content or "")
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
