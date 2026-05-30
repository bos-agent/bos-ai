from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from bos.core.contract import ReasoningEffort, ToolNoiseFilter


class KeyedConfigs(RootModel[dict[str, dict[str, Any]]]):
    """Reusable model for named-extension configuration sections.

    Shape: ``{key: {config_key: value}}``
    """


# ── Extension configs (replaces [harness]) ──────────────────────────


class ExtensionsConfig(BaseModel):
    """Top-level ``[exts]`` section — all extension configs keyed by EP name.

    ``extra='allow'`` means newly registered ``ep_<name>`` extension points
    automatically accept configuration without schema changes.
    """

    model_config = ConfigDict(extra="allow")


# ── Tools / Plugins sub-models ──────────────────────────────────────


class ToolsConfig(BaseModel):
    """Per-agent tools configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    usages: dict[str, str] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    """Per-agent plugins enable/disable/prompts configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    prompts: dict[str, str] = Field(default_factory=dict)


# ── Agent config ────────────────────────────────────────────────────


class AgentConfig(BaseModel):
    """Agent configuration used in ``[agent.defaults]`` and ``[agents.<name>]``.

    ``extra='allow'`` — legacy keys and plugin-specific overrides are harmless
    pass-throughs that the harness merges.
    """

    model_config = ConfigDict(extra="allow")

    system_prompt: str | None = None
    model: str | None = None
    agent_name: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    max_tokens: int = 131_072
    max_iterations: int = 25
    tool_noise_filter: ToolNoiseFilter | None = None

    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    plugin_bindings: KeyedConfigs = Field(
        default_factory=lambda: KeyedConfigs.model_validate({}),
        alias="plugin-bindings",
    )


# ── Top-level sections ──────────────────────────────────────────────


class ExtConfig(BaseModel):
    """The ``[ext]`` section — selects which implementation to use per EP.

    ``extra='allow'`` so newly registered EPs automatically accept
    selection without schema changes.
    """

    model_config = ConfigDict(extra="forbid")

    consolidator: str = "_default"
    chat_store: str = "_default"
    interceptors: list[str] = Field(default_factory=list)


class PlatformConfig(BaseModel):
    """The ``[platform]`` section — shrunk to discovery/loading config only."""

    model_config = ConfigDict(extra="forbid")

    envfile: str | None = None
    envs: dict[str, str] = Field(default_factory=dict)
    extensions: list[str] = Field(default_factory=lambda: ["bos.exts", "./extensions"])
    agent_dirs: list[str] = Field(default_factory=lambda: ["./agents"])


class ActorConfig(BaseModel):
    """A named actor under ``[runtime.actors.<key>]``.

    The TOML key is the actor's identity and memory scope.
    ``agent`` selects which registered agent kind to use.
    Extra keys are passed through as agent overrides.
    """

    model_config = ConfigDict(extra="allow")

    agent: str
    display_name: str | None = None


class RuntimeConfig(BaseModel):
    """The ``[runtime]`` section — was ``[main]``."""

    model_config = ConfigDict(extra="allow")

    agent: str = "_default"
    channels: list[dict[str, Any]] = Field(default_factory=list)
    actors: dict[str, ActorConfig] = Field(default_factory=dict)


class AgentSection(BaseModel):
    """The ``[agent]`` section — agent defaults."""

    model_config = ConfigDict(extra="forbid")

    defaults: AgentConfig = Field(default_factory=AgentConfig)


class RootConfig(BaseModel):
    """Top-level config schema.

    ``extra='allow'`` preserves unknown top-level keys for forward compatibility.
    """

    model_config = ConfigDict(extra="allow")

    platform: PlatformConfig | None = None
    ext: ExtConfig | None = None
    exts: ExtensionsConfig | None = None
    agent: AgentSection | None = None
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    runtime: RuntimeConfig | None = None


# ── Validation helpers ──────────────────────────────────────────────


def validate_config(raw: dict[str, Any]) -> RootConfig:
    """Validate a raw config dict against the Pydantic schema.

    Raises :class:`pydantic.ValidationError` on invalid config.
    """
    return RootConfig.model_validate(raw)


def validate_agent_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a single agent spec and return the normalized dict.

    Used for external agent definitions loaded outside ``[agents.*]``.

    Raises :class:`ValueError` on invalid agent spec.
    """
    try:
        validated = AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return validated.model_dump(exclude_defaults=True)


def _agent_config_to_dict(cfg: AgentConfig) -> dict[str, Any]:
    """Convert an :class:`AgentConfig` to a plain dict for downstream consumers.

    Uses ``exclude_defaults=True`` so only explicitly-set fields appear.
    Nested defaults inside sub-models (e.g. ``tools.enabled``) are also excluded.
    """
    return cfg.model_dump(exclude_defaults=True)
