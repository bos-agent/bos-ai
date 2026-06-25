from __future__ import annotations

import re
import uuid
from typing import Any

from .contract import ChatMeta, Message, ToolNoiseFilter

# Internal (non-user-facing) chats embed this separator in their chat_id —
# today, subagent child chats (see ``make_subagent_chat_id``). The user-facing
# read paths (chat list, memory ingestion) hide them.
INTERNAL_CHAT_SEPARATOR = "~"


def make_subagent_chat_id(parent_chat_id: str, role: str) -> str:
    """Derive a child chat_id for a subagent turn. Embeds
    ``INTERNAL_CHAT_SEPARATOR`` so the result is recognized by
    ``is_internal_chat`` and kept out of the user's chat list / recall."""
    agent_tag = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "agent"
    agent_tag = agent_tag[:10]
    return f"{parent_chat_id}{INTERNAL_CHAT_SEPARATOR}{agent_tag}{uuid.uuid4().hex[:8]}"


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
