from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from bos.protocol import Envelope, MessageContent, MessageType, TurnEvent

from .registry import ExtensionPoint, ToolRegistry

# ── BEP 5: Shared literals ─────────────────────────────────────────────────

ToolNoiseFilter = Literal["keep_signatures", "strip_all", "keep_all"]
TokenEstimateSource = Literal["litellm", "fallback", "fallback-error"]
ToolResultStatus = Literal["success", "error", "unknown"]


@runtime_checkable
class Closeable(Protocol):
    """Opt-in cleanup contract for extensions that hold resources."""

    async def aclose(self) -> None: ...


ep_tool = ToolRegistry(
    description="""
        Tool. An async function could be invoked by llm.
        On registration, the parameters of the tool whould be provided in jsonschema format.
        for example:

        @ep_tool(
            name="echo",
            description="Echo the message.",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "str"},
                },
                "required": ["message"],
            },
        )
        async def echo(message: str) -> str:
            ...
    """
)

ep_provider = ExtensionPoint(
    description="""
        LLM provider. An async function that takes messages and returns response:LLMResponse.
        for example:

        async def my_provider(messages: list[dict], **kwargs: Any) -> LLMResponse:
            ...
    """
)

@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    # BEP 5: is_summary marks this message as a compaction boundary.
    # get_context() and get_messages(active_only=True) use the latest
    # is_summary=True message to determine the active-context window.
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


ep_chat_store = ExtensionPoint(
    description="Chat store factory. Creates ChatStore implementations for persistence + context assembly."
)


@runtime_checkable
class ChatStore(Protocol):
    # ── Turn persistence ──
    async def save_turn(
        self, chat_id: str, messages: Iterable[Message], *, turn_id: str | None = None
    ) -> None: ...

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

    # ── Metadata ──
    async def list_chats(self) -> dict[str, ChatMeta]: ...


ep_consolidator = ExtensionPoint(
    description="""
        Content consolidator. A factory that creates consolidators implementing the Consolidator protocol.
    """
)


@runtime_checkable
class Consolidator(Protocol):
    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str: ...


ep_turn_interceptor = ExtensionPoint(
    description="Turn Interceptor. A factory that creates interceptors implementing the TurnInterceptor protocol."
)


if TYPE_CHECKING:
    from .agent import TurnContext


@runtime_checkable
class TurnInterceptor(Protocol):
    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
            "error",
        ],
        context: TurnContext,
    ) -> None: ...


ep_agent = ExtensionPoint(description="Agent. A factory that creates agents implementing the Agent protocol.")

ep_preset = ExtensionPoint(description="Preset. A factory that returns a Preset object for agent configuration.")


class Preset(Protocol):
    """A preset provides agent configuration for the ``-c`` CLI option."""

    def get_agent_spec(self) -> dict[str, Any]:
        """Return an agent spec dict with keys: name, system_prompt, tools, tools_usage, plugins, etc."""
        ...


class Agent(Protocol):
    async def ask(
        self,
        chat_id: str,
        message: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: EventSink | None = None,
    ) -> str: ...


@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: TurnEvent) -> None: ...


ep_mail_route = ExtensionPoint(
    description="MailRoute. Used for message routing between agents. It should implement the MailRoute protocol."
)


@runtime_checkable
class MailBox(Protocol):
    @property
    def address(self) -> str: ...

    async def receive(self) -> Envelope: ...

    async def send(
        self,
        recipient: str,
        content: MessageContent,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def receive_nowait(self) -> Envelope | None: ...


@runtime_checkable
class MailRoute(Protocol):
    def bind(self, address: str) -> MailBox: ...
    async def deliver(self, env: Envelope) -> None: ...


ep_channel = ExtensionPoint(description="Channel. Bridges external clients to/from a bound mailbox address.")


@runtime_checkable
class Channel(Protocol):
    async def run(self, mailbox: MailBox) -> None: ...


ep_actor_command = ExtensionPoint(
    description="""Actor command handler. An async function with injectable arguments: input, env, actor, harness.
    If the command returns None, it will be treated as '(done)'.

    For example:

    @ep_actor_command(name="echo")
    async def echo(input: str) -> str:
        return input

    @ep_actor_command(name="tools")
    async def tools(actor: Any) -> dict:
        return actor._agent._get_tool_defs()
    """
)


# ── BEP 4: Plugin Architecture ─────────────────────────────────────────────

ep_plugin = ExtensionPoint(
    description="Harness plugin. A class or factory implementing HarnessPlugin."
)


@dataclass(frozen=True)
class ToolContext:
    agent_name: str
    chat_id: str
    turn_id: str
    event_sink: EventSink | None = None
    # Escape hatch for plugin/runtime-specific context that is intentionally
    # not modeled as a core ToolContext field.
    extra_data: Mapping[str, Any] = field(default_factory=dict)


class SubagentRuntime(Protocol):
    async def ask(
        self,
        role: str,
        message: str,
        *,
        parent: ToolContext,
    ) -> str:
        """Delegate to a configured subagent and return its response."""
        ...


@dataclass(frozen=True)
class PluginServices:
    bos_dir: Path
    workspace: Path
    llm: Any  # LLMClient
    consolidator: Consolidator
    subagents: SubagentRuntime
    chat_store: ChatStore | None = None


@runtime_checkable
class AgentPlugin(Protocol):
    @property
    def name(self) -> str: ...

    def register_tools(self, registry: ToolRegistry) -> None: ...

    async def get_system_prompt_section(self, context: Any) -> str | None: ...

    def get_interceptors(self) -> Sequence[Any]: ...


@runtime_checkable
class HarnessPlugin(Protocol):
    @property
    def name(self) -> str: ...

    def default_config(self) -> Mapping[str, Any]: ...

    async def setup(self, services: PluginServices) -> None: ...

    def validate_config(
        self,
        config: Mapping[str, Any],
    ) -> None: ...

    def bind(
        self,
        config: Mapping[str, Any],
    ) -> AgentPlugin: ...

    async def teardown(self) -> None: ...
