from __future__ import annotations

import asyncio
import contextvars
from typing import Any

from bos.core import AgentActor, Message, ReactAgent
from bos.core._utils import _compact
from bos.core.actor import _RouteAwareMailboxEventSink
from bos.core.harness import CURRENT_MAILBOX
from bos.protocol import Envelope, MessageContent, MessageType
from bos.protocol.content import content_as_parts, content_length


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
    return [{"type": "text", "text": label}] + content_as_parts(content)


def _display_label(display_name: str | None, actor_name: str, agent_kind: str | None) -> str:
    label = display_name or actor_name
    return f"{label} ({agent_kind})" if agent_kind else label


class NamedAgent(ReactAgent):
    """ReactAgent that renders shared history with actor attribution."""

    async def _get_chat_history(self, chat_id: str) -> list[dict[str, Any]]:
        async def _get_messages() -> list[dict[str, Any]]:
            messages = await self._message_store.get_messages(chat_id)
            return _filter_tool_noise([self._history_item(message) for message in messages])

        history = await _get_messages()
        if sum(content_length(m.get("content", "")) for m in history) > self._max_tokens:
            summary = await self._consolidator.consolidate(history)
            await self._message_store.save_summary(chat_id, summary)
            history = await _get_messages()
        return history

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

    def _fire_pending(self, chat_id: str) -> None:
        session = self._sessions[chat_id]
        messages = [env for env in session.buffers.pending if env.content_type == MessageType.MESSAGE]
        session.buffers.pending.clear()
        if not messages:
            return

        content = self._merge_pending_messages(messages)
        last_message = messages[-1]
        session.buffers.interrupts.clear()
        session.execution.reply_recipient = last_message.sender
        session.execution.reply_chat_id = last_message.chat_id
        generation = session.execution.generation
        session.execution.task = asyncio.create_task(
            self._run_ask(
                chat_id=chat_id,
                generation=generation,
                reply_recipient=last_message.sender,
                reply_chat_id=last_message.chat_id,
                content=content,
                inbound_env=last_message,
            )
        )

    async def _run_ask(
        self,
        *,
        chat_id: str,
        generation: int,
        reply_recipient: str,
        reply_chat_id: str | None,
        content: MessageContent,
        inbound_env: Envelope,
    ) -> None:
        while True:
            token: contextvars.Token | None = None
            try:
                token = CURRENT_MAILBOX.set(self._mailbox)
                event_sink = _RouteAwareMailboxEventSink(self._mailbox, reply_recipient, reply_chat_id)
                response = await self._agent.ask(
                    chat_id,
                    content,
                    interrupt=self._make_interrupt(chat_id, generation),
                    ctx_metadata=self._turn_metadata(reply_recipient, inbound_env),
                    event_sink=event_sink,
                )
            finally:
                if token is not None:
                    CURRENT_MAILBOX.reset(token)

            if not self._execution_is_current(chat_id, generation):
                return

            assistant_metadata = self._assistant_metadata(reply_recipient)
            await self._mailbox.send(
                reply_recipient,
                response,
                chat_id=reply_chat_id,
                metadata=assistant_metadata,
            )

            if not self._execution_is_current(chat_id, generation):
                return

            session = self._sessions[chat_id]
            messages = [env for env in session.buffers.pending if env.content_type == MessageType.MESSAGE]
            session.buffers.pending.clear()
            if not messages:
                break

            inbound_env = messages[-1]
            reply_recipient = inbound_env.sender
            reply_chat_id = inbound_env.chat_id
            session.execution.reply_recipient = reply_recipient
            session.execution.reply_chat_id = reply_chat_id
            content = self._merge_pending_messages(messages)

    def _turn_metadata(self, reply_recipient: str, inbound_env: Envelope) -> dict[str, Any]:
        return {
            "sender": reply_recipient,
            "actor_name": self.actor_name,
            "actor_address": self._address,
            "actor_display": self.display_label,
            "target_actor": inbound_env.metadata.get("target_actor") or self.actor_name,
            "user_message_metadata": self._user_metadata(inbound_env),
            "assistant_message_metadata": self._assistant_metadata(reply_recipient),
        }

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
