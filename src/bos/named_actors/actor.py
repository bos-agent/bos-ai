from __future__ import annotations

import asyncio
import contextvars
from dataclasses import replace
from typing import Any

from bos.core import AgentActor, Message, ReactAgent
from bos.core._utils import _compact
from bos.core.actor import _RouteAwareMailboxEventSink
from bos.core.harness import CURRENT_MAILBOX
from bos.protocol import Envelope, MessageContent, MessageContentPart, MessageType
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

    async def run(self) -> None:
        try:
            while True:
                for chat_id in list(self._sessions.keys()):
                    await self._finalize_done_task(chat_id)

                env = await self._mailbox.receive_nowait()
                if env is None:
                    await asyncio.sleep(0.1)
                    continue

                if not env.chat_id:
                    await self._mailbox.send(
                        env.sender,
                        "(error: missing chat_id)",
                        content_type=MessageType.SYSTEM,
                    )
                    continue

                chat_id = env.chat_id
                session = self._get_or_create_session(chat_id)

                if env.content_type == MessageType.COMMAND:
                    if env.content.strip().startswith("/"):
                        self._spawn_command_task(env)
                        continue
                    env.content_type = MessageType.MESSAGE

                if session.execution.task is None:
                    if env.content_type == MessageType.MESSAGE:
                        session.interrupts.clear()
                        session.execution.reply_recipient = env.sender
                        session.execution.reply_chat_id = env.chat_id
                        generation = session.execution.generation
                        session.execution.task = asyncio.create_task(
                            self._run_ask(
                                chat_id=chat_id,
                                generation=generation,
                                reply_recipient=env.sender,
                                reply_chat_id=env.chat_id,
                                content=env.content,
                                inbound_env=env,
                            )
                        )
                    continue

                if env.content_type == MessageType.INTERRUPT_ABORT:
                    self._abort_current_turn(session)
                elif env.content_type == MessageType.INTERRUPT_MESSAGE:
                    session.interrupts.append(env)
                else:
                    await self._mailbox.send(
                        env.sender,
                        "(busy: a response is already in progress for this chat)",
                        content_type=MessageType.SYSTEM,
                        chat_id=env.chat_id,
                    )

        except asyncio.CancelledError:
            await self.aclose()
            raise

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

        await self._mailbox.send(
            reply_recipient,
            response,
            chat_id=reply_chat_id,
            metadata=self._assistant_metadata(reply_recipient),
        )

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
