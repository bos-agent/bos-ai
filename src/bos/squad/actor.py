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


class SquadActor:
    """Actor that annotates incoming messages with target_actor attribution.

    Inherits from AgentActor, adding attribution annotation in
    _merge_pending_messages so the agent sees which actor each message was
    addressed to.
    """

    def __init__(self, agent, mailbox, chat_state=None, *, actor_name=None):
        from bos.core import AgentActor

        self._wrapped = AgentActor(agent, mailbox, chat_state)
        self.actor_name = actor_name

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def _merge_pending_messages(self, messages):

        merged = self._wrapped._merge_pending_messages(messages)
        if self.actor_name is None:
            return merged

        attribution = f"[user → @{self.actor_name}]: "
        if isinstance(merged, str):
            return attribution + merged

        if isinstance(merged, list):
            return [{"type": "text", "text": attribution}] + merged

        return merged
