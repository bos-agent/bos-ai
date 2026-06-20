"""Per-chat last-handled revision store (BEP 10 §4 watermark).

Single JSON file under the agent's memory subtree. Atomic write via
write-then-replace. Concurrency: relies on the JobRunner serializing
consolidation per chat; no in-process lock needed.

Each agent has its own WatermarkStore instance (Ω: per-agent storage),
so the store is keyed by chat_id only — no cross-agent dimension."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


class WatermarkStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _load_sync(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_sync(self, data: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    async def get(self, chat_id: str) -> int:
        data = await asyncio.to_thread(self._load_sync)
        return int(data.get(chat_id, 0))

    async def set(self, chat_id: str, revision: int) -> None:
        def _write() -> None:
            data = self._load_sync()
            data[chat_id] = int(revision)
            self._save_sync(data)

        await asyncio.to_thread(_write)

    async def snapshot(self) -> dict[str, int]:
        return await asyncio.to_thread(self._load_sync)
