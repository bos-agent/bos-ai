from __future__ import annotations

import json
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
    """Extensible LLM client with provider routing and scoped config."""

    def __init__(self, providers_cfg: dict[str, dict[str, Any]] | None = None) -> None:
        self._providers_cfg: dict[str, dict[str, Any]] = (
            {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in providers_cfg.items() if v is not None}
            if providers_cfg is not None
            else {}
        )

    async def complete(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        if model := kwargs.get("model"):
            provider_name, model_name = model.split("/", 1)
            if not ep_provider.has(provider_name):
                provider_name, model_name = "_default", model
        else:
            provider_name, model_name = "_default", None
        params = self._providers_cfg.get(provider_name, {}) | kwargs | {"messages": messages, "model": model_name}
        return await ep_provider.invoke_async(provider_name, params)
