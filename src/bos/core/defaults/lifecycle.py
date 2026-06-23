"""Default in-process EventBus — ephemeral, type-keyed pub/sub with isolated fan-out."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from bos.core.actor import Event

logger = logging.getLogger(__name__)

_Handler = Callable[[Event], Awaitable[None]]


class DefaultEventBus:
    """In-process, ephemeral, single-producer-tolerant pub/sub (BEP 11 §1).

    Type-keyed: subscribers register for a concrete ``Event`` subclass; ``emit``
    dispatches an event to handlers registered for its class or any base
    (walking the MRO), so subscribing to ``Event`` itself taps everything.
    Handlers run sequentially and exceptions are isolated, so one failing
    handler cannot break delivery to the others."""

    def __init__(self) -> None:
        self._subscribers: dict[type[Event], list[_Handler]] = defaultdict(list)

    def subscribe[E: Event](self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None:
        self._subscribers[event_type].append(handler)  # type: ignore[arg-type]  # E is a subtype of Event

    async def emit(self, event: Event) -> None:
        for cls in type(event).__mro__:
            for handler in list(self._subscribers.get(cls, ())):  # type: ignore[arg-type]
                try:
                    await handler(event)
                except Exception:
                    logger.exception("EventBus handler raised on %r", type(event).__name__)


# Back-compat alias — historical name before BEP 13 §2.10. Tests and any
# out-of-tree importers of ``DefaultLifecycleBus`` keep resolving.
DefaultLifecycleBus = DefaultEventBus
