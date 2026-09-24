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

from bos.core.agent import AgentResult, ChatCommit, ChatStore, Message, MessageContent

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

    mcp_tools_raw = cfg.get("mcp_tools")
    if mcp_tools_raw is not None and not isinstance(mcp_tools_raw, (list, tuple)):
        raise ValueError(
            f"`mcp_tools` must be a list of tool names, even for one tool; got "
            f"{mcp_tools_raw!r} ({type(mcp_tools_raw).__name__}). A bare string is split "
            f"into individual characters, not treated as a single tool name — write it "
            f"as a one-item list instead."
        )
    mcp_tools = tuple(mcp_tools_raw or ())
    if "*" in mcp_tools:
        raise ValueError(
            '`mcp_tools` does not accept "*". List every tool to expose; the default '
            "is to expose none."
        )

    auth = cfg.get("auth", "subscription")
    if auth not in ("subscription", "api_key"):
        raise ValueError(f'`auth` must be "subscription" or "api_key"; got {auth!r}.')

    # Same shape as `mcp_tools` above: a bare value where a table belongs must fail
    # here, not surface as an uncontrolled TypeError from dict() (or a silently wrong
    # native_options) once a runtime actually reads it.
    native_options_raw = cfg.get("native_options")
    if native_options_raw is not None and not isinstance(native_options_raw, dict):
        raise ValueError(
            f"`native_options` must be a table of runtime-specific settings; got "
            f"{native_options_raw!r} ({type(native_options_raw).__name__}). Use "
            f'`native_options = {{ key = "value" }}`.'
        )

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
        native_options=dict(native_options_raw or {}),
    )


async def read_native_session_id(store: ChatStore, chat_id: str, *, runtime: str) -> str | None:
    """The native session this chat is bound to, or None to start a fresh one.

    Read from the metadata of the newest committed message that names *runtime*
    (BEP 19 §3.6) — no second store and no schema change. Returns None rather
    than raising for an unknown chat, a chat that predates this feature, or a
    turn whose metadata was written incompletely: the caller's correct response
    to all three is to start a new native session.
    """
    try:
        # active_only=False: a summary written over this chat must not make a
        # live vendor session unrecoverable. Returning None here means
        # abandoning that session, not merely re-reading trimmed history, so
        # the full log is scanned rather than risking a false "no session".
        messages = await store.get_messages(chat_id, active_only=False)
    except Exception:
        logger.debug("No chat %r to recover a %s session from", chat_id, runtime, exc_info=True)
        return None
    for message in reversed(messages):
        metadata = message.metadata or {}
        if metadata.get("external_runtime") != runtime:
            continue
        session_id = metadata.get("native_session_id")
        if not isinstance(session_id, str):
            return None
        # A blank or whitespace-only value is as good as absent: it can never
        # be a real vendor session id, and returning it verbatim would hand a
        # non-id back to a runtime instead of starting fresh.
        return session_id.strip() or None
    return None


async def commit_external_turn(
    store: ChatStore,
    chat_id: str,
    *,
    turn_id: str,
    user_content: MessageContent,
    response: str,
    runtime: str,
    native_session_id: str,
    native_turn_id: str | None = None,
    usage: dict[str, int] | None = None,
) -> ChatCommit:
    """Persist the user message and the final answer — and nothing else.

    Two messages per turn, not a mirror of the native transcript: the runtime
    owns its own history and compaction, and a second copy would be a second
    source of truth that BOS cannot keep correct (BEP 19 §3.7). Intra-turn tool
    activity reaches a UI through the event sink; the full transcript is read
    back from the runtime on demand.
    """
    metadata: dict[str, Any] = {
        "external_runtime": runtime,
        "native_session_id": native_session_id,
    }
    if native_turn_id is not None:
        metadata["native_turn_id"] = native_turn_id
    if usage:
        metadata["usage"] = dict(usage)
    return await store.commit_turn(
        chat_id,
        [
            Message(llm_message={"role": "user", "content": user_content or ""}, turn_id=turn_id),
            Message(
                llm_message={"role": "assistant", "content": response},
                turn_id=turn_id,
                metadata=metadata,
            ),
        ],
        turn_id=turn_id,
    )


def external_agent_result(
    *,
    output: Any,
    turn_id: str,
    usage: dict[str, int] | None,
    finish_reason: str | None,
    structured: bool = False,
) -> AgentResult:
    """An ``AgentResult`` for a turn the native harness ran.

    ``iterations`` is 1: BOS ran one turn against the runtime. How many model
    calls the runtime made inside it is its own business and is not comparable
    to a BOS agent's iteration count.
    """
    return AgentResult(
        output=output,
        structured=structured,
        iterations=1,
        usage=dict(usage or {}),
        turn_id=turn_id,
        finish_reason=finish_reason,
    )
