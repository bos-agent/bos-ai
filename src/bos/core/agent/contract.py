from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from ._content import MessageContent
from ._utils import _as_parts

# Every contract the Agent defines or depends on. ``core.agent`` is the
# innermost ring: this module imports stdlib + the package-internal leaves
# (``._content`` / ``._utils``) only. Outer rings implement these ports and
# depend inward.

# ── Shared literals ────────────────────────────────────────────────────────

ToolNoiseFilter = Literal["strip_all", "keep_all"]
TokenEstimateSource = Literal["litellm", "fallback", "fallback-error"]
ReasoningEffort = Literal["low", "medium", "high"]
InterceptorStage = Literal[
    "prepare", "before_llm", "after_llm", "after_tool", "final_response", "max_iteration", "error"
]


# ── Turn events (the agent's output stream) ────────────────────────────────


# The TurnEvent vocabulary is an OPEN extension point: plugins emit their own
# events (e.g. event_type "task"/"plan") to join the turn lifecycle, so the
# fields stay plain ``str``. These namespace classes name the *core's own*
# canonical values so emitters and consumers reference a symbol instead of
# duplicating string literals — they are a shared reference, not a closed set.


class AgentEventType:
    """``event_type`` values the Agent itself emits."""

    turn = "turn"
    llm = "llm"
    response = "response"
    tool = "tool"


class TurnEventPhase:
    """``phase`` values: the point within an event's span."""

    start = "start"
    finish = "finish"
    fail = "fail"


class TurnEventStage:
    """``stage`` labels for the turn lifecycle (mirror ``InterceptorStage``)."""

    prepare = "prepare"
    before_llm = "before_llm"
    after_llm = "after_llm"
    after_tool = "after_tool"
    final_response = "final_response"
    max_iteration = "max_iteration"
    error = "error"


class TurnEventDetail:
    """``detail`` values the Agent emits (fine-grained sub-kind)."""

    start = "start"
    thinking = "thinking"
    tool_calls = "tool_calls"
    response_ready = "response_ready"
    final = "final"
    tool_call = "tool_call"
    tool_result = "tool_result"
    max_iteration = "max_iteration"
    error = "error"


@dataclass
class TurnEvent:
    event_type: str
    phase: str
    chat_id: str
    turn_id: str
    agent_name: str | None = None
    stage: str | None = None
    detail: str | None = None
    parent_turn_id: str | None = None
    parent_chat_id: str | None = None
    parent_agent_name: str | None = None
    tool_name: str | None = None
    content: str | None = None
    summary: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_type": self.event_type,
            "phase": self.phase,
            "chat_id": self.chat_id,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "agent_name": self.agent_name,
            "stage": self.stage,
            "detail": self.detail,
            "parent_turn_id": self.parent_turn_id,
            "parent_chat_id": self.parent_chat_id,
            "parent_agent_name": self.parent_agent_name,
            "tool_name": self.tool_name,
            "content": self.content,
            "summary": self.summary,
            "tool_calls": self.tool_calls,
            "metadata": self.metadata,
        }
        return {k: v for k, v in payload.items() if v is not None}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TurnEvent":
        timestamp = payload.get("timestamp")
        return cls(
            event_type=str(payload["event_type"]),
            phase=str(payload["phase"]),
            chat_id=str(payload["chat_id"]),
            turn_id=str(payload["turn_id"]),
            agent_name=payload.get("agent_name"),
            stage=payload.get("stage"),
            detail=payload.get("detail"),
            parent_turn_id=payload.get("parent_turn_id"),
            parent_chat_id=payload.get("parent_chat_id"),
            parent_agent_name=payload.get("parent_agent_name"),
            tool_name=payload.get("tool_name"),
            content=payload.get("content"),
            summary=payload.get("summary"),
            tool_calls=payload.get("tool_calls"),
            metadata=payload.get("metadata") or {},
            timestamp=datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else datetime.now(),
        )


