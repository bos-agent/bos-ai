"""
JSONL-backed mailbox.

Each agent has an inbox file ``<agent_name>.jsonl`` inside *store_dir*.
Messages are appended atomically via file locking.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from bos.core import Envelope, _flock, ep_mailbox


def _slugify(address: str) -> str:
    """Convert an address to a filesystem-safe filename segment using URL encoding.

    The ``@`` separator is kept unencoded for readability with the
    ``type@name`` convention.

    Examples:
        "agent@main"  -> "agent@main"
        "channel@http"  -> "channel@http"
    """
    return urllib.parse.quote(address, safe="@")


@ep_mailbox(name="JsonlMailbox")
class JsonlMailbox:
    """File-based mailbox using JSONL inbox files."""

    def __init__(self, store_dir: str | Path = None) -> None:
        self._dir = Path(store_dir or "./mailboxs").expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._byte_offsets: dict[str, int] = {}

    def _inbox_path(self, address: str) -> Path:
        return self._dir / f"{_slugify(address)}.jsonl"

    async def receive(self, address: str) -> Envelope:
        while True:
            env = await self.receive_nowait(address)
            if env is not None:
                return env
            await asyncio.sleep(0.5)

    async def send(self, env: Envelope) -> None:
        target = self._dir / f"{_slugify(env.recipient)}.jsonl"
        payload = asdict(env)
        payload["timestamp"] = env.timestamp.isoformat()
        serialized = json.dumps(payload, default=str) + "\n"

        def _write() -> None:
            with _flock(target):
                with target.open("a", encoding="utf-8") as f:
                    f.write(serialized)

        await asyncio.to_thread(_write)

    def _init_offset(self, address: str) -> int:
        """Seek to EOF on first access so we skip messages from prior runs."""
        inbox = self._inbox_path(address)
        offset = inbox.stat().st_size if inbox.exists() else 0
        self._byte_offsets[address] = offset
        return offset

    async def receive_nowait(self, address: str) -> Envelope | None:
        """Non-blocking receive. Returns ``None`` when inbox is empty."""
        inbox = self._inbox_path(address)
        offset = self._byte_offsets.get(address) if address in self._byte_offsets else self._init_offset(address)

        def _read() -> tuple[str | None, int]:
            if not inbox.exists():
                return None, offset
            with _flock(inbox):
                with inbox.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    line = f.readline()
                    if not line or not line.endswith("\n"):
                        return None, offset
                    return line, f.tell()

        line, new_offset = await asyncio.to_thread(_read)
        self._byte_offsets[address] = new_offset
        if line is None:
            return None
        raw = json.loads(line)
        if timestamp := raw.get("timestamp"):
            raw["timestamp"] = datetime.fromisoformat(timestamp)
        return Envelope(**raw)

    async def aclose(self) -> None:
        """Release tracked state. File locks are per-operation (not held long-term)."""
        self._byte_offsets.clear()
