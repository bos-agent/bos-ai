from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from bos.protocol import Envelope, MessageContent, MessageType
from bos.protocol.content import content_as_parts

from .agent import AbortTurn
from .chat_state import ChatState
from .contract import ep_actor_command
from .events import MailboxEventSink
from .harness import CURRENT_HARNESS, CURRENT_MAILBOX
from .tasks import task_chat_id, task_metadata

logger = logging.getLogger(__name__)


@dataclass
class SessionBuffers:
    pending: list[Envelope] = field(default_factory=list)
    interrupts: list[Envelope] = field(default_factory=list)


@dataclass
class SessionExecution:
    task: asyncio.Task | None = None
    generation: int = 0
    reply_recipient: str | None = None
    reply_chat_id: str | None = None


@dataclass
class SessionState:
    chat_id: str
    execution: SessionExecution = field(default_factory=SessionExecution)
    buffers: SessionBuffers = field(default_factory=SessionBuffers)


class _RouteAwareMailboxEventSink(MailboxEventSink):
    def __init__(self, mailbox: Any, recipient: str, chat_id: str | None) -> None:
        super().__init__(mailbox, recipient)
        self._chat_id = chat_id

    async def emit(self, event: Any) -> None:
        await self._mailbox.send(
            self._recipient,
            json.dumps(event.to_payload(), default=str),
            content_type=MessageType.TURN_EVENT,
            chat_id=self._chat_id,
            metadata={"turn_id": event.turn_id, "event_type": event.event_type},
        )


