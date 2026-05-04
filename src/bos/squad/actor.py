from __future__ import annotations

from typing import Any

from bos.core import ReactAgent


def _filter_tool_noise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            content = msg.get("content", "")
            if not content or not str(content).strip():
                continue
            cleaned.append({"role": "assistant", "content": str(content)})
        else:
            cleaned.append(msg)
    return cleaned


class SquadAgent(ReactAgent):
    """ReactAgent that filters tool-call noise from shared chat history."""

    async def _get_chat_history(self, chat_id: str) -> list[dict[str, Any]]:
        history = await super()._get_chat_history(chat_id)
        return _filter_tool_noise(history)
