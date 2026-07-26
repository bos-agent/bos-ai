from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bos.core.actor import Actor, Envelope, EventBus, MailBox, MessageType
from bos.core.agent import AbortTurn, Agent, MessageContent, TurnEvent
from bos.core.contract import SessionEvent, SessionEventKind
from bos.core.sinks import CLIENT_TURN_EVENT_TYPES, HostChannelSink, MailboxEventSink

from ..core.chat_coordinator import ChannelConversationRef, ChatCoordinationError, ChatCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# How long a hard stop waits for cancelled turn tasks to unwind before giving up
# on them. The process is going down; a task that will not unwind must not take
# the rest of teardown with it.
_FORCED_CANCEL_SECONDS = 5.0


@dataclass
class SessionExecution:
    task: asyncio.Task | None = None
    generation: int = 0
    reply_recipient: str | None = None
    reply_chat_id: str | None = None
    # Most recent ChatCommit.revision the actor observed for this session.
    # `retire_session` emits session_close with it so the event carries a real
    # base_revision without an out-of-band chat_store lookup.
    last_committed_revision: int | None = None


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
        metadata: dict[str, Any] = {"turn_id": event.turn_id, "event_type": event.event_type}
        if self._channel_metadata:
            metadata["channel"] = self._channel_metadata
        await self._mailbox.send(
            self._recipient,
            json.dumps(event.to_payload(), default=str),
            content_type=MessageType.TURN_EVENT,
            chat_id=self._chat_id,
            metadata=metadata,
        )


