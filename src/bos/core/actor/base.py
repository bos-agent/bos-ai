from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from .envelope import Envelope
from .event_bus import Event, EventBus
from .mailbox import MailBox

logger = logging.getLogger(__name__)


class Actor:
    """Base for long-lived, mailbox-bound components (BEP 13 §2).

    The domain-agnostic actor runtime: it owns the message pump (poll the
    mailbox, dispatch each envelope to ``handle``), background-task lifecycle
    (``_spawn`` / ``aclose``), and event emission (``emit``). It deliberately
    knows nothing about turns, sessions, interrupts, or conversations — those
    are a *specialization*'s concern (see ``AgentActor`` in the harness ring).
    A specialization implements ``handle`` and may override ``_on_idle_tick``
    and ``aclose``.
    """

    _poll_interval: float = 0.1

    def __init__(self, mailbox: MailBox, *, event_bus: EventBus | None = None) -> None:
        self._mailbox = mailbox
        self._address = mailbox.address
        self._event_bus = event_bus
        self._tasks: set[asyncio.Task] = set()

    @property
    def address(self) -> str:
        return self._address

    async def run(self) -> None:
        """Poll the bound mailbox and dispatch each message to ``handle``.

        Runs an idle-tick hook once per loop (before polling), sleeps when the
        mailbox is empty, and drains on cancellation."""
        try:
            while True:
                await self._on_idle_tick()
                env = await self._mailbox.receive_nowait()
                if env is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                await self.handle(env)
        except asyncio.CancelledError:
            await self.aclose()
            raise

    async def handle(self, env: Envelope) -> None:
        """Process one inbound envelope. Subclasses implement this."""
        raise NotImplementedError

    async def _on_idle_tick(self) -> None:
        """Run once per loop iteration before polling (default: no-op)."""

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """Spawn a tracked background task, drained by ``aclose``."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        """Cancel and drain tracked background tasks."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def emit(self, event: Event) -> None:
        """Broadcast an event on the bus, if one is wired (best-effort).

        Mechanism only; the event *vocabulary* belongs to the specialization."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(event)
        except Exception:
            logger.exception("actor event emit raised")
