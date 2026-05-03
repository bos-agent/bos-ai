"""Markdown-file-backed memory extension for maxims and episodic memories.

All file I/O is offloaded to threads via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path

from .._utils import _flock
from ..contract import MemoryEntry, ep_memory

logger = logging.getLogger(__name__)


@ep_memory(name="_default")
class MarkdownMemoryExtension:
    """File-based memory store. Maxims in ``maxims/``, memories in ``memories/``."""

    def __init__(self, store_dir: str | Path | None = None, bos_dir: str | Path | None = None) -> None:
        store_dir = Path(store_dir).expanduser() if store_dir else "memory"
        self._dir = Path(bos_dir or ".").expanduser().resolve() / store_dir
        self._maxims_dir = self._dir / "maxims"
        self._memories_dir = self._dir / "memories"
        self._maxims_dir.mkdir(parents=True, exist_ok=True)
        self._memories_dir.mkdir(parents=True, exist_ok=True)

    # ── helpers ──

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read text from %s", path, exc_info=True)
            return ""

    @staticmethod
    def _file_to_entry(path: Path) -> MemoryEntry | None:
        try:
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                return None
            lines = content.splitlines()
            tags_line = lines[0] if lines and lines[0].startswith("tags:") else ""
            body = "\n".join(lines[1:]) if tags_line else content
            tags = [t.strip() for t in tags_line.removeprefix("tags:").split(",") if t.strip()]
            stat = path.stat()
            return MemoryEntry(
                id=path.stem,
                content=body.strip(),
                tags=tags,
                created_at=datetime.fromtimestamp(stat.st_ctime).isoformat(),
            )
        except Exception:
            logger.warning("Failed to read memory entry %s", path, exc_info=True)
            return None

    # ── Maxims ──

    async def get_maxim(self, key: str) -> str:
        return await asyncio.to_thread(self._read_text_sync, self._maxims_dir / f"{key.lower()}.md")

    async def set_maxim(self, key: str, content: str) -> None:
        path = self._maxims_dir / f"{key.lower()}.md"

        def _write() -> None:
            with _flock(path):
                path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    # ── Memories ──

    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        def _search() -> list[MemoryEntry]:
            q = query.lower()
            entries = []
            for path in sorted(self._memories_dir.glob("*.md"), key=lambda p: p.stat().st_ctime, reverse=True):
                if entry := self._file_to_entry(path):
                    if q in entry.content.lower() or any(q in t.lower() for t in entry.tags):
                        entries.append(entry)
                if len(entries) >= top_k:
                    break
            return entries

        return await asyncio.to_thread(_search)

    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str:
        entry_id = uuid.uuid4().hex[:12]
        path = self._memories_dir / f"{entry_id}.md"
        tag_header = f"tags:{','.join(tags or [])}\n" if tags else ""

        def _write() -> None:
            with _flock(path):
                path.write_text(f"{tag_header}{content}", encoding="utf-8")

        await asyncio.to_thread(_write)
        return entry_id

    async def get_memory(self, entry_id: str) -> MemoryEntry | None:
        return await asyncio.to_thread(self._file_to_entry, self._memories_dir / f"{entry_id}.md")

    async def forget_memory(self, entry_id: str) -> None:
        path = self._memories_dir / f"{entry_id}.md"

        def _remove() -> None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_remove)

    # ── Optimization ──

    async def optimize(self) -> None:
        pass
