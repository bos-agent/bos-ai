"""
JSONL-backed chat store.

Each chat maps to a single ``<chat_id>.jsonl`` file.
Implements the ChatStore protocol: persistence + context assembly
(load, filter, project, token estimation).

All file I/O is offloaded to threads via ``asyncio.to_thread``
so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from bos.protocol.content import content_preview

from .._chat_store_utils import filter_tool_noise, project_message
from .._utils import _flock
from ..contract import (
    ChatMeta,
    ContextResult,
    Message,
    TokenEstimate,
    ToolNoiseFilter,
    ep_chat_store,
)


@ep_chat_store(name="_default")
class JsonlChatStore:
    """Persistent chat store backed by JSONL files."""

    def __init__(
        self,
        store_dir: str | Path | None = None,
        bos_dir: str | Path | None = None,
        tool_noise_filter: ToolNoiseFilter = "keep_signatures",
    ) -> None:
        store_dir = Path(store_dir).expanduser() if store_dir else "messages"
        self._dir = Path(bos_dir or ".").expanduser().resolve() / store_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._default_filter: ToolNoiseFilter = tool_noise_filter

    # ── helpers ──────────────────────────────────────────────────

    def _chat_path(self, chat_id: str) -> Path:
        return self._dir / f"{chat_id}.jsonl"

    def _read_messages_sync(self, chat_id: str) -> list[Message]:
        path = self._chat_path(chat_id)
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
                        created_at=datetime.fromisoformat(raw["created_at"])
                        if "created_at" in raw
                        else datetime.now(),
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

    def _active_messages(self, messages: list[Message]) -> list[Message]:
        """Return the latest summary (if any) plus all messages after it."""
        result: list[Message] = []
        for m in reversed(messages):
            if m.is_summary:
                result.append(m)
                break
            result.append(m)
        result.reverse()
        return result

    def _resolve_filter(self, filter_mode: ToolNoiseFilter | None) -> ToolNoiseFilter:
        return filter_mode if filter_mode is not None else self._default_filter

    def _estimate(self, projected: list[dict[str, Any]], model: str | None) -> TokenEstimate:
        try:
            from litellm import token_counter

            count = int(token_counter(model=model, messages=projected))
            return TokenEstimate(count=count, tokenizer_model=model, source="litellm")
        except Exception:
            pass
        try:
            serialized = json.dumps(projected, default=str, sort_keys=True)
            count = math.ceil(len(serialized) / 3) + 8 * len(projected)
            return TokenEstimate(count=count, tokenizer_model=model, source="fallback")
        except Exception:
            serialized = json.dumps(projected, default=str, sort_keys=True)
            count = math.ceil(len(serialized) / 3) + 12 * len(projected)
            return TokenEstimate(count=count, tokenizer_model=model, source="fallback-error")

    # ── ChatStore protocol ────────────────────────────────────────

    async def save_turn(
        self, chat_id: str, messages: list[Message], *, turn_id: str | None = None
    ) -> None:
        lines = [self._serialize_message(m) + "\n" for m in messages]
        path = self._chat_path(chat_id)

        def _write() -> None:
            with _flock(path):
                with path.open("a", encoding="utf-8") as f:
                    f.writelines(lines)

        await asyncio.to_thread(_write)

    async def get_context(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> ContextResult:
        all_messages = await asyncio.to_thread(self._read_messages_sync, chat_id)
        active = self._active_messages(all_messages)
        resolved_filter = self._resolve_filter(filter_mode)

        filtered = filter_tool_noise(active, mode=resolved_filter)
        projected = [project_message(m) for m in filtered]
        estimate = self._estimate(projected, tokenizer_model)

        summary_applied = any(m.is_summary for m in active)
        excluded = len(all_messages) - len(active)
        latest_summary = active[0] if summary_applied else None

        return ContextResult(
            messages=projected,
            source_messages=filtered,
            estimated_tokens=estimate.count,
            tokenizer_model=estimate.tokenizer_model,
            estimation_source=estimate.source,
            filter_mode=resolved_filter,
            summary_applied=summary_applied,
            summary_message_count_excluded=excluded,
            latest_summary=latest_summary,
        )

    async def get_compaction_messages(
        self,
        chat_id: str,
        *,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> list[Message]:
        all_messages = await asyncio.to_thread(self._read_messages_sync, chat_id)
        active = self._active_messages(all_messages)
        resolved_filter = self._resolve_filter(filter_mode)
        return filter_tool_noise(active, mode=resolved_filter)

    async def estimate_tokens(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> TokenEstimate:
        all_messages = await asyncio.to_thread(self._read_messages_sync, chat_id)
        active = self._active_messages(all_messages)
        resolved_filter = self._resolve_filter(filter_mode)
        filtered = filter_tool_noise(active, mode=resolved_filter)
        projected = [project_message(m) for m in filtered]
        return self._estimate(projected, tokenizer_model)

    async def save_summary(self, chat_id: str, summary: str) -> None:
        m = Message(
            llm_message={"role": "system", "content": f"Chat summary:\n{summary}"},
            is_summary=True,
        )
        line = self._serialize_message(m) + "\n"
        path = self._chat_path(chat_id)

        def _write() -> None:
            with _flock(path):
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)

        await asyncio.to_thread(_write)

    async def get_summary(self, chat_id: str) -> Message | None:
        messages = await asyncio.to_thread(self._read_messages_sync, chat_id)
        for m in reversed(messages):
            if m.is_summary:
                return m
        return None

    async def get_messages(self, chat_id: str, *, active_only: bool = True) -> list[Message]:
        messages = await asyncio.to_thread(self._read_messages_sync, chat_id)
        if not active_only:
            return messages
        return self._active_messages(messages)

    async def list_chats(self) -> dict[str, ChatMeta]:
        def _scan() -> dict[str, ChatMeta]:
            if not self._dir.exists():
                return {}
            result: dict[str, ChatMeta] = {}
            for path in self._dir.glob("*.jsonl"):
                chat_id = path.stem
                messages = self._read_messages_sync(chat_id)
                if not messages:
                    continue
                if not (m := next((m for m in messages if m.llm_message["role"] == "user"), None)):
                    m = messages[0]
                has_summary = any(msg.is_summary for msg in messages)
                latest_summary_at = None
                if has_summary:
                    for msg in reversed(messages):
                        if msg.is_summary:
                            latest_summary_at = msg.created_at
                            break
                result[chat_id] = ChatMeta(
                    chat_id=chat_id,
                    message_count=len(messages),
                    last_activity=messages[-1].created_at,
                    has_summary=has_summary,
                    latest_summary_at=latest_summary_at,
                    description=content_preview(m.llm_message["content"]),
                )
            return result

        return await asyncio.to_thread(_scan)
