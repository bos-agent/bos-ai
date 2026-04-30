from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from bos.protocol import Envelope, MessageContent, MessageType, TurnEvent

from .registry import ExtensionPoint, ToolRegistry


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

ep_message_store = ExtensionPoint(
    description="""
        Message store. A factory that creates message stores implementing the MessageStore protocol.
    """
)


@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    is_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MessageStore(Protocol):
    async def save_messages(self, chat_id: str, messages: Iterable[Message]) -> None: ...
    async def get_messages(self, chat_id: str, original: bool = False) -> Iterable[Message]: ...
    async def save_summary(self, chat_id: str, summary: str) -> None: ...
    async def list_chats(self) -> dict[str, dict[str, Any]]: ...


ep_memory_store = ExtensionPoint(
    description="""
        Memory store. A factory that creates memory stores implementing the MemoryStore protocol.
    """
)


@runtime_checkable
class MemoryStore(Protocol):
    async def load_memory(self, key: str) -> str: ...
    async def save_memory(self, key: str, content: str) -> None: ...
    async def list_memories(self) -> dict[str, str]: ...
    async def search_memory(self, query: str) -> dict[str, str]: ...


ep_consolidator = ExtensionPoint(
    description="""
        Content consolidator. A factory that creates consolidators implementing the Consolidator protocol.
    """
)


@runtime_checkable
class Consolidator(Protocol):
    async def consolidate(self, messages: list[dict], instruction: str | None = None) -> str: ...


ep_skills_loader = ExtensionPoint(
    description="""
        Skills Loader. A factory that creates skills loaders implementing the SkillsLoader protocol.
    """
)


@dataclass
class SkillMeta:
    location: str
    name: str = ""
    description: str = ""


@runtime_checkable
class SkillsLoader(Protocol):
    async def load_skill(self, name: str) -> str: ...
    async def search_skills(self, query: str | None = None) -> dict[str, SkillMeta]: ...


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
