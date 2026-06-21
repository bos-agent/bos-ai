from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar, runtime_checkable

from bos.protocol import Envelope, MessageType

# Outer-ring contracts (extension registry, jobs, lifecycle, channels, mailbox
# wire, plugins). These depend *inward* on the agent core.
#
# The agent core (``bos.core.agent``) owns the contracts the Agent defines and
# depends on. They are re-exported here so existing ``from bos.core.contract
# import X`` call sites keep working — ``core.contract`` is an outer ring that
# depends inward on ``core.agent``; the dependency direction is preserved.
from .agent import (
    LLM,
    ChatCommit,
    ChatMeta,
    ChatStore,
    Consolidator,
    ContextResult,
    EventSink,
    InterceptorStage,
    LLMResponse,
    Message,
    MessageContent,
    PromptProvider,
    ReasoningEffort,
    TokenEstimate,
    TokenEstimateSource,
    ToolAttributes,
    ToolCallRequest,
    ToolContext,
    ToolNoiseFilter,
    ToolSet,
    TurnContext,
    TurnEvent,
    TurnInterceptor,
)
from .registry import ExtensionPoint, ToolRegistry

# ── BEP 5: Shared literals ─────────────────────────────────────────────────


@runtime_checkable
class Closeable(Protocol):
    """Opt-in cleanup contract for extensions that hold resources."""

    async def aclose(self) -> None: ...


