"""Flushes a turn's recalled entry-ids to durable last_used metadata via the
L1 operation service (BEP 10 §6). The ids are supplied by the caller (the
memory plugin reads them from its own per-turn buffer), so this carries no
knowledge of how turn_complete is delivered."""

from __future__ import annotations

import logging

from .operation_service import DefaultMemoryOperationService

logger = logging.getLogger(__name__)


class RecallFlushSubscriber:
    def __init__(self, operation_service: DefaultMemoryOperationService) -> None:
        self._svc = operation_service

    async def flush(self, recalled: list[str], *, chat_id: str = "") -> None:
        if not recalled:
            return
        try:
            await self._svc.touch_last_used(recalled)
        except Exception:
            logger.exception("recall flush failed for chat=%s", chat_id)