class AgentActor:
    """Actor that drives an Agent via a bound MailBox.

    Sessions are keyed by ``chat_id``.  Each distinct
    ``chat_id`` gets its own concurrent task slot, pending/interrupt
    buffers, and generation counter.
    """

    def __init__(self, agent: Any, mailbox: Any, chat_state: ChatState | None = None, task_ledger: Any = None):
        self._address = mailbox.address
        self._agent = agent
        self._mailbox = mailbox
        self._chat_state = chat_state or ChatState()
        self._task_ledger = task_ledger
        self._sessions: dict[str, SessionState] = {}
        self._command_tasks: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        tasks = [session.execution.task for session in self._sessions.values() if session.execution.task is not None]
        tasks.extend(self._command_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._command_tasks.clear()
        for session in self._sessions.values():
            session.execution.task = None

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
                if self._task_ledger is not None:
                    try:
                        self._task_ledger.validate_task_envelope(
                            chat_id=chat_id,
                            metadata=env.metadata,
                            actor_address=self._address,
                        )
                    except Exception as exc:
                        await self._mailbox.send(
                            env.sender,
                            f"(error: {exc})",
                            content_type=MessageType.SYSTEM,
                            chat_id=env.chat_id,
                            metadata={"task_validation_error": str(exc)},
                        )
                        continue
                session = self._get_or_create_session(chat_id)

                if env.content_type == MessageType.COMMAND:
                    if env.content.strip().startswith("/"):
                        self._spawn_command_task(env)
                        continue
                    env.content_type = MessageType.MESSAGE

                if session.execution.task is None:
                    if env.content_type == MessageType.MESSAGE:
                        session.buffers.pending.append(env)
                        self._fire_pending(chat_id)
                    continue

                if env.content_type in (MessageType.INTERRUPT_MESSAGE, MessageType.INTERRUPT_ABORT):
                    session.buffers.interrupts.append(env)
                else:
                    session.buffers.pending.append(env)

        except asyncio.CancelledError:
            await self.aclose()
            raise

    def current_chat_id(self, env: Envelope, explicit_input: str | None = None) -> str | None:
        if explicit_input and explicit_input.strip():
            return self._chat_state.resolve_alias_or_id(explicit_input.strip())
        return env.chat_id

    async def reset_chat(self, env: Envelope) -> str:
        """Reset the session for the current chat.

        Pops the old session (cancelling any in-flight task) and returns
        a fresh chat_id.  The caller adopts the new id going forward.
        """
        await self.retire_session(env.chat_id)
        return uuid.uuid4().hex

    async def retire_session(self, chat_id: str | None) -> None:
        if not chat_id:
            return
        session = self._sessions.pop(chat_id, None)
        if session is None:
            return
        session.execution.generation += 1
        session.buffers.pending.clear()
        session.buffers.interrupts.clear()
        task = session.execution.task
        session.execution.task = None
        session.execution.reply_recipient = None
        session.execution.reply_chat_id = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _spawn_command_task(self, env: Envelope) -> None:
        task = asyncio.create_task(self._handle_command(env))
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

    async def _finalize_done_task(self, chat_id: str) -> None:
        session = self._sessions.get(chat_id)
        if session is None:
            return
        task = session.execution.task
        if task is None or not task.done():
            return

        if not task.cancelled() and (exc := task.exception()):
            import traceback

            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.error("Ask task failed for chat_id=%s:\n%s", chat_id, tb)
            error_content = f"(error: {exc})\n\n```\n{tb}```"
            if session.execution.reply_recipient is not None:
                await self._mailbox.send(
                    session.execution.reply_recipient,
                    error_content,
                    chat_id=session.execution.reply_chat_id,
                )

        session.execution.task = None
        session.execution.reply_recipient = None
        session.execution.reply_chat_id = None

        if any(env.content_type == MessageType.MESSAGE for env in session.buffers.pending):
            self._fire_pending(chat_id)

    def _get_or_create_session(self, chat_id: str) -> SessionState:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionState(chat_id=chat_id)
        return self._sessions[chat_id]

    def _fire_pending(self, chat_id: str) -> None:
        session = self._sessions[chat_id]
        messages = [env for env in session.buffers.pending if env.content_type == MessageType.MESSAGE]
        session.buffers.pending.clear()
        if not messages:
            return

        content = self._merge_pending_messages(messages)
        session.buffers.interrupts.clear()
        no_reply = bool(messages[-1].metadata.get("no_reply"))
        reply_recipient = None if no_reply else messages[-1].sender
        reply_chat_id = None if no_reply else messages[-1].chat_id
        session.execution.reply_recipient = reply_recipient
        session.execution.reply_chat_id = reply_chat_id
        generation = session.execution.generation
        session.execution.task = asyncio.create_task(
            self._run_ask(
                chat_id=chat_id,
                generation=generation,
                reply_recipient=reply_recipient,
                reply_chat_id=reply_chat_id,
                content=content,
            )
        )

    async def _run_ask(
        self,
        *,
        chat_id: str,
        generation: int,
        reply_recipient: str | None,
        reply_chat_id: str | None,
        content: MessageContent,
    ) -> None:
        while True:
            token: contextvars.Token | None = None
            try:
                token = CURRENT_MAILBOX.set(self._mailbox)
                event_sink = (
                    _RouteAwareMailboxEventSink(self._mailbox, reply_recipient, reply_chat_id)
                    if reply_recipient is not None
                    else None
                )
                response = await self._agent.ask(
                    chat_id,
                    content,
                    interrupt=self._make_interrupt(chat_id, generation),
                    ctx_metadata={"sender": reply_recipient, "actor_address": self._address},
                    event_sink=event_sink,
                )
            finally:
                if token is not None:
                    CURRENT_MAILBOX.reset(token)

            if not self._execution_is_current(chat_id, generation):
                return

            if reply_recipient is not None:
                routed_chat_id, routed_metadata = self._task_response_route(
                    source_chat_id=chat_id,
                    reply_chat_id=reply_chat_id,
                    reply_recipient=reply_recipient,
                    response=response,
                )
                await self._mailbox.send(
                    reply_recipient,
                    response,
                    chat_id=routed_chat_id,
                    metadata=routed_metadata,
                )

            if not self._execution_is_current(chat_id, generation):
                return

            session = self._sessions[chat_id]
            messages = [env for env in session.buffers.pending if env.content_type == MessageType.MESSAGE]
            session.buffers.pending.clear()
            if not messages:
                break

            no_reply = bool(messages[-1].metadata.get("no_reply"))
            reply_recipient = None if no_reply else messages[-1].sender
            reply_chat_id = None if no_reply else messages[-1].chat_id
            session.execution.reply_recipient = reply_recipient
            session.execution.reply_chat_id = reply_chat_id
            content = self._merge_pending_messages(messages)

    def _task_response_route(
        self,
        *,
        source_chat_id: str,
        reply_chat_id: str | None,
        reply_recipient: str,
        response: str,
    ) -> tuple[str | None, dict[str, Any]]:
        if self._task_ledger is None:
            return reply_chat_id, {}
        binding = self._task_ledger.get_binding(source_chat_id)
        if binding is None or binding.actor_address != self._address:
            return reply_chat_id, {}
        task = self._task_ledger.get_task(binding.task_id)
        self._task_ledger.append_event(
            task.id,
            "progress",
            actor=self._address,
            content=response,
            metadata={"source_chat_id": source_chat_id, "reply_recipient": reply_recipient},
        )
        if binding.purpose != "worker" or not reply_recipient.startswith("agent@") or reply_recipient == self._address:
            return reply_chat_id, task_metadata(task, chat_id=reply_chat_id or source_chat_id)

        notify_binding = self._task_ledger.first_binding(
            task.id,
            actor_address=reply_recipient,
            purpose="coordinator",
        )
        if notify_binding is None:
            notify_binding = self._task_ledger.bind_chat(
                task_id=task.id,
                chat_id=task_chat_id(task.id, "coordinator"),
                actor_address=reply_recipient,
                purpose="coordinator",
            )
        metadata = task_metadata(task, chat_id=notify_binding.chat_id) | {
            "task_notification": True,
            "no_reply": True,
        }
        return notify_binding.chat_id, metadata

    async def _handle_command(self, env: Envelope) -> None:
        parts = env.content.split(None, 1)
        cmd_name, input = parts[0].lstrip("/"), "" if len(parts) == 1 else parts[1]

        if not ep_actor_command.has(cmd_name):
            result: str | dict[str, Any] = f"Invalid command `{cmd_name}`"
        else:
            try:
                result = await ep_actor_command.invoke_async(
                    cmd_name, {"input": input, "env": env, "actor": self, "harness": CURRENT_HARNESS.get(None)}
                )
            except Exception as exc:
                result = {"name": cmd_name, "ok": False, "error": str(exc), "result": str(exc)}

        if result is None:
            result = "(done)"

        if not isinstance(result, str):
            result = json.dumps(result, default=str)

        await self._mailbox.send(
            env.sender,
            result,
            content_type=MessageType.COMMAND_RESULT,
            chat_id=env.chat_id,
            metadata=self._command_result_metadata(env),
        )

    @staticmethod
    def _command_result_metadata(env: Envelope) -> dict[str, Any]:
        routing = env.metadata.get("routing")
        if isinstance(routing, dict) and routing:
            return {"routing": routing}
        return {}

    def _make_interrupt(self, chat_id: str, generation: int):
        def _interrupt() -> dict[str, Any] | None:
            if not self._generation_is_current(chat_id, generation):
                raise AbortTurn()

            session = self._sessions.get(chat_id)
            if session is None:
                return None

            buf = session.buffers.interrupts
            parts: list[str] = []
            remaining: list[Envelope] = []
            for env in buf:
                if env.content_type == MessageType.INTERRUPT_ABORT:
                    session.buffers.interrupts = remaining
                    raise AbortTurn()
                if env.content_type == MessageType.INTERRUPT_MESSAGE:
                    parts.append(f"[from {env.sender}]: {env.content}")
                else:
                    remaining.append(env)
            session.buffers.interrupts = remaining
            if parts:
                return {"role": "user", "content": "\n\n".join(parts)}
            return None

        return _interrupt

    def _generation_is_current(self, chat_id: str, generation: int) -> bool:
        session = self._sessions.get(chat_id)
        return session is not None and session.execution.generation == generation

    def _execution_is_current(self, chat_id: str, generation: int) -> bool:
        session = self._sessions.get(chat_id)
        return (
            session is not None
            and session.execution.generation == generation
            and session.execution.task is not None
        )

    @staticmethod
    def _merge_pending_messages(messages: list[Envelope]) -> MessageContent:
        if len(messages) == 1:
            return messages[0].content

        parts: list[dict[str, Any]] = []
        for idx, env in enumerate(messages):
            if idx:
                parts.append({"type": "text", "text": "\n\n"})
            parts.append({"type": "text", "text": f"[from {env.sender} {env.timestamp.isoformat()}]: "})
            parts.extend(content_as_parts(env.content))
        return parts