ep_tool = ToolRegistry(
    name="ep_tool",
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
    """,
)

ep_provider = ExtensionPoint(
    name="ep_provider",
    description="""
        LLM provider. An async function that takes messages and returns response:LLMResponse.
        for example:

        async def my_provider(messages: list[dict], **kwargs: Any) -> LLMResponse:
            ...
    """,
)

ep_agent = ExtensionPoint(
    name="ep_agent",
    description="""
        Agent spec factory. A sync or async function returning an agent spec dict
        validatable by AgentConfig — the same shape as a [agents.<name>] TOML table.
        Invoked exactly once per bootstrap, after env loading and the [exts] defaults
        merge; [exts.ep_agent.<name>] config is passed in as keyword arguments.
        The resolved spec merges as: [agent.defaults] -> factory result -> [agents.<name>].
        for example:

        @ep_agent(name="weather_agent", description="Weather forecasting agent")
        def weather_agent(region: str = "us") -> dict:
            return {
                "system_prompt": f"You report weather for {region}.",
                "model": "gemini-2.5-flash",
                "tools": {"enabled": ["GetWeather"]},
            }
    """,
)


ep_chat_store = ExtensionPoint(
    name="ep_chat_store",
    description="Chat store factory. Creates ChatStore implementations for persistence + context assembly.",
)


ep_consolidator = ExtensionPoint(
    name="ep_consolidator",
    description="""
        Content consolidator. A factory that creates consolidators implementing the Consolidator protocol.
    """,
)


ep_turn_interceptor = ExtensionPoint(
    name="ep_turn_interceptor",
    description="Turn Interceptor. A factory that creates interceptors implementing the TurnInterceptor protocol.",
)


# ── BEP 11 §1: Lifecycle bus ───────────────────────────────────────────────

LifecycleKind = Literal["turn_complete", "session_close"]


@dataclass(frozen=True)
class LifecycleEvent:
    kind: LifecycleKind
    chat_id: str
    actor_name: str | None
    base_revision: int | None
    # The turn this event closes, for ``turn_complete``. ``None`` for
    # ``session_close`` (which spans the whole session, not one turn).
    turn_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LifecycleBus(Protocol):
    def subscribe(self, kind: LifecycleKind, handler: Callable[[LifecycleEvent], Awaitable[None]]) -> None: ...
    async def emit(self, event: LifecycleEvent) -> None: ...


# ── BEP 11 §2: Job runner ──────────────────────────────────────────────────

JobTrigger = Literal["session_close", "idle", "manual"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@runtime_checkable
class Job(Protocol):
    @property
    def key(self) -> str: ...

    async def run(self) -> None: ...


@dataclass(frozen=True)
class JobRecord:
    id: str
    key: str
    status: JobStatus
    error: str | None
    submitted_at: str
    finished_at: str | None


@runtime_checkable
class JobRunner(Protocol):
    async def start(self) -> None: ...
    async def submit(self, job: Job) -> str: ...
    def bind_trigger(
        self,
        trigger: JobTrigger,
        factory: Callable[[LifecycleEvent | None], Job | None],
    ) -> None: ...
    async def drain(self, *, timeout: float) -> None: ...
    async def status(self, job_id: str) -> JobStatus: ...
    async def list(self, *, filter: dict | None = None) -> list[JobRecord]: ...
    async def retry(self, job_id: str) -> None: ...
    async def cancel(self, job_id: str) -> None: ...


ep_job_runner = ExtensionPoint(
    name="ep_job_runner",
    description="Off-critical-path job runner implementations (BEP 11 §2).",
)


# ── BEP 11 §3: Background LLM ──────────────────────────────────────────────


@runtime_checkable
class BackgroundLLM(Protocol):
    async def ask(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


ep_mail_route = ExtensionPoint(
    name="ep_mail_route",
    description="MailRoute. Used for message routing between agents. It should implement the MailRoute protocol.",
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


ep_channel = ExtensionPoint(
    name="ep_channel",
    description="Channel. Bridges external clients to/from a bound mailbox address.",
)


SettingsT = TypeVar("SettingsT")


@runtime_checkable
class Channel(Protocol):
    @property
    def channel_id(self) -> str: ...

    @property
    def display_name(self) -> str | None: ...

    @property
    def target_actor(self) -> str: ...

    @property
    def identity_key(self) -> str | None: ...

    async def run(self, mailbox: MailBox) -> None: ...


class BaseChannel(Generic[SettingsT]):
    """Optional helper for gateway-created conversational channels.

    The public channel contract remains structural. This helper exists only to
    centralize common constructor/settings handling for adapters that want it.
    ``runtime`` is intentionally typed as ``Any`` so core does not import
    gateway-owned ``ChannelRuntimeContext``.
    """

    SettingsType: ClassVar[type[Any] | None] = None

    def __init__(
        self,
        *,
        channel_id: str,
        target_actor: str,
        settings: SettingsT,
        display_name: str | None = None,
        runtime: Any = None,
    ) -> None:
        self._channel_id = channel_id
        self._target_actor = target_actor
        self._display_name = display_name
        self._settings = settings
        self._runtime = runtime

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def display_name(self) -> str | None:
        return self._display_name

    @property
    def target_actor(self) -> str:
        return self._target_actor

    @property
    def identity_key(self) -> str | None:
        return None


# ── BEP 4: Plugin Architecture ─────────────────────────────────────────────

ep_plugin = ExtensionPoint(
    name="ep_plugin",
    description="Harness plugin. A class or factory implementing HarnessPlugin.",
)


class SubagentRuntime(Protocol):
    async def ask(
        self,
        role: str,
        message: str,
        *,
        parent: ToolContext,
        agent_cfg: dict[str, Any] | None = None,
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
    chat_store: ChatStore
    events: LifecycleBus | None = None
    jobs: JobRunner | None = None
    background_llm: BackgroundLLM | None = None


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


__all__ = [
    # ── Outer-ring contracts owned here ──
    "AgentPlugin",
    "BackgroundLLM",
    "BaseChannel",
    "Channel",
    "Closeable",
    "Envelope",
    "HarnessPlugin",
    "Job",
    "JobRecord",
    "JobRunner",
    "JobStatus",
    "JobTrigger",
    "LifecycleBus",
    "LifecycleEvent",
    "LifecycleKind",
    "MailBox",
    "MailRoute",
    "MessageType",
    "PluginServices",
    "SettingsT",
    "SubagentRuntime",
    "ep_agent",
    "ep_channel",
    "ep_chat_store",
    "ep_consolidator",
    "ep_job_runner",
    "ep_mail_route",
    "ep_plugin",
    "ep_provider",
    "ep_tool",
    "ep_turn_interceptor",
    # ── Re-exported from the agent core (bos.core.agent) ──
    "LLM",
    "ChatCommit",
    "ChatMeta",
    "ChatStore",
    "Consolidator",
    "ContextResult",
    "EventSink",
    "InterceptorStage",
    "LLMResponse",
    "Message",
    "MessageContent",
    "PromptProvider",
    "ReasoningEffort",
    "TokenEstimate",
    "TokenEstimateSource",
    "ToolAttributes",
    "ToolCallRequest",
    "ToolContext",
    "ToolNoiseFilter",
    "ToolSet",
    "TurnContext",
    "TurnEvent",
    "TurnInterceptor",
]
