"""
JSONL-backed message store.

Each thread maps to a single `<conversation_id>.jsonl` file.
Implements exactly the same semantics as DefaultMessageStore.

All file I/O is offloaded to threads via ``asyncio.to_thread``
so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from bos.core import Message, _flock, ep_message_store


@ep_message_store(name="JsonlMessageStore")
class JsonlMessageStore:
    """Persistent message store backed by JSONL files."""

    def __init__(self, store_dir: str | Path | None = None) -> None:
        self._dir = Path(store_dir or "./messages").expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── helpers ──────────────────────────────────────────────────

    def _conversation_path(self, conversation_id: str) -> Path:
        return self._dir / f"{conversation_id}.jsonl"

    def _read_messages_sync(self, conversation_id: str) -> list[Message]:
        path = self._conversation_path(conversation_id)
        if not path.exists():
            return []
        messages: list[Message] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                raw = json.loads(line)
                messages.append(
                    Message(
                        llm_message=raw.get("llm_message", {}),
                        created_at=datetime.fromisoformat(raw["created_at"]) if "created_at" in raw else datetime.now(),
                        turn_id=raw.get("turn_id"),
                        is_summary=raw.get("is_summary", False),
                        metadata=raw.get("metadata", {}),
                    )
                )
        return messages

    def _serialize_message(self, m: Message) -> str:
        return json.dumps(
            {
                "llm_message": m.llm_message,
                "created_at": m.created_at.isoformat(),
                "turn_id": m.turn_id,
                "is_summary": m.is_summary,
                "metadata": m.metadata,
            },
            default=str,
        )

    # ── MessageStore protocol ────────────────────────────────────

    async def save_messages(self, conversation_id: str, messages: list[Message]) -> None:
        lines = [self._serialize_message(m) + "\n" for m in messages]
        path = self._conversation_path(conversation_id)

        def _write() -> None:
            with _flock(path):
                with path.open("a", encoding="utf-8") as f:
                    f.writelines(lines)

        await asyncio.to_thread(_write)

    async def get_messages(self, conversation_id: str, original: bool = False) -> list[Message]:
        messages = await asyncio.to_thread(self._read_messages_sync, conversation_id)
        if original:
            return [m for m in messages if not m.is_summary]
        result = []
        for m in reversed(messages):
            if m.is_summary:
                result.append(m)
                break
            result.append(m)
        result.reverse()
        return result

    async def save_summary(self, conversation_id: str, summary: str) -> None:
        m = Message(
            llm_message={"role": "system", "content": f"Conversation summary:\n{summary}"},
            is_summary=True,
        )
        line = self._serialize_message(m) + "\n"
        path = self._conversation_path(conversation_id)

        def _write() -> None:
            with _flock(path):
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)

        await asyncio.to_thread(_write)

    async def list_conversations(self) -> dict[str, Any]:
        def _scan() -> dict[str, Any]:
            if not self._dir.exists():
                return {}
            contexts: dict[str, Any] = {}
            for path in self._dir.glob("*.jsonl"):
                conversation_id = path.stem
                messages = self._read_messages_sync(conversation_id)
                if not messages:
                    continue
                if not (m := next((m for m in messages if m.llm_message["role"] == "user"), None)):
                    m = messages[0]
                contexts[conversation_id] = {
                    "description": m.llm_message["content"],
                    "created_at": m.created_at,
                    "last_activity": messages[-1].created_at,
                    "message_count": len(messages),
                }
            return contexts

        return await asyncio.to_thread(_scan)
