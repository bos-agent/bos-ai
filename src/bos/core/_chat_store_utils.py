from __future__ import annotations

from typing import Any

from .contract import Message, ToolNoiseFilter


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
