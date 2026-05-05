from __future__ import annotations

from dataclasses import replace
from typing import Any

from bos.core import AgentActor, Message, ReactAgent
from bos.core._utils import _compact
from bos.protocol import Envelope, MessageContent, MessageContentPart
from bos.protocol.content import content_length


def _filter_tool_noise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            content = msg.get("content", "")
            if not content or (isinstance(content, str) and not content.strip()):
                continue
            item = {"role": "assistant", "content": content}
            cleaned.append(item)
        else:
            cleaned.append(msg)
    return cleaned


def _prefix_content(label: str, content: MessageContent) -> MessageContent:
    if isinstance(content, str):
        return f"{label}{content}"
    text_part: MessageContentPart = {"type": "text", "text": label}
    parts: list[MessageContentPart] = [text_part, *content]
    return parts


def _display_label(display_name: str | None, actor_name: str, agent_kind: str | None) -> str:
    label = display_name or actor_name
    return f"{label} ({agent_kind})" if agent_kind else label


class NamedAgent(ReactAgent):
    """ReactAgent that renders shared history with actor attribution."""

    async def _get_chat_history(self, chat_id: str, *, budget_model: str | None = None) -> list[dict[str, Any]]:
        history_messages = await self._get_history_messages(chat_id)
        history = [message.llm_message for message in history_messages]
        if sum(content_length(m.get("content", "")) for m in history) > self._max_tokens:
            summary = await self._consolidator.consolidate(history_messages)
            await self._message_store.save_summary(chat_id, summary)
            history_messages = await self._get_history_messages(chat_id)
        return [message.llm_message for message in history_messages]

    async def _get_history_messages(self, chat_id: str) -> list[Message]:
        messages = await self._message_store.get_messages(chat_id)
        attributed = [replace(message, llm_message=self._history_item(message)) for message in messages]
        return self._filter_tool_noise_messages(attributed)

    @staticmethod
    def _filter_tool_noise_messages(messages: list[Message]) -> list[Message]:
        cleaned: list[Message] = []
        for message in messages:
            msg = message.llm_message
            role = msg.get("role", "")
            if role == "tool":
                continue
            if role == "assistant" and msg.get("tool_calls"):
                content = msg.get("content", "")
                if not content or (isinstance(content, str) and not content.strip()):
                    continue
                cleaned.append(replace(message, llm_message={"role": "assistant", "content": content}))
            else:
                cleaned.append(message)
        return cleaned

    def _history_item(self, message: Message) -> dict[str, Any]:
        msg = message.llm_message
        content = self._format_content(msg)
        label = self._label_for_metadata(msg.get("role", ""), message.metadata)
        if label:
            content = _prefix_content(label, content)
        return _compact(
            {
                "role": msg["role"],
                "content": content,
                "tool_calls": msg.get("tool_calls", None),
                "tool_call_id": msg.get("tool_call_id", None),
                "name": msg.get("name", None),
            }
        )

    @staticmethod
    def _format_content(msg: dict[str, Any]) -> MessageContent:
        content = msg.get("content", "")
        if msg.get("role") == "tool" and isinstance(content, str) and len(content) > 150:
            return content[:147] + "..."
        return content

    @staticmethod
    def _label_for_metadata(role: str, metadata: dict[str, Any]) -> str:
        if not isinstance(metadata, dict):
            return ""
        if role == "user" and metadata.get("speaker_type") == "user":
            target = metadata.get("to_display") or metadata.get("target_display")
            if not target and metadata.get("to_actor"):
                target = f"@{metadata['to_actor']}"
            return f"[user -> {target}]: " if target else ""
        if role == "assistant" and metadata.get("speaker_type") == "actor":
            source = metadata.get("from_display")
            if not source and metadata.get("from_actor"):
                source = f"@{metadata['from_actor']}"
            target = metadata.get("to") or "user"
            return f"[{source} -> {target}]: " if source else ""
        return ""


class NamedActor(AgentActor):
    """AgentActor with named actor metadata for shared chat attribution."""

    def __init__(
        self,
        agent: Any,
        mailbox: Any,
        chat_state: Any = None,
        *,
        actor_name: str,
        display_name: str | None = None,
        agent_kind: str | None = None,
    ) -> None:
        super().__init__(agent, mailbox, chat_state)
        self.actor_name = actor_name
        self.display_name = display_name
        self.agent_kind = agent_kind
        self.display_label = _display_label(display_name, actor_name, agent_kind)

    def _turn_metadata(self, reply_recipient: str, inbound_env: Envelope | None = None) -> dict[str, Any]:
        if inbound_env is None:
            return super()._turn_metadata(reply_recipient, inbound_env)
        return {
            "sender": reply_recipient,
            "actor_name": self.actor_name,
            "actor_address": self._address,
            "actor_display": self.display_label,
            "target_actor": inbound_env.metadata.get("target_actor") or self.actor_name,
            "user_message_metadata": self._user_metadata(inbound_env),
            "assistant_message_metadata": self._assistant_metadata(reply_recipient),
        }

    def _reply_metadata(self, reply_recipient: str, inbound_env: Envelope | None = None) -> dict[str, Any]:
        return self._assistant_metadata(reply_recipient)

    def _user_metadata(self, env: Envelope) -> dict[str, Any]:
        target_actor = env.metadata.get("target_actor") or self.actor_name
        target_display = env.metadata.get("target_display") or self.display_label
        return {
            "speaker_type": "user",
            "from": "user",
            "sender": env.sender,
            "to_actor": target_actor,
            "to_address": self._address,
            "to_display": target_display,
            "channel": env.sender,
            "target_actor": target_actor,
            "target_display": target_display,
        }

    def _assistant_metadata(self, reply_recipient: str) -> dict[str, Any]:
        return {
            "speaker_type": "actor",
            "from_actor": self.actor_name,
            "from_address": self._address,
            "from_display": self.display_label,
            "to": "user",
            "channel": reply_recipient,
        }
