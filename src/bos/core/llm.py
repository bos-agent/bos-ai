from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .contract import ep_provider


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None

    @property
    def text(self) -> str:
        return self.content or self.reasoning_content or ""


@dataclass
class ToolCallRequest:
    """Tool-call request projected into a provider-agnostic shape."""

    id: str
    name: str
    arguments: dict[str, Any]
    metadata: dict[str, Any] | None = None

    def to_openai_call(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            },
        }


class LLMClient:
    """Extensible LLM client with provider routing via ep_provider defaults.

    Provider configuration is managed through ``[exts.ep_provider.<impl>]`` in
    config and merged into EP defaults during bootstrap. No per-instance
    provider config is needed.
    """

    def __init__(self) -> None:
        pass

    async def complete(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        if model := kwargs.get("model") or os.getenv("BOS_MODEL"):
            provider_name, model_name = model.split("/", 1)
            if not ep_provider.has(provider_name):
                provider_name, model_name = "_default", model
        else:
            provider_name, model_name = "_default", None
        params = kwargs | {"messages": messages, "model": model_name}
        return await ep_provider.invoke(provider_name, params)