class AgentActor(Actor):
    """Actor that drives an Agent via a bound MailBox.

    Sessions are keyed by ``chat_id``.  Each distinct ``chat_id`` gets its
    own concurrent task slot, interrupt buffer, and generation counter.
    Messages arriving during an active turn are rejected immediately.

    Specializes the foundation ``Actor``: the mailbox pump, background-task
    lifecycle, and event emission come from the base; this class adds the
    turn/session/interrupt model and the ``SessionEvent`` vocabulary.
    """

    def __init__(
        self,
        agent: Agent,
        mailbox: MailBox,
        *,
        event_bus: EventBus | None = None,
        chat_coordinator: ChatCoordinator | None = None,
    ):
        super().__init__(mailbox, event_bus=event_bus)
        self._agent = agent
        self._sessions: dict[str, SessionState] = {}
        # Optional turn fencing for multi-client gateway hosting. When absent
        # (e.g. a bare actor in a test), the turn hooks below are no-ops beyond
        # the session-event emit.
        self._chat_coordinator = chat_coordinator
        self._draining = False

    async def drain(self, grace: float) -> None:
        """Close in-flight turns cooperatively, within *grace* seconds.

        Shutdown does not wait for a turn's work to finish — a turn can run for
        minutes. It asks the agent to stop, and each running turn abandons what
        it was awaiting and closes with a handoff, which travels out over the
        normal reply path so the waiting client gets an actionable answer
        instead of a dropped connection. Whatever misses the deadline is
        cancelled by ``aclose`` as before, so the drain is bounded.

        New turns are refused for the rest of the process's life; the caller is
        expected to be shutting down."""
        self._draining = True
        self._agent.request_stop()
        tasks = [s.execution.task for s in self._sessions.values() if s.execution.task is not None]
        if not tasks or grace <= 0:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace)
        if pending:
            logger.warning(
                "Shutdown grace of %.1fs expired with %d turn(s) still closing; cancelling them",
                grace,
                len(pending),
            )

    async def aclose(self) -> None:
        session_tasks = [s.execution.task for s in self._sessions.values() if s.execution.task is not None]
        for task in session_tasks:
            task.cancel()
        if session_tasks:
            _, pending = await asyncio.wait(set(session_tasks), timeout=_FORCED_CANCEL_SECONDS)
            if pending:
                logger.warning(
                    "%d turn task(s) did not unwind within %.1fs of cancellation; abandoning them",
                    len(pending),
                    _FORCED_CANCEL_SECONDS,
                )
        for session in self._sessions.values():
            session.execution.task = None
        await super().aclose()  # drains command tasks tracked by the base

    async def _on_idle_tick(self) -> None:
        # Reap completed turn tasks (and surface their errors) once per loop.
        for chat_id in list(self._sessions.keys()):
            await self._finalize_done_task(chat_id)

    async def handle(self, env: Envelope) -> None:
        if not env.chat_id:
            await self._mailbox.send(
                env.sender,
                "(error: missing chat_id)",
                content_type=MessageType.SYSTEM,
            )
            return

        chat_id = env.chat_id

        if self._draining and env.content_type == MessageType.MESSAGE:
            # Admitting a turn we are about to stop would burn a model call and
            # leave the sender waiting on a reply that never lands.
            await self._mailbox.send(
                env.sender,
                "(unavailable: the agent is shutting down)",
                content_type=MessageType.SYSTEM,
                chat_id=chat_id,
                metadata={"event": "shutting_down"},
            )
            return

        session = self._get_or_create_session(chat_id)

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
            return

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
        await self._emit_session_event(
            "session_close",
            chat_id=chat_id,
            actor_name=self._actor_name(),
            turn_id=None,
            base_revision=session.execution.last_committed_revision,
        )

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
            mailbox_sink = _RouteAwareMailboxEventSink(
                self._mailbox,
                reply_recipient,
                reply_chat_id,
                self._channel_metadata(inbound_env),
            )
            event_sink = self._build_host_sink(turn_ctx, mailbox_sink)

            def _observe_commit(commit: Any) -> None:
                nonlocal committed_revision
                committed_revision = getattr(commit, "revision", None)
                session_now = self._sessions.get(chat_id)
                if session_now is not None and committed_revision is not None:
                    session_now.execution.last_committed_revision = committed_revision

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
        # Fence the turn through the coordinator (multi-client gateway hosting).
        if self._chat_coordinator is None:
            return
        ref = _ctx_channel_ref(ctx)
        if ref is None:
            raise ChatCoordinationError("Gateway actor turn is missing channel metadata.")
        if ctx.base_revision is None:
            raise ChatCoordinationError("Gateway actor turn is missing base_revision.")
        await self._chat_coordinator.begin_turn(
            chat_id=ctx.chat_id,
            ref=ref,
            actor=ctx.actor_name,
            turn_id=ctx.turn_id,
            base_revision=ctx.base_revision,
        )

    async def _on_turn_finished(self, ctx: ActorTurnContext, result: ActorTurnResult) -> None:
        if self._chat_coordinator is not None:
            self._chat_coordinator.end_turn(
                chat_id=ctx.chat_id,
                turn_id=ctx.turn_id,
                committed_revision=result.committed_revision,
            )
        # A completed turn is a generic platform session event.
        if result.status == "completed":
            await self._emit_session_event(
                "turn_complete",
                chat_id=ctx.chat_id,
                actor_name=ctx.actor_name,
                turn_id=ctx.turn_id,
                base_revision=result.committed_revision,
            )
        if self._chat_coordinator is not None and result.status in ("aborted", "error") and ctx.reply_recipient:
            # Aborted/errored turns send no reply envelope, but may have
            # committed history and advanced the chat revision. Notify the
            # originating channel so its revision cursor re-syncs; otherwise the
            # client's next send is rejected as stale.
            if result.status == "aborted":
                content, event = "Turn aborted.", "turn_aborted"
            else:
                content, event = f"Turn failed: {result.error}", "turn_error"
            await self._mailbox.send(
                ctx.reply_recipient,
                content,
                content_type=MessageType.SYSTEM,
                chat_id=ctx.chat_id,
                metadata={"event": event},
            )

    async def _emit_session_event(
        self,
        kind: SessionEventKind,
        *,
        chat_id: str,
        actor_name: str | None,
        turn_id: str | None,
        base_revision: int | None,
    ) -> None:
        """Emit a session event on the bus (via the base ``emit``,
        which no-ops when no bus is wired and isolates handler failures). The
        bus is the single generic channel for turn_complete / session_close —
        the actor names no plugin; plugins subscribe to react (recall flush,
        consolidation)."""
        await self.emit(
            SessionEvent(
                kind=kind,
                chat_id=chat_id,
                actor_name=actor_name,
                base_revision=base_revision,
                turn_id=turn_id,
                payload={},
            )
        )

    def _build_host_sink(self, ctx: ActorTurnContext, mailbox_sink: MailboxEventSink) -> HostChannelSink:
        """Construct the per-turn event sink. The base actor registers the
        mailbox forwarder for every client-facing event type — that's the
        wire protocol clients see. Subclasses may override and call
        ``super()._build_host_sink(...)`` to extend the sink with additional
        handlers (e.g. capturing internal events into a turn-local buffer)."""
        sink = HostChannelSink()
        for event_type in CLIENT_TURN_EVENT_TYPES:
            sink.on(event_type, mailbox_sink.emit)
        return sink

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

    def _make_interrupt(self, chat_id: str, generation: int) -> Callable[[], dict[str, Any] | None]:
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
        return session is not None and session.execution.generation == generation and session.execution.task is not None


def _ctx_channel_ref(ctx: ActorTurnContext) -> ChannelConversationRef | None:
    raw = ctx.channel_ref
    if not isinstance(raw, dict):
        return None
    channel_id = raw.get("channel_id")
    conversation_id = raw.get("channel_conversation_id")
    if not isinstance(channel_id, str) or not isinstance(conversation_id, str):
        return None
    return ChannelConversationRef(channel_id=channel_id, channel_conversation_id=conversation_id)
