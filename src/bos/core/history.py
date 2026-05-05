from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from ._utils import _compact
from .contract import Message

TokenEstimateSource = Literal["litellm", "fallback"]


@dataclass(frozen=True)
class HistoryProjection:
    messages: list[dict[str, Any]]
    estimated_tokens: int
    model: str | None
    source: TokenEstimateSource


def project_message_history(messages: list[Message]) -> list[dict[str, Any]]:
    return [_project_message(message) for message in messages]


def estimate_message_history_tokens(messages: list[Message], *, budget_model: str | None) -> HistoryProjection:
    projected = project_message_history(messages)
    try:
        from litellm import token_counter

        estimated_tokens = int(token_counter(model=budget_model, messages=projected))
        return HistoryProjection(
            messages=projected,
            estimated_tokens=estimated_tokens,
            model=budget_model,
            source="litellm",
        )
    except Exception:
        serialized = json.dumps(projected, default=str, sort_keys=True)
        estimated_tokens = math.ceil(len(serialized) / 3) + 8 * len(projected)
        return HistoryProjection(
            messages=projected,
            estimated_tokens=estimated_tokens,
            model=budget_model,
            source="fallback",
        )


def _project_message(message: Message) -> dict[str, Any]:
    llm_message = message.llm_message
    return _compact(
        {
            "role": llm_message["role"],
            "content": _project_content(llm_message),
            "tool_calls": llm_message.get("tool_calls"),
            "tool_call_id": llm_message.get("tool_call_id"),
            "name": llm_message.get("name"),
        }
    )


def _project_content(llm_message: dict[str, Any]) -> Any:
    content = llm_message.get("content", "")
    if llm_message.get("role") == "tool" and isinstance(content, str) and len(content) > 150:
        return content[:147] + "..."
    return content
