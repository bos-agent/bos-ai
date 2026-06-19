"""Markdown-file memory backend with YAML frontmatter metadata (BEP 10 §6).

Maxims live in ``maxims/`` as plain text. Memories live in ``memories/`` as a
YAML frontmatter block followed by the content body. Legacy files (tags-header
or plain content, no frontmatter) are read with defaulted metadata."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from bos.core import _flock

from .plugin import pep_memory_backend
from .scoped_memory import MemoryEntry, MemoryIndexEntry, RequestedBy

logger = logging.getLogger(__name__)

_DEFAULT_META = {
    "importance": 5,
    "valid": True,
    "invalidated_at": None,
    "invalidated_by": None,
    "last_used": None,
    "links": [],
    "source_turn_ids": [],
    "summary": None,
}


@pep_memory_backend(name="_default")
class MarkdownMemoryBackend:
    def __init__(self, store_dir: str | Path | None = None, bos_dir: str | Path | None = None) -> None:
        store_dir = Path(store_dir).expanduser() if store_dir else "memory"
        self._dir = Path(bos_dir or ".").expanduser().resolve() / store_dir
        self._maxims_dir = self._dir / "maxims"
        self._memories_dir = self._dir / "memories"
        self._maxims_dir.mkdir(parents=True, exist_ok=True)
        self._memories_dir.mkdir(parents=True, exist_ok=True)

    # ── serialization helpers ──

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read text from %s", path, exc_info=False)
            return ""

    @classmethod
    def _file_to_entry(cls, path: Path) -> MemoryEntry | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return None
        if not raw.strip():
            return None
        meta = dict(_DEFAULT_META)
        tags: list[str] = []
        created_at = datetime.fromtimestamp(path.stat().st_ctime).isoformat()
        if raw.startswith("---\n"):
            _, _, rest = raw.partition("---\n")
            fm, _, body = rest.partition("\n---\n")
            try:
                front = yaml.safe_load(fm) or {}
            except yaml.YAMLError:
                logger.warning("Bad frontmatter in %s", path, exc_info=True)
                front = {}
            if not isinstance(front, dict):
                logger.warning("Non-mapping frontmatter in %s; ignoring", path, exc_info=False)
                front = {}
            tags = list(front.pop("tags", []) or [])
            created_at = front.pop("created_at", created_at) or created_at
            for k, default in _DEFAULT_META.items():
                meta[k] = front.get(k, default)
                if meta[k] is None and default is not None:
                    meta[k] = default
            content = body.strip()
        else:  # legacy: optional "tags:" header + body
            lines = raw.splitlines()
            tags_line = lines[0] if lines and lines[0].startswith("tags:") else ""
            body = "\n".join(lines[1:]) if tags_line else raw
            tags = [t.strip() for t in tags_line.removeprefix("tags:").split(",") if t.strip()]
            content = body.strip()
        return MemoryEntry(id=path.stem, content=content, tags=tags, created_at=created_at, metadata=meta)

    @staticmethod
    def _serialize(entry: MemoryEntry) -> str:
        front = {"tags": entry.tags, "created_at": entry.created_at, **entry.metadata}
        fm = yaml.safe_dump(front, sort_keys=True, allow_unicode=True).strip()
        return f"---\n{fm}\n---\n{entry.content}\n"

    def _write_entry_unlocked(self, entry: MemoryEntry) -> None:
        """Serialize + write without taking the file lock. Callers performing a
        read-modify-write hold _flock(path) across the whole sequence and use this;
        nesting _write_entry's own lock inside that flock would self-deadlock."""
        path = self._memories_dir / f"{entry.id}.md"
        path.write_text(self._serialize(entry), encoding="utf-8")

    def _write_entry(self, entry: MemoryEntry) -> None:
        path = self._memories_dir / f"{entry.id}.md"
        with _flock(path):
            self._write_entry_unlocked(entry)

    # ── maxims ──

    async def get_maxim(self, key: str) -> str:
        return await asyncio.to_thread(self._read_text_sync, self._maxims_dir / f"{key.lower()}.md")

    async def set_maxim(self, key: str, content: str) -> None:
        path = self._maxims_dir / f"{key.lower()}.md"

        def _write() -> None:
            with _flock(path):
                path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def append_to_maxim(self, key: str, line: str, *, max_len: int | None = None) -> tuple[bool, int]:
        path = self._maxims_dir / f"{key.lower()}.md"

        def _append() -> tuple[bool, int]:
            # Read + append + write under one flock so concurrent revisers cannot
            # each read the same snapshot and clobber one another's appends.
            with _flock(path):
                current = self._read_text_sync(path)
                revised = f"{current}\n{line}" if current else line
                if max_len is not None and len(revised) > max_len:
                    return (False, len(revised))
                path.write_text(revised, encoding="utf-8")
                return (True, len(revised))

        return await asyncio.to_thread(_append)

    # ── capture + read ──

    async def ingest_memory(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: int = 5,
        summary: str | None = None,
        source_turn_ids: list[str] | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex[:12]
        meta = dict(_DEFAULT_META)
        meta["importance"] = importance
        meta["summary"] = summary
        meta["source_turn_ids"] = list(source_turn_ids or [])
        entry = MemoryEntry(
            id=entry_id,
            content=content,
            tags=list(tags or []),
            created_at=datetime.now().isoformat(),
            metadata=meta,
        )
        await asyncio.to_thread(self._write_entry, entry)
        return entry_id

    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None:
        entry = await asyncio.to_thread(self._file_to_entry, self._memories_dir / f"{entry_id}.md")
        if entry is None:
            return None
        if not include_invalid and not entry.metadata.get("valid", True):
            return None
        return entry

    def _all_entries(self, *, include_invalid: bool) -> list[MemoryEntry]:
        out = []
        for path in self._memories_dir.glob("*.md"):
            if entry := self._file_to_entry(path):
                if include_invalid or entry.metadata.get("valid", True):
                    out.append(entry)
        return out

    async def search_memories(
        self,
        query: str,
        *,
        top_k: int = 5,
        include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        def _search() -> list[MemoryEntry]:
            tokens = re.findall(r"\w+", query.lower())
            scored = []
            for e in self._all_entries(include_invalid=include_invalid):
                text = (e.content + " " + " ".join(e.tags)).lower()
                lex = sum(1 for t in tokens if t in text) if tokens else 1
                if lex == 0:
                    continue
                scored.append((lex, e.metadata.get("importance", 5), e.created_at, e))
            scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
            return [s[3] for s in scored[:top_k]]

        return await asyncio.to_thread(_search)

    async def list_index(self) -> list[MemoryIndexEntry]:
        def _index() -> list[MemoryIndexEntry]:
            entries = self._all_entries(include_invalid=False)
            entries.sort(key=lambda e: (e.metadata.get("importance", 5), e.created_at), reverse=True)
            out = []
            for e in entries:
                summary = e.metadata.get("summary") or (e.content[:80] + ("…" if len(e.content) > 80 else ""))
                out.append(MemoryIndexEntry(id=e.id, tags=e.tags, summary=summary))
            return out

        return await asyncio.to_thread(_index)

    # ── curation writes ──

    async def update_memory(
        self,
        entry_id: str,
        *,
        content=None,
        tags=None,
        importance=None,
        summary=None,
        links=None,
        last_used=None,
    ) -> None:
        path = self._memories_dir / f"{entry_id}.md"

        def _update() -> None:
            # Hold the lock across read+write so a concurrent writer (e.g. a
            # recall-flush last_used bump racing a consolidation importance edit)
            # cannot read a stale snapshot and clobber the other's field.
            with _flock(path):
                entry = self._file_to_entry(path)
                if entry is None:
                    return
                if content is not None:
                    entry.content = content
                if tags is not None:
                    entry.tags = list(tags)
                if importance is not None:
                    entry.metadata["importance"] = importance
                if summary is not None:
                    entry.metadata["summary"] = summary
                if links is not None:
                    entry.metadata["links"] = list(links)
                if last_used is not None:
                    entry.metadata["last_used"] = last_used
                self._write_entry_unlocked(entry)

        await asyncio.to_thread(_update)

    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None:
        path = self._memories_dir / f"{entry_id}.md"

        def _invalidate() -> None:
            with _flock(path):
                entry = self._file_to_entry(path)
                if entry is None:
                    return
                entry.metadata["valid"] = False
                entry.metadata["invalidated_at"] = datetime.now().isoformat()
                entry.metadata["invalidated_by"] = requested_by
                self._write_entry_unlocked(entry)

        await asyncio.to_thread(_invalidate)

    async def restore_memory(self, entry_id: str) -> None:
        path = self._memories_dir / f"{entry_id}.md"

        def _restore() -> None:
            with _flock(path):
                entry = self._file_to_entry(path)
                if entry is None:
                    return
                entry.metadata["valid"] = True
                entry.metadata["invalidated_at"] = None
                entry.metadata["invalidated_by"] = None
                self._write_entry_unlocked(entry)

        await asyncio.to_thread(_restore)

    async def purge_invalidated(self, *, older_than_days: int) -> int:
        def _purge() -> int:
            cutoff = datetime.now() - timedelta(days=older_than_days)
            count = 0
            for path in self._memories_dir.glob("*.md"):
                entry = self._file_to_entry(path)
                if entry is None or entry.metadata.get("valid", True):
                    continue
                inv_at = entry.metadata.get("invalidated_at")
                try:
                    when = datetime.fromisoformat(inv_at) if inv_at else cutoff
                except ValueError:
                    when = cutoff
                if when <= cutoff:
                    path.unlink(missing_ok=True)
                    count += 1
            return count

        return await asyncio.to_thread(_purge)
