from __future__ import annotations

import json
from typing import Any

from .contract import Message, ToolNoiseFilter, ToolResultStatus


def filter_tool_noise(messages: list[Message], *, mode: ToolNoiseFilter) -> list[Message]:
    if mode == "keep_all":
        return list(messages)

    result: list[Message] = []
    for msg in messages:
        llm = msg.llm_message
        role = llm.get("role", "")

        if mode == "strip_all":
            if role == "tool":
                continue
            if role == "assistant" and llm.get("tool_calls"):
                content = llm.get("content", "")
                if not content or (isinstance(content, str) and not content.strip()):
                    continue
                result.append(_message_with_llm(
                    msg, {"role": "assistant", "content": content}
                ))
            else:
                result.append(msg)
            continue

        # keep_signatures mode
        if role == "tool":
            continue
        if role == "assistant" and llm.get("tool_calls"):
            content = llm.get("content", "")
            tool_calls = llm["tool_calls"]
            # Build textual signatures
            sig_parts: list[str] = []
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                args_str = json.dumps(func.get("arguments", {}))
                if len(args_str) > 80:
                    args_str = args_str[:77] + "..."
                status = _tool_result_status(tc.get("id", ""), messages)
                sig_parts.append(f"[tool call: {name}({args_str}) -> {status}]")
            sig_text = "\n".join(sig_parts)

            if content and (isinstance(content, str) and content.strip()):
                new_content = f"{content}\n{sig_text}" if isinstance(content, str) else content
                result.append(_message_with_llm(
                    msg,
                    {"role": "assistant", "content": new_content},
                ))
            else:
                # No meaningful content — emit signature as a standalone message
                result.append(_message_with_llm(
                    msg,
                    {"role": "assistant", "content": sig_text},
                ))
        else:
            result.append(msg)
    return result


def _tool_result_status(tool_call_id: str, messages: list[Message]) -> ToolResultStatus:
    for msg in messages:
        llm = msg.llm_message
        if llm.get("role") == "tool" and llm.get("tool_call_id") == tool_call_id:
            content = llm.get("content", "")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "error" in parsed:
                        return "error"
                except (json.JSONDecodeError, TypeError):
                    pass
            return "success"
    return "unknown"


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
