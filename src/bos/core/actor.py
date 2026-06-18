from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from bos.protocol import Envelope, MessageContent, MessageType, TurnEvent

from .agent import AbortTurn, Agent
from .chat_state import ChatState, ChatStateError
from .contract import MailBox
from .events import MailboxEventSink
from .harness import CURRENT_MAILBOX

logger = logging.getLogger(__name__)


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
    interrupts: list[Envelope] = field(default_factory=list)


@dataclass(frozen=True)
class ActorTurnContext:
    chat_id: str
    actor_name: str
    actor_address: str
    turn_id: str
    reply_recipient: str
    inbound_env: Envelope | None = None
    base_revision: int | None = None
    channel_ref: Any | None = None


@dataclass(frozen=True)
class ActorTurnResult:
    status: str
    committed_revision: int | None = None
    error: BaseException | None = None


class _RouteAwareMailboxEventSink(MailboxEventSink):
    def __init__(
        self,
        mailbox: MailBox,
        recipient: str,
        chat_id: str | None,
        channel_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(mailbox, recipient)
        self._chat_id = chat_id
        self._channel_metadata = dict(channel_metadata or {})

    async def emit(self, event: TurnEvent) -> None:
        metadata = {"turn_id": event.turn_id, "event_type": event.event_type}
        if self._channel_metadata:
            metadata["channel"] = self._channel_metadata
        await self._mailbox.send(
            self._recipient,
            json.dumps(event.to_payload(), default=str),
            content_type=MessageType.TURN_EVENT,
            chat_id=self._chat_id,
            metadata=metadata,
        )


class AgentActor:
    """Actor that drives an Agent via a bound MailBox.

    Sessions are keyed by ``chat_id``.  Each distinct ``chat_id`` gets its
    own concurrent task slot, interrupt buffer, and generation counter.
    Messages arriving during an active turn are rejected immediately.
    """

    def __init__(
        self, agent: Agent, mailbox: MailBox, chat_state: ChatState | None = None,
        *, lifecycle_emitter: Any = None,
    ):
        self._address = mailbox.address
        self._agent = agent
        self._mailbox = mailbox
        self._chat_state = chat_state or ChatState()
        self._sessions: dict[str, SessionState] = {}
        self._lifecycle_emitter = lifecycle_emitter
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
                session = self._get_or_create_session(chat_id)

                if env.content_type == MessageType.COMMAND:
                    command_content = self._command_content(env)
                    if command_content is None:
                        await self._send_command_content_error(env, content_type=MessageType.SYSTEM)
                        continue
                    if command_content.strip().startswith("/"):
                        self._spawn_command_task(env)
                        continue
                    env.content_type = MessageType.MESSAGE

                if session.execution.task is None:
                    if env.content_type == MessageType.MESSAGE:
                        session.interrupts.clear()
                        session.execution.reply_recipient = env.sender
                        session.execution.reply_chat_id = env.chat_id
                        generation = session.execution.generation
                        turn_id = uuid.uuid4().hex
                        session.execution.task = asyncio.create_task(
                            self._run_ask(
                                chat_id=chat_id,
                                generation=generation,
                                turn_id=turn_id,
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
        session.interrupts.clear()
        task = session.execution.task
        session.execution.task = None
        session.execution.reply_recipient = None
        session.execution.reply_chat_id = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        emitter = getattr(self, "_lifecycle_emitter", None)
        if emitter is not None:
            try:
                await emitter(chat_id=chat_id, actor_name=self._actor_name())
            except Exception:
                logger.exception("session_close emitter raised")

    def _spawn_command_task(self, env: Envelope) -> None:
        task = asyncio.create_task(self._handle_command(env))
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

    def _abort_current_turn(self, session: SessionState) -> None:
        """Cancel the current execution and fence stale replies.

        Keep the task attached to the session until the actor loop observes its
        completion so the agent can run cancellation cleanup, including any
        abort-safe history persistence, before a new turn starts for this chat.
        """
        task = session.execution.task
        session.execution.generation += 1
        session.interrupts.clear()
        session.execution.reply_recipient = None
        session.execution.reply_chat_id = None
        if task is not None:
            if task.done():
                self._log_aborted_task_result(task)
                session.execution.task = None
            else:
                task.cancel()

    @staticmethod
    def _log_aborted_task_result(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.debug(
                "Aborted ask task finished with an exception",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

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

    def _get_or_create_session(self, chat_id: str) -> SessionState:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionState(chat_id=chat_id)
        return self._sessions[chat_id]

    async def _run_ask(
        self,
        *,
        chat_id: str,
        generation: int,
        turn_id: str,
        reply_recipient: str,
        reply_chat_id: str | None,
        content: MessageContent,
        inbound_env: Envelope | None = None,
    ) -> None:
        token: contextvars.Token | None = None
        committed_revision: int | None = None
        turn_ctx = ActorTurnContext(
            chat_id=chat_id,
            actor_name=self._actor_name(),
            actor_address=self._address,
            turn_id=turn_id,
            reply_recipient=reply_recipient,
            inbound_env=inbound_env,
            base_revision=self._base_revision(inbound_env),
            channel_ref=self._channel_ref(inbound_env),
        )

        try:
            await self._on_turn_started(turn_ctx)
        except Exception as exc:
            await self._mailbox.send(
                reply_recipient,
                f"(error: {exc})",
                content_type=MessageType.SYSTEM,
                chat_id=reply_chat_id,
                metadata=self._reply_metadata(reply_recipient, inbound_env),
            )
            return

        try:
            token = CURRENT_MAILBOX.set(self._mailbox)
            event_sink = _RouteAwareMailboxEventSink(
                self._mailbox,
                reply_recipient,
                reply_chat_id,
                self._channel_metadata(inbound_env),
            )

            def _observe_commit(commit: Any) -> None:
                nonlocal committed_revision
                committed_revision = getattr(commit, "revision", None)

            response = await self._agent.ask(
                chat_id,
                content,
                turn_id=turn_id,
                interrupt=self._make_interrupt(chat_id, generation),
                ctx_metadata=self._turn_metadata(reply_recipient, inbound_env),
                event_sink=event_sink,
                commit_observer=_observe_commit,
            )
        except asyncio.CancelledError as exc:
            await self._on_turn_finished(
                turn_ctx,
                ActorTurnResult(status="aborted", committed_revision=committed_revision, error=exc),
            )
            raise
        except Exception as exc:
            await self._on_turn_finished(
                turn_ctx,
                ActorTurnResult(status="error", committed_revision=committed_revision, error=exc),
            )
            raise
        finally:
            if token is not None:
                CURRENT_MAILBOX.reset(token)

        if not self._execution_is_current(chat_id, generation):
            await self._on_turn_finished(
                turn_ctx,
                ActorTurnResult(status="stale", committed_revision=committed_revision),
            )
            return

        metadata = self._reply_metadata(reply_recipient, inbound_env)
        send_kwargs: dict[str, Any] = {"chat_id": reply_chat_id}
        if metadata:
            send_kwargs["metadata"] = metadata

        await self._mailbox.send(
            reply_recipient,
            response,
            **send_kwargs,
        )
        await self._on_turn_finished(
            turn_ctx,
            ActorTurnResult(status="completed", committed_revision=committed_revision),
        )

    async def _on_turn_started(self, ctx: ActorTurnContext) -> None:
        pass

    async def _on_turn_finished(self, ctx: ActorTurnContext, result: ActorTurnResult) -> None:
        pass

    def _turn_metadata(self, reply_recipient: str, inbound_env: Envelope | None = None) -> dict[str, Any]:
        metadata: dict[str, Any] = {"sender": reply_recipient, "actor_address": self._address}
        actor_name = self._actor_name()
        metadata["assistant_message_metadata"] = {
            "agent_name": actor_name,
            "actor_address": self._address,
        }
        if inbound_env is not None:
            target_actor = inbound_env.metadata.get("target_agent") or inbound_env.metadata.get("target_actor")
            target_display = inbound_env.metadata.get("target_display")
            workdir = inbound_env.metadata.get("workdir")
            user_metadata: dict[str, Any] = {}
            if isinstance(target_actor, str) and target_actor:
                user_metadata["target_agent"] = target_actor
                if isinstance(target_display, str) and target_display:
                    user_metadata["target_display"] = target_display
                if target_actor == actor_name and isinstance(target_display, str) and target_display:
                    metadata["assistant_message_metadata"]["agent_display"] = target_display
            if isinstance(workdir, str) and workdir:
                user_metadata["workdir"] = workdir
            if user_metadata:
                metadata["user_message_metadata"] = user_metadata
        return metadata

    def _reply_metadata(self, reply_recipient: str, inbound_env: Envelope | None = None) -> dict[str, Any]:
        channel = self._channel_metadata(inbound_env)
        return {"channel": channel} if channel else {}

    @staticmethod
    def _channel_metadata(inbound_env: Envelope | None = None) -> dict[str, Any]:
        if inbound_env is None:
            return {}
        channel = inbound_env.metadata.get("channel")
        return dict(channel) if isinstance(channel, dict) else {}

    @staticmethod
    def _base_revision(inbound_env: Envelope | None = None) -> int | None:
        if inbound_env is None:
            return None
        raw = inbound_env.metadata.get("base_revision")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        return None

    @staticmethod
    def _channel_ref(inbound_env: Envelope | None = None) -> dict[str, Any] | None:
        channel = AgentActor._channel_metadata(inbound_env)
        return channel or None

    def _actor_name(self) -> str:
        name = getattr(self._agent, "name", None)
        return str(name) if name is not None else self._address.removeprefix("agent@")

    async def _handle_command(self, env: Envelope) -> None:
        command_content = self._command_content(env)
        if command_content is None:
            await self._send_command_content_error(env, content_type=MessageType.COMMAND_RESULT)
            return

        parts = command_content.split(None, 1)
        cmd_name, input = parts[0].lstrip("/"), "" if len(parts) == 1 else parts[1]

        handler = self._COMMANDS.get(cmd_name)
        if handler is None:
            result: str | dict[str, Any] = f"Invalid command `{cmd_name}`"
        else:
            try:
                result = await handler(self, input, env)
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
    def _command_client_id(env: Envelope) -> str | None:
        routing = env.metadata.get("routing")
        if isinstance(routing, dict):
            client_id = routing.get("client_id")
            if isinstance(client_id, str) and client_id.strip():
                return client_id.strip()
        channel = env.metadata.get("channel")
        if isinstance(channel, dict):
            channel_id = channel.get("channel_id")
            conversation_id = channel.get("channel_conversation_id")
            if (
                isinstance(channel_id, str)
                and channel_id.strip()
                and isinstance(conversation_id, str)
                and conversation_id
            ):
                return f"{channel_id}:{conversation_id}"
        return None

    async def _cmd_chats(self, input: str, env: Envelope) -> dict[str, Any]:
        """List all chats, most recently active first."""
        chats = await self._agent._chat_store.list_chats()
        metas = sorted(
            chats.values(),
            key=lambda m: (m.last_activity is not None, m.last_activity),
            reverse=True,
        )
        result = [
            {
                "chat_id": m.chat_id,
                "message_count": m.message_count,
                "last_activity": m.last_activity.isoformat() if m.last_activity else None,
                "description": m.description,
            }
            for m in metas
        ]
        return {"name": "chats", "ok": True, "result": result}

    async def _cmd_prompt(self, input: str, env: Envelope) -> dict[str, Any]:
        """Show the current agent system prompt."""
        return {"name": "prompt", "ok": True, "result": await self._agent._build_system_prompt()}

    async def _cmd_new(self, input: str, env: Envelope) -> dict[str, Any]:
        """Start a new chat for the current client."""
        client_id = self._command_client_id(env)
        if client_id:
            chat_id = self._chat_state.new_chat_for_client(client_id)
        else:
            chat_id = await self.reset_chat(env)
        await self.retire_session(env.chat_id)
        return {"name": "new", "ok": True, "result": "chat reset", "chat_id": chat_id}

    async def _cmd_resume(self, input: str, env: Envelope) -> dict[str, Any]:
        """Resume a chat by id for the current client."""
        client_id = self._command_client_id(env)
        if not client_id:
            return {"name": "resume", "ok": False, "error": "Cannot resume without channel metadata."}
        if not input.strip():
            return {"name": "resume", "ok": False, "error": "Usage: /resume <chat-id>"}
        try:
            chat_id = self._chat_state.resolve_alias_or_id(input.strip())
            self._chat_state.set_cursor(client_id, chat_id)
        except ChatStateError as exc:
            return {"name": "resume", "ok": False, "error": str(exc)}
        if env.chat_id != chat_id:
            await self.retire_session(env.chat_id)
        return {"name": "resume", "ok": True, "result": f"resumed {chat_id}", "chat_id": chat_id}

    _COMMANDS = {
        "chats": _cmd_chats,
        "prompt": _cmd_prompt,
        "new": _cmd_new,
        "resume": _cmd_resume,
    }

    @staticmethod
    def _command_result_metadata(env: Envelope) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        routing = env.metadata.get("routing")
        if isinstance(routing, dict) and routing:
            metadata["routing"] = routing
        channel = env.metadata.get("channel")
        if isinstance(channel, dict) and channel:
            metadata["channel"] = dict(channel)
        return metadata

    @staticmethod
    def _command_content(env: Envelope) -> str | None:
        return env.content if isinstance(env.content, str) else None

    async def _send_command_content_error(self, env: Envelope, *, content_type: MessageType) -> None:
        await self._mailbox.send(
            env.sender,
            "(error: command content must be text)",
            content_type=content_type,
            chat_id=env.chat_id,
        )

    def _make_interrupt(self, chat_id: str, generation: int):
        def _interrupt() -> dict[str, Any] | None:
            if not self._generation_is_current(chat_id, generation):
                raise AbortTurn()

            session = self._sessions.get(chat_id)
            if session is None:
                return None

            buf = session.interrupts
            parts: list[str] = []
            remaining: list[Envelope] = []
            for env in buf:
                if env.content_type == MessageType.INTERRUPT_ABORT:
                    session.interrupts = remaining
                    raise AbortTurn()
                if env.content_type == MessageType.INTERRUPT_MESSAGE:
                    parts.append(f"[from {env.sender}]: {env.content}")
                else:
                    remaining.append(env)
            session.interrupts = remaining
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
