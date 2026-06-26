from __future__ import annotations

import re
import uuid
from typing import Any

from .contract import ChatMeta, Message, ToolNoiseFilter

# Internal (non-user-facing) chats embed this separator in their chat_id —
# disposable-agent chats (see ``make_internal_chat_id``). The user-facing read
# paths (chat list, memory ingestion) hide them.
INTERNAL_CHAT_SEPARATOR = "~"


def make_internal_chat_id(tag: str, parent_chat_id: str | None = None) -> str:
    """Derive a chat_id for an internal (non-user-facing) disposable agent.

    Covers both flavors: on-turn subagents (pass the parent's ``parent_chat_id``
    so the child nests under it) and off-turn agents like the memory consolidator
    (omit it). Always embeds ``INTERNAL_CHAT_SEPARATOR`` so ``is_internal_chat``
    recognizes it and it stays out of the user's chat list / recall. Shape:
    ``{parent}{sep}{tag}{sep}{uuid}`` (``parent`` empty when off-turn). The uuid
    slice is 48 random bits — enough that disposable-agent chats don't collide in
    the store."""
    sep = INTERNAL_CHAT_SEPARATOR
    # Sanitize the tag to a safe slug: drop the separator and any path-dangerous
    # characters (keep [a-z0-9]; collapse every other run to '-'). Not truncated.
    agent_tag = re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-") or "agent"
    return f"{parent_chat_id or ''}{sep}{agent_tag}{sep}{uuid.uuid4().hex[:12]}"


def is_internal_chat(chat_id: str) -> bool:
    """True for chats that should not surface in the user's chat list or feed
    memory recall — currently subagent child chats, whose ids carry
    ``INTERNAL_CHAT_SEPARATOR``."""
    return INTERNAL_CHAT_SEPARATOR in chat_id


def filter_internal_chats(chats: dict[str, ChatMeta]) -> dict[str, ChatMeta]:
    """Drop internal (non-user-facing) chats from a ``list_chats()`` result.
    Convenience for read-time, call-site filtering of the user's chat list."""
    return {chat_id: meta for chat_id, meta in chats.items() if not is_internal_chat(chat_id)}


def filter_tool_noise(messages: list[Message], *, mode: ToolNoiseFilter) -> list[Message]:
    if mode == "keep_all":
        return list(messages)

    # strip_all: drop tool results and empty tool-call assistant turns; keep
    # assistant turns that carry real text (minus their tool_calls field).
    result: list[Message] = []
    for msg in messages:
        llm = msg.llm_message
        role = llm.get("role", "")
        if role == "tool":
            continue
        if role == "assistant" and llm.get("tool_calls"):
            content = llm.get("content", "")
            if not content or (isinstance(content, str) and not content.strip()):
                continue
            result.append(_message_with_llm(msg, {"role": "assistant", "content": content}))
        else:
            result.append(msg)
    return result


def project_message(message: Message) -> dict[str, Any]:
    llm = message.llm_message
    projected: dict[str, Any] = {"role": llm["role"]}
    content = llm.get("content", "")
    if content or content == "":
        projected["content"] = content
    # Never emit historical tool_calls — provider-validity requires
    # matching tool messages, which are dropped by filtering.
    # ChatStore implementations call this AFTER filter_tool_noise.
    if message.is_summary:
        projected["_is_summary"] = True
    return projected


def _message_with_llm(msg: Message, llm_message: dict[str, Any]) -> Message:
    return Message(
        llm_message=llm_message,
        created_at=msg.created_at,
        turn_id=msg.turn_id,
        is_summary=msg.is_summary,
        metadata=dict(msg.metadata),
    )
