"""Default in-process EventBus — ephemeral pub/sub with isolated fan-out."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from bos.core.contract import SessionEvent, SessionEventKind

logger = logging.getLogger(__name__)

_Handler = Callable[[SessionEvent], Awaitable[None]]


class DefaultEventBus:
    """In-process, ephemeral, single-producer-tolerant pub/sub.

    Subscribers register per-kind; emit awaits each subscriber sequentially
    and isolates exceptions so one failing handler cannot break delivery to
    the others (BEP 11 §1). Kind-keyed while ``SessionEvent`` is the only
    event category; generalizes to subscribe-by-type with the second (BEP 13
    §2.10)."""

    def __init__(self) -> None:
        self._subscribers: dict[SessionEventKind, list[_Handler]] = defaultdict(list)

    def subscribe(self, kind: SessionEventKind, handler: _Handler) -> None:
        self._subscribers[kind].append(handler)

    async def emit(self, event: SessionEvent) -> None:
        for handler in list(self._subscribers.get(event.kind, ())):
            try:
                await handler(event)
            except Exception:
                logger.exception("EventBus handler raised on %r", event.kind)


# Back-compat alias — historical name before BEP 13 §2.10. Tests and any
# out-of-tree importers of ``DefaultLifecycleBus`` keep resolving.
DefaultLifecycleBus = DefaultEventBus
