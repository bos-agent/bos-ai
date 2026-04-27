from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from bos.protocol import MessageType, TurnEvent

from .contract import EventSink, MailBox


def derive_event_sink(event_sink: EventSink | None, **defaults: Any) -> EventSink | None:
    if event_sink is None:
        return None
    return DerivedEventSink(event_sink, defaults)


class DerivedEventSink:
    def __init__(self, inner: EventSink, defaults: dict[str, Any]) -> None:
        self._inner = inner
        self._defaults = {key: value for key, value in defaults.items() if value is not None}

    async def emit(self, event: TurnEvent) -> None:
        updates = {key: value for key, value in self._defaults.items() if getattr(event, key, None) is None}
        await self._inner.emit(replace(event, **updates) if updates else event)


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
