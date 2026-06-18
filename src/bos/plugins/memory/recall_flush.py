"""turn_complete subscriber that flushes the per-turn recall log to durable
last_used metadata via the L1 operation service (BEP 10 §6)."""

from __future__ import annotations

import logging

from bos.core.contract import LifecycleEvent

from .operation_service import DefaultMemoryOperationService

logger = logging.getLogger(__name__)


class RecallFlushSubscriber:
    def __init__(self, operation_service: DefaultMemoryOperationService) -> None:
        self._svc = operation_service

    async def handle(self, event: LifecycleEvent) -> None:
        recalled = list((event.payload or {}).get("recalled", []))
        if not recalled:
            return
        try:
            await self._svc.touch_last_used(recalled)
        except Exception:
            logger.exception("recall flush failed for chat=%s", event.chat_id)