# ── Chat history / context assembly ────────────────────────────────────────


@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    # is_summary marks this message as a compaction boundary. get_context() and
    # get_messages(active_only=True) use the latest is_summary=True message to
    # determine the active-context window.
    is_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenEstimate:
    count: int
    tokenizer_model: str | None
    source: TokenEstimateSource


@dataclass(frozen=True)
class ContextResult:
    """Provider-ready context assembled by get_context()."""

    messages: list[dict[str, Any]]
    source_messages: list[Message]
    estimated_tokens: int
    tokenizer_model: str | None
    estimation_source: TokenEstimateSource
    filter_mode: ToolNoiseFilter
    summary_applied: bool
    summary_message_count_excluded: int
    latest_summary: Message | None = None


@dataclass(frozen=True)
class ChatMeta:
    chat_id: str
    message_count: int
    last_activity: datetime | None
    has_summary: bool
    latest_summary_at: datetime | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatCommit:
    chat_id: str
    turn_id: str
    revision: int
    messages: list[Message]
    committed_at: datetime


@runtime_checkable
class ChatStore(Protocol):
    # ── Turn persistence ──
    async def commit_turn(self, chat_id: str, messages: Iterable[Message], *, turn_id: str) -> ChatCommit: ...

    # ── Context assembly: pure read, no consolidation ──
    async def get_context(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> ContextResult: ...

    # ── Compaction input: active + filtered but not provider-projected ──
    async def get_compaction_messages(
        self,
        chat_id: str,
        *,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> list[Message]: ...

    # ── Token estimation for diagnostics and commands ──
    async def estimate_tokens(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> TokenEstimate: ...

    # ── Compaction boundary persistence and inspection ──
    async def save_summary(self, chat_id: str, summary: str) -> None: ...
    async def get_summary(self, chat_id: str) -> Message | None: ...

    # ── Raw access ──
    async def get_messages(self, chat_id: str, *, active_only: bool = True) -> list[Message]: ...

    # ── Revision-window read (BEP 5 amendment for BEP 10 consolidation) ──
    async def get_revision(self, chat_id: str) -> int: ...
    async def get_messages_since(self, chat_id: str, *, revision: int) -> list[Message]: ...

    # ── Metadata ──
    async def list_chats(self) -> dict[str, ChatMeta]: ...


@runtime_checkable
class Consolidator(Protocol):
    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str: ...


# ── LLM completion ─────────────────────────────────────────────────────────


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


@runtime_checkable
class LLM(Protocol):
    """The model-completion capability the Agent depends on.

    The concrete client (provider routing, config) is an outer-ring adapter
    that implements this; the Agent receives it injected.
    """

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse: ...


# ── Turn state ─────────────────────────────────────────────────────────────


@dataclass
class TurnContext:
    agent_name: str
    chat_id: str
    turn_id: str
    system: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    current: list[Message] = field(default_factory=list)
    ephemeral: list[dict[str, Any]] = field(default_factory=list)
    tool_defs: list[dict[str, Any]] = field(default_factory=list)
    current_llm_response: LLMResponse | None = None
    final_content: str | None = None
    event_sink: TurnEventSink | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    current_message_projector: Callable[[Message], dict[str, Any]] | None = None

    def set_system_prompt(self, content: MessageContent) -> None:
        self.system = [{"role": "system", "content": content}]

    def add_message(self, llm_message: dict[str, Any], *, merge: bool = False, **kwargs) -> None:
        if merge and self.current and self.current[-1].llm_message["role"] == llm_message["role"]:
            parts = _as_parts(self.current[-1].llm_message["content"]) + _as_parts(llm_message["content"])
            self.current[-1].llm_message["content"] = parts
        else:
            self.current.append(Message(llm_message=llm_message, turn_id=self.turn_id, metadata=kwargs))

    def get_llm_messages(self) -> list[dict[str, Any]]:
        project = self.current_message_projector or (lambda m: m.llm_message)
        return (
            self.system
            + self.history
            + [project(m) for m in self.current]
            + [{k: v for k, v in message.items() if not k.startswith("_")} for message in self.ephemeral]
        )

    def set_ephemeral_message(self, key: str, llm_message: dict[str, Any]) -> None:
        """Set or replace a keyed ephemeral message for this turn.

        Ephemeral messages are sent to the LLM after persisted/current messages, but are not
        persisted to chat history. A stable key lets interceptors refresh dynamic context without
        accumulating stale duplicates across LLM iterations in the same turn.
        """
        self.clear_ephemeral_message(key)
        self.ephemeral.append(llm_message | {"_ephemeral_key": key})

    def clear_ephemeral_message(self, key: str) -> None:
        self.ephemeral = [message for message in self.ephemeral if message.get("_ephemeral_key") != key]

    def get_last_user_text(self) -> str:
        """Return the most-recent user message's text from this turn's `current` messages.

        Iterates `self.current` in reverse and returns the first user message's text
        content. Multimodal content (a list of parts) is flattened to its text parts
        concatenated; non-text parts are skipped. Returns "" if no user message is
        present in this turn — interceptors use this on `prepare` to read the
        incoming user message without depending on the underlying Message shape.
        """
        for message in reversed(self.current):
            llm_msg = message.llm_message
            if llm_msg.get("role") != "user":
                continue
            content = llm_msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
                )
            return str(content)
        return ""

    @property
    def final_response(self) -> str:
        return self.final_content or self.current[-1].llm_message["content"] if self.current else "(no response)"


# ── Per-turn ports the agent invokes ───────────────────────────────────────


@runtime_checkable
class TurnInterceptor(Protocol):
    async def intercept(
        self,
        stage: InterceptorStage,
        context: TurnContext,
    ) -> None: ...


@runtime_checkable
class TurnEventSink(Protocol):
    """Emit-only callback port for intra-turn ``TurnEvent``s (BEP 13 §2.10).

    Not a bus — the Agent is a pure emitter; fan-out/subscription is an outer
    concern (``HostChannelSink``)."""

    async def emit(self, event: TurnEvent) -> None: ...


@runtime_checkable
class PromptProvider(Protocol):
    """Supplies extra system-prompt sections for a turn.

    Which plugins contribute and in what order is the outer layer's job; the
    Agent just asks the provider each turn and appends the result to its base
    system prompt.
    """

    async def sections(self, context: TurnContext) -> list[str]: ...


# ── Tools ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolAttributes:
    """Per-tool attributes that core (Agent) recognizes.

    This is the typed escape hatch for evolving tool behavior: new attributes
    are added here as explicit, typed fields with defaults — core owns the keys
    and their value types, so the surface stays self-documenting and outer
    layers cannot smuggle in semantics core silently depends on. Outer layers
    populate it from their own (richer, untyped) metadata.
    """

    parallel_safe: bool = False


@runtime_checkable
class ToolSet(Protocol):
    """A resolved collection of callable tools an Agent may use.

    The core contract for tools: Agent depends only on this, not on the
    extension mechanism (``ToolRegistry``/``ExtensionPoint``) that implements
    it. Outer layers resolve the agent's allowed tools (merge, filter, plugin
    registration) and inject a value satisfying this protocol.
    """

    def has(self, name: str) -> bool: ...
    def to_openai_schema(self) -> dict[str, dict[str, Any]]: ...
    def describe_usage(self) -> dict[str, str]: ...
    def attributes(self, name: str) -> ToolAttributes: ...
    async def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> str: ...


@dataclass(frozen=True)
class ToolContext:
    agent_name: str
    chat_id: str
    turn_id: str
    event_sink: TurnEventSink | None = None
    # Escape hatch for plugin/runtime-specific context that is intentionally
    # not modeled as a core ToolContext field.
    extra_data: Mapping[str, Any] = field(default_factory=dict)
