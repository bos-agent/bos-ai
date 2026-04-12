"""Markdown-backed memory store for long-term agent identity and rules.

All file I/O is offloaded to threads via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bos.core import _flock, ep_memory_store

logger = logging.getLogger(__name__)


@ep_memory_store(name="markdown_memory_store")
class MarkdownMemoryStore:
    """File-based memory store for long-term agent identity and rules."""

    def __init__(self, store_dir: str | Path | None = None) -> None:
        self._dir = Path(store_dir or "./memories").expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read text from %s", path, exc_info=True)
            return ""

    async def load_memory(self, key: str) -> str:
        return await asyncio.to_thread(self._read_text_sync, self._dir / f"{key.lower()}.md")

    async def save_memory(self, key: str, content: str) -> None:
        path = self._dir / f"{key.lower()}.md"

        def _write() -> None:
            with _flock(path):
                path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def list_memories(self) -> dict[str, str]:
        def _scan() -> dict[str, str]:
            if not self._dir.exists():
                return {}
            return {p.stem.lower(): txt for p in self._dir.glob("*.md") if (txt := self._read_text_sync(p))}

        return await asyncio.to_thread(_scan)

    async def search_memory(self, query: str) -> dict[str, str]:
        memories = await self.list_memories()
        return {key: txt for key, txt in memories.items() if query.lower() in txt.lower()}
