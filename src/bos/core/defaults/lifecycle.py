"""Default in-process LifecycleBus — ephemeral pub/sub with isolated fan-out."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from bos.core.contract import LifecycleEvent, LifecycleKind

logger = logging.getLogger(__name__)

_Handler = Callable[[LifecycleEvent], Awaitable[None]]


class DefaultLifecycleBus:
    """In-process, ephemeral, single-producer-tolerant pub/sub.

    Subscribers register per-kind; emit awaits each subscriber sequentially
    and isolates exceptions so one failing handler cannot break delivery to
    the others (BEP 11 §1)."""

    def __init__(self) -> None:
        self._subscribers: dict[LifecycleKind, list[_Handler]] = defaultdict(list)

    def subscribe(self, kind: LifecycleKind, handler: _Handler) -> None:
        self._subscribers[kind].append(handler)

    async def emit(self, event: LifecycleEvent) -> None:
        for handler in list(self._subscribers.get(event.kind, ())):
            try:
                await handler(event)
            except Exception:
                logger.exception("LifecycleBus handler raised on %r", event.kind)
