from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from bos.protocol import MessageType, TurnEvent

from .contract import AgentEventType, MailBox, TurnEventSink

logger = logging.getLogger(__name__)

HostEventHandler = Callable[[TurnEvent], Awaitable[None] | None]

# The first four are the Agent's own event types; "task"/"plan" are emitted by
# the task/plan plugins (the event vocabulary is an open extension point).
CLIENT_TURN_EVENT_TYPES: tuple[str, ...] = (
    AgentEventType.turn,
    AgentEventType.llm,
    AgentEventType.response,
    AgentEventType.tool,
    "task",
    "plan",
)


def derive_event_sink(event_sink: TurnEventSink | None, **defaults: Any) -> TurnEventSink | None:
    if event_sink is None:
        return None
    return DerivedEventSink(event_sink, defaults)


class DerivedEventSink:
    def __init__(self, inner: TurnEventSink, defaults: dict[str, Any]) -> None:
        self._inner = inner
        self._defaults = {key: value for key, value in defaults.items() if value is not None}

    async def emit(self, event: TurnEvent) -> None:
        updates = {key: value for key, value in self._defaults.items() if getattr(event, key, None) is None}
        await self._inner.emit(replace(event, **updates) if updates else event)


class HostChannelSink:
    """Opt-in pub/sub event sink. Handlers register per ``event_type``;
    ``emit()`` dispatches to all handlers registered for that type. Events
    with no registered handler drop silently — the mailbox forwarder is just
    one consumer that registers for the client-facing types.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[HostEventHandler]] = {}

    def on(self, event_type: str, handler: HostEventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event: TurnEvent) -> None:
        for handler in self._handlers.get(event.event_type, ()):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Host channel handler raised", exc_info=True)


class MailboxEventSink:
    def __init__(self, mailbox: MailBox, recipient: str) -> None:
        self._mailbox = mailbox
        self._recipient = recipient

    async def emit(self, event: TurnEvent) -> None:
        await self._mailbox.send(
            self._recipient,
            json.dumps(event.to_payload(), default=str),
            content_type=MessageType.TURN_EVENT,
            chat_id=event.chat_id,
            metadata={"turn_id": event.turn_id, "event_type": event.event_type},
        )
