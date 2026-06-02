from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bos.core import ChatStore
from bos.protocol import MessageType


@dataclass(frozen=True)
class ChannelConversationRef:
    channel_id: str
    channel_conversation_id: str


@dataclass(frozen=True)
class ActiveTurn:
    chat_id: str
    actor: str
    turn_id: str
    base_revision: int
    started_by_ref: ChannelConversationRef
    started_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class PrepareSendResult:
    ok: bool
    chat_id: str
    ref: ChannelConversationRef
    current_revision: int
    stale: bool = False
    active_turn: bool = False
    missing_messages: list[dict[str, Any]] | None = None
    error: str | None = None


class ChatCoordinationError(RuntimeError):
    pass


class ChatCoordinator:
    def __init__(self, chat_store: ChatStore) -> None:
        self._chat_store = chat_store
        self._cursors: dict[ChannelConversationRef, str] = {}
        self._observed: dict[tuple[ChannelConversationRef, str], int] = {}
        self._active_turns: dict[str, ActiveTurn] = {}
        self._revision_cache: dict[str, int] = {}

    def get_cursor(self, ref: ChannelConversationRef) -> str | None:
        return self._cursors.get(ref)

    def set_cursor(self, ref: ChannelConversationRef, chat_id: str, *, observed_revision: int) -> None:
        self._cursors[ref] = chat_id
        self.mark_observed(chat_id=chat_id, ref=ref, revision=observed_revision)

    def new_chat(self, ref: ChannelConversationRef) -> str:
        chat_id = uuid.uuid4().hex
        self.set_cursor(ref, chat_id, observed_revision=0)
        return chat_id

    async def prepare_send(
        self,
        *,
        chat_id: str,
        ref: ChannelConversationRef,
        base_revision: int,
        content_type: MessageType | str = MessageType.MESSAGE,
    ) -> PrepareSendResult:
        current_revision = await self.current_revision(chat_id)
        if base_revision < current_revision:
            return PrepareSendResult(
                ok=False,
                chat_id=chat_id,
                ref=ref,
                current_revision=current_revision,
                stale=True,
                missing_messages=await self.hydrate(chat_id=chat_id, from_revision=base_revision),
                error="stale_chat",
            )
        if base_revision > current_revision:
            return PrepareSendResult(
                ok=False,
                chat_id=chat_id,
                ref=ref,
                current_revision=current_revision,
                stale=True,
                error="future_base_revision",
            )

        active = self._active_turns.get(chat_id)
        if active is None:
            return PrepareSendResult(ok=True, chat_id=chat_id, ref=ref, current_revision=current_revision)

        if self._is_interrupt(content_type) and self._can_interrupt(chat_id, ref, active, base_revision):
            return PrepareSendResult(ok=True, chat_id=chat_id, ref=ref, current_revision=current_revision)

        return PrepareSendResult(
            ok=False,
            chat_id=chat_id,
            ref=ref,
            current_revision=current_revision,
            active_turn=True,
            error="active_turn",
        )

    def mark_observed(self, *, chat_id: str, ref: ChannelConversationRef, revision: int) -> None:
        self._observed[(ref, chat_id)] = revision
        self._revision_cache[chat_id] = max(self._revision_cache.get(chat_id, 0), revision)

    async def hydrate(self, *, chat_id: str, from_revision: int | None = None) -> list[dict[str, Any]]:
        messages = await self._chat_store.get_messages(chat_id, active_only=False)
        result: list[dict[str, Any]] = []
        for message in messages:
            revision = self._message_revision(message.metadata)
            if from_revision is not None and revision <= from_revision:
                continue
            result.append(
                {
                    "llm_message": message.llm_message,
                    "metadata": dict(message.metadata),
                    "turn_id": message.turn_id,
                    "created_at": message.created_at.isoformat(),
                    "is_summary": message.is_summary,
                }
            )
        return result

    def active_turn_status(self, chat_id: str) -> dict[str, Any] | None:
        active = self._active_turns.get(chat_id)
        if active is None:
            return None
        return {
            "chat_id": active.chat_id,
            "actor": active.actor,
            "turn_id": active.turn_id,
            "base_revision": active.base_revision,
            "started_by": {
                "channel_id": active.started_by_ref.channel_id,
                "channel_conversation_id": active.started_by_ref.channel_conversation_id,
            },
            "started_at": active.started_at.isoformat(),
        }

    def active_turns_status(self) -> dict[str, dict[str, Any]]:
        return {
            chat_id: status
            for chat_id in list(self._active_turns)
            if (status := self.active_turn_status(chat_id)) is not None
        }

    def clear_active_turns(self, *, actor: str | None = None) -> list[ActiveTurn]:
        cleared: list[ActiveTurn] = []
        for chat_id, active in list(self._active_turns.items()):
            if actor is not None and active.actor != actor:
                continue
            cleared.append(active)
            self._active_turns.pop(chat_id, None)
        return cleared

    async def begin_turn(
        self,
        *,
        chat_id: str,
        ref: ChannelConversationRef,
        actor: str,
        turn_id: str,
        base_revision: int,
    ) -> ActiveTurn:
        current_revision = await self.current_revision(chat_id)
        if current_revision != base_revision:
            raise ChatCoordinationError(
                f"Chat {chat_id!r} is at revision {current_revision}, not base revision {base_revision}."
            )
        if chat_id in self._active_turns:
            raise ChatCoordinationError(f"Chat {chat_id!r} already has an active turn.")
        active = ActiveTurn(
            chat_id=chat_id,
            actor=actor,
            turn_id=turn_id,
            base_revision=base_revision,
            started_by_ref=ref,
        )
        self._active_turns[chat_id] = active
        self.set_cursor(ref, chat_id, observed_revision=base_revision)
        return active

    def end_turn(self, *, chat_id: str, turn_id: str, committed_revision: int | None) -> None:
        active = self._active_turns.get(chat_id)
        if active is not None and active.turn_id == turn_id:
            self._active_turns.pop(chat_id, None)
        if committed_revision is not None:
            self._revision_cache[chat_id] = committed_revision

    async def current_revision(self, chat_id: str) -> int:
        messages = await self._chat_store.get_messages(chat_id, active_only=False)
        revision = max((self._message_revision(message.metadata) for message in messages), default=0)
        self._revision_cache[chat_id] = revision
        return revision

    def _can_interrupt(
        self,
        chat_id: str,
        ref: ChannelConversationRef,
        active: ActiveTurn,
        base_revision: int,
    ) -> bool:
        if base_revision != active.base_revision:
            return False
        observed = self._observed.get((ref, chat_id))
        return observed == base_revision or self._cursors.get(ref) == chat_id

    @staticmethod
    def _is_interrupt(content_type: MessageType | str) -> bool:
        return str(content_type) in {
            str(MessageType.INTERRUPT_MESSAGE),
            str(MessageType.INTERRUPT_ABORT),
        }

    @staticmethod
    def _message_revision(metadata: dict[str, Any]) -> int:
        raw = metadata.get("chat_revision")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        return 0
