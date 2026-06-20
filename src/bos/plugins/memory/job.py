"""MemoryConsolidationJob — BEP 11 Job for off-turn memory consolidation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from bos.core.contract import ChatStore

from ._watermark import WatermarkStore
from .consolidator import ConsolidationPolicy, MemoryConsolidationRequest, MemoryConsolidator
from .operation_service import DefaultMemoryOperationService
from .scoped_memory import MemoryBackend

logger = logging.getLogger(__name__)

TriggerName = Literal["session_close", "idle", "manual"]


@dataclass
class MemoryConsolidationJob:
    actor_name: str
    chat_id: str
    base_revision: int
    trigger: TriggerName
    policy: ConsolidationPolicy
    chat_store: ChatStore
    backend: MemoryBackend
    consolidator: MemoryConsolidator
    operation_service: DefaultMemoryOperationService
    watermarks: WatermarkStore
    maxim_keys: set[str]

    @property
    def key(self) -> str:
        return f"consolidate:{self.actor_name}:{self.chat_id}:{self.base_revision}:{self.trigger}"

    async def run(self) -> None:
        watermark = await self.watermarks.get(self.chat_id)
        if self.base_revision <= watermark:
            logger.info(
                "consolidation skipped (no new turns) chat=%s rev=%d wm=%d",
                self.chat_id,
                self.base_revision,
                watermark,
            )
            return
        transcript = await self.chat_store.get_messages_since(self.chat_id, revision=watermark)
        candidates = await self.backend.search_memories("", top_k=10_000)
        active_maxims = {key: await self.backend.get_maxim(key) for key in self.maxim_keys}
        request = MemoryConsolidationRequest(
            chat_id=self.chat_id,
            actor_name=self.actor_name,
            base_revision=self.base_revision,
            trigger=self.trigger,
            transcript_window=transcript,
            raw_appends=[],
            candidate_memories=candidates,
            active_maxims=active_maxims,
            policy=self.policy,
        )
        ops = await self.consolidator.propose(request)
        # Authoritative provenance for this run: the distinct turn ids actually
        # in the consolidated window, app-derived (order-preserving), recorded on
        # every audit record for audit/reconciliation.
        window_turn_ids = list(dict.fromkeys(m.turn_id for m in transcript if m.turn_id))
        await self.operation_service.apply(ops, dry_run=not self.policy.auto_apply, window_turn_ids=window_turn_ids)
        # Only advance the watermark when ops were actually applied. A dry-run
        # mutates nothing, so burning the watermark would silently exclude these
        # turns from every future real consolidation.
        if self.policy.auto_apply:
            await self.watermarks.set(self.chat_id, self.base_revision)
