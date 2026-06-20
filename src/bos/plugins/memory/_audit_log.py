"""Append-only JSONL log used for the curation audit trail and recall log."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bos.core import _flock


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    async def append(self, row: dict) -> None:
        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with _flock(self._path):
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

        await asyncio.to_thread(_write)

    async def read(self) -> list[dict]:
        def _read() -> list[dict]:
            if not self._path.exists():
                return []
            rows = []
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
            return rows

        return await asyncio.to_thread(_read)
