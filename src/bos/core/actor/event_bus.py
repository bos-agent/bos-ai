from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Event:
    """Marker base for platform events broadcast on an ``EventBus``.

    Domain-agnostic: the foundation owns only the marker and the bus mechanism.
    Concrete event *vocabularies* (e.g. the harness ring's ``SessionEvent``)
    subclass this and are owned by the ring that emits them.
    """


@runtime_checkable
class EventBus(Protocol):
    """Best-effort, broadcast pub/sub over platform ``Event``s.

    Type-keyed: subscribers register for a concrete ``Event`` subclass and
    receive every event of that class (or a subclass). ``emit`` is fire-and-
    forget — zero subscribers is fine, and a failing handler must not break
    delivery to the others.
    """

    def subscribe[E: Event](self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None: ...
    async def emit(self, event: Event) -> None: ...
