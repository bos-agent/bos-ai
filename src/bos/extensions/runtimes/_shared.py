"""Config, session mapping and persistence shared by the external runtimes (BEP 19).

Plain functions and one dataclass, deliberately not a base class: the two
runtimes have nothing in common at the behavioural level, so there is no
interface here to inherit — only work neither should write twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

PERMISSIONS = ("read-only", "workspace-write", "full-access")

# Keys an external runtime honours. Anything else in the agent config is either
# dropped (below) or an error.
_KNOWN_KEYS = {
    "cwd", "permission", "system_prompt", "base_instructions", "model",
    "auth", "timeout_seconds", "mcp_tools", "native_options",
    # Written by the inheritance resolver, never by config — a hand-written one is
    # rejected at the config layer, where it can still be told apart (BEP 19 §3.4).
    "external_runtime",
}

# BOS-agent keys with no counterpart in either runtime (BEP 19 §3.9). Dropped with
# one log line rather than rejected: they arrive from [agents.*] and agent files
# that a project may legitimately share, and failing on them would make an
# external agent unable to sit alongside normal ones in the same config shape.
_DROPPED_KEYS = {
    "max_tokens", "max_iterations", "max_iteration_handoff", "shutdown_handoff",
    "tool_noise_filter", "history_attribution", "reasoning_effort",
    "plugins", "plugin-bindings", "plugin_bindings", "tools", "exclude_tools",
    "tools_usage", "description", "agent_name", "kind", "name",
}

@dataclass(frozen=True)
class ExternalAgentConfig:
    runtime: str
    cwd: Path
    permission: Literal["read-only", "workspace-write", "full-access"]
    system_prompt: str | None = None
    base_instructions: str | None = None
    model: str | None = None
    auth: Literal["subscription", "api_key"] = "subscription"
    timeout_seconds: float | None = None
    mcp_tools: tuple[str, ...] = ()
    native_options: dict[str, Any] = field(default_factory=dict)


def parse_external_config(
    cfg: dict[str, Any], *, runtime: str, workspace: Path
) -> ExternalAgentConfig:
    """Validate one external agent's config, strictly."""
    unknown = set(cfg) - _KNOWN_KEYS - _DROPPED_KEYS
    if unknown:
        raise ValueError(
            f"Unknown config key(s) for the {runtime!r} runtime: {sorted(unknown)}. "
            f"Known: {sorted(_KNOWN_KEYS)}."
        )

    if dropped := sorted(set(cfg) & _DROPPED_KEYS):
        logger.debug(
            "Runtime %r ignores BOS agent keys with no native counterpart: %s (BEP 19 §3.9)",
            runtime, dropped,
        )

    permission = cfg.get("permission")
    if permission not in PERMISSIONS:
        raise ValueError(
            f"Agent config for the {runtime!r} runtime must set `permission` to one of "
            f"{list(PERMISSIONS)}; got {permission!r}. There is no default: what an "
            f"external runtime may do to the filesystem is never inferred."
        )

    root = Path(workspace).resolve()
    cwd = (root / str(cfg.get("cwd", "."))).resolve()
    if cwd != root and root not in cwd.parents:
        raise ValueError(
            f"`cwd` resolves to {cwd}, which is outside the workspace {root}. "
            f"An external runtime is confined to the workspace."
        )

    system_prompt = cfg.get("system_prompt")
    base_instructions = cfg.get("base_instructions")
    if system_prompt is not None and base_instructions is not None:
        raise ValueError(
            "Set `system_prompt` (appended to the runtime's own prompt) or "
            "`base_instructions` (replaces it), not both."
        )

    mcp_tools = tuple(cfg.get("mcp_tools") or ())
    if "*" in mcp_tools:
        raise ValueError(
            '`mcp_tools` does not accept "*". List every tool to expose; the default '
            "is to expose none."
        )

    auth = cfg.get("auth", "subscription")
    if auth not in ("subscription", "api_key"):
        raise ValueError(f'`auth` must be "subscription" or "api_key"; got {auth!r}.')

    return ExternalAgentConfig(
        runtime=runtime,
        cwd=cwd,
        permission=permission,
        system_prompt=system_prompt,
        base_instructions=base_instructions,
        model=cfg.get("model"),
        auth=auth,
        timeout_seconds=cfg.get("timeout_seconds"),
        mcp_tools=mcp_tools,
        native_options=dict(cfg.get("native_options") or {}),
    )
