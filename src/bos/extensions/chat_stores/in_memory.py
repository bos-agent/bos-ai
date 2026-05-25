"""In-memory chat store.

Useful for testing and lightweight ephemeral workloads that do not
require persistence.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from bos.core._chat_store_utils import filter_tool_noise, project_message
from bos.core.contract import (
    ChatMeta,
    ContextResult,
    Message,
    TokenEstimate,
    ToolNoiseFilter,
    ep_chat_store,
)
from bos.protocol.content import content_preview


@ep_chat_store(name="InMemChatStore")
class InMemChatStore:
    """In-process chat store backed by a plain dict."""

    def __init__(
        self,
        tool_noise_filter: ToolNoiseFilter = "keep_signatures",
    ) -> None:
        self._messages: dict[str, list[Message]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._default_filter: ToolNoiseFilter = tool_noise_filter

    def _get_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def _resolve_filter(self, filter_mode: ToolNoiseFilter | None) -> ToolNoiseFilter:
        return filter_mode if filter_mode is not None else self._default_filter

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
        async with self._get_lock(chat_id):
            self._messages.setdefault(chat_id, []).extend(messages)

    async def get_context(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> ContextResult:
        async with self._get_lock(chat_id):
            all_messages = list(self._messages.get(chat_id, []))
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
        async with self._get_lock(chat_id):
            all_messages = list(self._messages.get(chat_id, []))
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
        async with self._get_lock(chat_id):
            all_messages = list(self._messages.get(chat_id, []))
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
        async with self._get_lock(chat_id):
            self._messages.setdefault(chat_id, []).append(m)

    async def get_summary(self, chat_id: str) -> Message | None:
        async with self._get_lock(chat_id):
            messages = self._messages.get(chat_id, [])
        for m in reversed(messages):
            if m.is_summary:
                return m
        return None

    async def get_messages(self, chat_id: str, *, active_only: bool = True) -> list[Message]:
        async with self._get_lock(chat_id):
            messages = list(self._messages.get(chat_id, []))
        if not active_only:
            return messages
        return self._active_messages(messages)

    async def list_chats(self) -> dict[str, ChatMeta]:
        result: dict[str, ChatMeta] = {}
        for chat_id, messages in self._messages.items():
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
