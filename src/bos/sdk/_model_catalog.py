"""The model catalog behind ``BosApp.list_models`` — hard-coded, by design.

Vendors change their line-ups about monthly, so a table updated with them beats
asking each one on every call. Nothing is validated against it: a model or an
effort missing here still goes through ``llm_args`` untouched, and a wrong one
comes back as the provider's own error. When that happens, this table is behind.

Last checked: 2026-09-26.
"""

from __future__ import annotations

from typing import TypedDict

from bos.core import ReasoningEffort


class ModelInfo(TypedDict):
    display_name: str
    # Values for a turn's ``llm_args["reasoning_effort"]``. Empty: the model has
    # no thinking level to pick.
    efforts: list[ReasoningEffort]
    # The model the agent runs when a turn names none.
    is_default: bool


# model id → (display name, efforts)
Catalog = dict[str, tuple[str, tuple[ReasoningEffort, ...]]]

_CLAUDE: tuple[ReasoningEffort, ...] = ("low", "medium", "high", "xhigh", "max")
_OPENAI: tuple[ReasoningEffort, ...] = ("none", "low", "medium", "high", "xhigh", "max")

# BOS's own Agent, served through litellm: the models each API-key env var
# unlocks, ids as litellm routes them. Efforts are the levels the pinned litellm
# (1.102.1) delivers to the vendor as that level, which can be fewer than the
# vendor's own: it refuses Mistral an effort, sends OpenRouter's "max" as
# "xhigh", and only turns DeepSeek's thinking on or off. Re-check them with every
# litellm bump.
LITELLM_MODELS: dict[str, Catalog] = {
    "ANTHROPIC_API_KEY": {
        "anthropic/claude-fable-5-1": ("Claude Fable 5.1", _CLAUDE),
        "anthropic/claude-opus-5-5": ("Claude Opus 5.5", _CLAUDE),
        "anthropic/claude-opus-5": ("Claude Opus 5", _CLAUDE),
        "anthropic/claude-sonnet-5": ("Claude Sonnet 5", _CLAUDE),
        "anthropic/claude-haiku-4-5": ("Claude Haiku 4.5", ("low", "medium", "high")),
        "anthropic/claude-opus-4-8": ("Claude Opus 4.8", _CLAUDE),
        "anthropic/claude-sonnet-4-6": ("Claude Sonnet 4.6", ("low", "medium", "high", "max")),
    },
    # `responses/` pins OpenAI's Responses API. Without it litellm sends a turn
    # with tools and no effort to /chat/completions, which these models refuse.
    "OPENAI_API_KEY": {
        "openai/responses/gpt-6-astra": ("GPT-6 Astra", ("low", "medium", "high", "xhigh", "max")),
        "openai/responses/gpt-6-sol": ("GPT-6 Sol", _OPENAI),
        "openai/responses/gpt-6-luna": ("GPT-6 Luna", _OPENAI),
        "openai/responses/gpt-5.6-sol": ("GPT-5.6 Sol", _OPENAI),
        "openai/responses/gpt-5.6-terra": ("GPT-5.6 Terra", _OPENAI),
        "openai/responses/gpt-5.6-luna": ("GPT-5.6 Luna", _OPENAI),
    },
    "GEMINI_API_KEY": {
        "gemini/gemini-3.1-pro-preview": ("Gemini 3.1 Pro Preview", ("low", "medium", "high")),
        "gemini/gemini-3.8-flash": ("Gemini 3.8 Flash", ("low", "medium", "high")),
        "gemini/gemini-3.5-flash-lite": ("Gemini 3.5 Flash-Lite", ("minimal", "low", "medium", "high")),
    },
    "GROQ_API_KEY": {
        "groq/openai/gpt-oss-120b": ("GPT-OSS 120B", ("low", "medium", "high")),
        "groq/openai/gpt-oss-20b": ("GPT-OSS 20B", ("low", "medium", "high")),
    },
    "MISTRAL_API_KEY": {
        "mistral/mistral-medium-3-5": ("Mistral Medium 3.5", ()),
        "mistral/mistral-large-2512": ("Mistral Large 3", ()),
        "mistral/mistral-small-2603": ("Mistral Small 4", ()),
    },
    # "none" turns thinking off; any other level turns it on at DeepSeek's
    # default, which is "high".
    "DEEPSEEK_API_KEY": {
        "deepseek/deepseek-v4-pro": ("DeepSeek-V4-Pro", ("none", "high")),
        "deepseek/deepseek-flash": ("DeepSeek-V4.1-Flash", ("none", "high")),
    },
    "XAI_API_KEY": {
        "xai/grok-4.7": ("Grok 4.7", ("low", "medium", "high", "xhigh")),
        "xai/grok-4.3": ("Grok 4.3", ("none", "low", "medium", "high")),
        "xai/grok-build-0.1": ("Grok Build 0.1", ()),
    },
    # OpenRouter's own per-model `supported_efforts`, less the "max" litellm
    # sends as "xhigh".
    "OPENROUTER_API_KEY": {
        "openrouter/anthropic/claude-opus-5.5": ("Claude Opus 5.5 (OpenRouter)", ("low", "medium", "high", "xhigh")),
        "openrouter/openai/gpt-6-sol": ("GPT-6 Sol (OpenRouter)", ("none", "low", "medium", "high", "xhigh")),
        "openrouter/google/gemini-3.8-flash": ("Gemini 3.8 Flash (OpenRouter)", ("low", "medium", "high")),
        "openrouter/deepseek/deepseek-v4.1-flash": ("DeepSeek V4.1 Flash (OpenRouter)", ("low", "high")),
        "openrouter/x-ai/grok-4.7": ("Grok 4.7 (OpenRouter)", ("low", "medium", "high", "xhigh")),
        "openrouter/moonshotai/kimi-k3": ("Kimi K3 (OpenRouter)", ("low", "high")),
    },
}

# External runtimes take native names. Claude Code's are what its own model
# picker offers (the CLI's `initialize` response), whose aliases track the
# vendor's latest model; Codex's are its `model/list`.
RUNTIME_MODELS: dict[str, Catalog] = {
    "claude-code": {
        "default": ("Default (recommended)", _CLAUDE),
        "opus": ("Opus", _CLAUDE),
        "opus[1m]": ("Opus (1M context)", _CLAUDE),
        "claude-fable-5-1[1m]": ("Fable", _CLAUDE),
        "sonnet": ("Sonnet", _CLAUDE),
        "haiku": ("Haiku", ()),
    },
    "codex": {
        "gpt-5.6-sol": ("GPT-5.6-Sol", ("low", "medium", "high", "xhigh", "max", "ultra")),
        "gpt-5.6-terra": ("GPT-5.6-Terra", ("low", "medium", "high", "xhigh", "max", "ultra")),
        "gpt-5.6-luna": ("GPT-5.6-Luna", ("low", "medium", "high", "xhigh", "max")),
        "gpt-5.5": ("GPT-5.5", ("low", "medium", "high", "xhigh")),
    },
}

# What a runtime runs when neither the agent's config nor the turn names a model.
RUNTIME_DEFAULTS: dict[str, str] = {"claude-code": "default", "codex": "gpt-5.6-sol"}


def to_model_infos(catalog: Catalog, default: str | None) -> dict[str, ModelInfo]:
    """Fresh dicts, so a caller that edits the result never edits the table."""
    return {
        model: {"display_name": name, "efforts": list(efforts), "is_default": model == default}
        for model, (name, efforts) in catalog.items()
    }
