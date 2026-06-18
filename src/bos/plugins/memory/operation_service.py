"""L1 memory operation service — the single validated/audited/dry-run write door
for curation (BEP 10 §4). Raw agent appends bypass this service and write to L0."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from .scoped_memory import MemoryBackend, MemoryEntry, RequestedBy

MemoryOpKind = Literal["ADD", "UPDATE", "INVALIDATE", "PROMOTE", "LINK", "NOOP"]


@dataclass(frozen=True)
class MemoryOperation:
    op: MemoryOpKind
    reason: str
    source_turn_ids: list[str] = field(default_factory=list)
    target_id: str | None = None
    content: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    importance: int | None = None
    maxim_key: str | None = None
    links: list[str] | None = None
    requested_by: RequestedBy = "consolidator"


@dataclass(frozen=True)
class AuditRecord:
    op: MemoryOperation
    result: Literal["applied", "dry_run", "rejected", "noop"]
    entry_id: str | None
    at: str
    error: str | None = None


@dataclass(frozen=True)
class RecallEvent:
    at: str
    chat_id: str
    turn_id: str
    entry_id: str
    source: Literal["auto_recall", "recall_tool"]
    query: str | None = None


class MemoryOperationService(Protocol):
    async def apply(self, ops: list[MemoryOperation], *, dry_run: bool = False) -> list[AuditRecord]: ...
    async def search_candidates(self, query: str, *, top_k: int) -> list[MemoryEntry]: ...
    async def touch_last_used(self, entry_ids: list[str]) -> None: ...
    async def restore(self, entry_id: str) -> None: ...
    async def audit(self, *, filter: dict | None = None) -> list[AuditRecord]: ...


class DefaultMemoryOperationService:
    """Validates each op, applies it via the L0 backend, appends an AuditRecord.
    Applies are serialized (one scope per service instance) so writes never
    interleave; dry-run validates and audits but mutates nothing."""

    def __init__(self, backend: MemoryBackend, *, audit_path=None, maxim_keys: set[str] | None = None) -> None:
        from ._audit_log import JsonlLog

        self._backend = backend
        self._maxim_keys = maxim_keys or set()
        self._lock = asyncio.Lock()
        self._log = JsonlLog(audit_path) if audit_path is not None else None
        self._mem_audit: list[AuditRecord] = []

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    def _validate(self, op: MemoryOperation) -> str | None:
        """Return an error string if invalid, else None."""
        if op.op == "ADD" and not op.content:
            return "ADD requires content"
        # UPDATE has two flavors: entry-targeted (target_id) and maxim-targeted (maxim_key).
        if op.op == "UPDATE":
            if op.maxim_key is not None and op.target_id is not None:
                return "UPDATE: maxim_key and target_id are mutually exclusive"
            if op.maxim_key is not None:
                if op.maxim_key not in self._maxim_keys:
                    return f"UPDATE maxim_key {op.maxim_key!r} not in allowed set"
                if not op.content:
                    return "UPDATE on a maxim requires content"
            else:
                if not op.target_id:
                    return "UPDATE requires target_id (or maxim_key for maxim rewrite)"
                if op.content is None and op.tags is None and op.importance is None:
                    return "UPDATE requires at least one of content/tags/importance"
        if op.op in ("INVALIDATE", "PROMOTE", "LINK") and not op.target_id:
            return f"{op.op} requires target_id"
        if op.op == "PROMOTE":
            if not op.maxim_key:
                return "PROMOTE requires maxim_key"
            if op.maxim_key not in self._maxim_keys:
                return f"PROMOTE maxim_key {op.maxim_key!r} not in allowed set"
        if op.op == "LINK" and not op.links:
            return "LINK requires links"
        return None

    async def _record(self, op, result, entry_id, error=None) -> AuditRecord:
        rec = AuditRecord(op=op, result=result, entry_id=entry_id, at=self._now(), error=error)
        self._mem_audit.append(rec)
        if self._log is not None:
            row = {**asdict(op), "_result": result, "_entry_id": entry_id, "_at": rec.at, "_error": error}
            await self._log.append(row)
        return rec

    async def _apply_one(self, op: MemoryOperation, *, dry_run: bool) -> AuditRecord:
        err = self._validate(op)
        if err is not None:
            return await self._record(op, "rejected", None, error=err)
        if op.op == "NOOP":
            return await self._record(op, "noop", None)
        # target existence check for ops that need it (maxim-targeted UPDATE has no target_id)
        if op.op in ("UPDATE", "INVALIDATE", "PROMOTE", "LINK") and op.target_id is not None:
            if await self._backend.get_memory(op.target_id, include_invalid=True) is None:
                return await self._record(op, "rejected", op.target_id, error=f"target {op.target_id} not found")
        if dry_run:
            return await self._record(op, "dry_run", op.target_id)
        entry_id = op.target_id
        if op.op == "ADD":
            entry_id = await self._backend.ingest_memory(
                op.content, tags=op.tags, importance=op.importance or 5,
                summary=op.summary, source_turn_ids=op.source_turn_ids,
            )
        elif op.op == "UPDATE":
            if op.maxim_key is not None:
                await self._backend.set_maxim(op.maxim_key, op.content)
                entry_id = None
            else:
                await self._backend.update_memory(
                    op.target_id, content=op.content, tags=op.tags,
                    importance=op.importance, summary=op.summary, links=op.links,
                )
        elif op.op == "INVALIDATE":
            await self._backend.invalidate_memory(op.target_id, requested_by=op.requested_by)
        elif op.op == "LINK":
            existing = await self._backend.get_memory(op.target_id, include_invalid=True)
            merged = list({*(existing.metadata.get("links") or []), *op.links})
            await self._backend.update_memory(op.target_id, links=merged)
        elif op.op == "PROMOTE":
            entry = await self._backend.get_memory(op.target_id, include_invalid=True)
            gist = (op.content or entry.content).strip()
            current = await self._backend.get_maxim(op.maxim_key)
            ts = self._now()[:16].replace("T", " ")
            revised = f"{current}\n[{ts}] {gist}" if current else f"[{ts}] {gist}"
            await self._backend.set_maxim(op.maxim_key, revised)
        return await self._record(op, "applied", entry_id)

    async def apply(self, ops: list[MemoryOperation], *, dry_run: bool = False) -> list[AuditRecord]:
        async with self._lock:
            return [await self._apply_one(op, dry_run=dry_run) for op in ops]

    async def search_candidates(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        return await self._backend.search_memories(query, top_k=top_k)

    async def touch_last_used(self, entry_ids: list[str]) -> None:
        now = self._now()
        for eid in entry_ids:
            await self._backend.update_memory(eid, last_used=now)

    async def restore(self, entry_id: str) -> None:
        await self._backend.restore_memory(entry_id)

    async def audit(self, *, filter: dict | None = None) -> list[AuditRecord]:
        records = list(self._mem_audit)
        if filter:
            for key, val in filter.items():
                records = [r for r in records if getattr(r.op, key, None) == val or getattr(r, key, None) == val]
        return records
