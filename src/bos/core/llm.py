from __future__ import annotations

import os
from typing import Any

from .agent import LLMResponse
from .contract import ep_provider


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
            provider_name, sep, model_name = model.partition("/")
            if not sep or not ep_provider.has(provider_name):
                provider_name, model_name = "litellm", model
        else:
            provider_name, model_name = "litellm", None
        params = kwargs | {"messages": messages, "model": model_name}
        return await ep_provider.invoke(provider_name, params)
