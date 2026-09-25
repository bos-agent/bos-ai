"""The Codex vendor runtime (BEP 19 Layer 4a).

``CodexAgent`` is BOS's ``ExternalRuntime`` adapter over ``openai_codex.AsyncCodex``,
the vendor SDK that talks to a lazily-spawned ``codex app-server`` child process.
It implements ``AgentPort`` without being a ``bos.core.agent.Agent``: it holds no
``LLM``, runs none of BOS's turn loop, and its permission enforcement is the
vendor's own OS-level sandbox rather than anything BOS assembles — BEP 19 §3.5.2:
"This is real confinement, and it is the one BOS does not have to build."

This module imports ``openai_codex`` at module scope. That is safe here, and only
here: it is reached exclusively through ``importlib``, from
``bos.core.harness._load_external_runtime``. Nothing under ``bos/core/`` or
``bos/sdk/`` may import this module or the vendor package directly, so a base
install with neither extra never touches either.

Stage 4 of 4 over ``CodexAgent``'s turn path: construction (Task 3), the
client and thread lifecycle (Task 4), a turn that runs and persists itself
including schema-validated structured output (BEP 12 semantics, via the
injected ``StructuredValidator`` — BEP 19 §3.2, §3.9) (Task 5), that turn made
observable via ``_emit_stream``, which streams ``AsyncTurnHandle.stream()``
into BOS ``TurnEvent``s as they arrive rather than awaiting one final result
(BEP 19 §3.9) (Task 6), and here — the control surface: ``_run_turn`` races
each native turn against a cooperative stop and ``timeout_seconds``, telling
apart a caller's deadline (raised), BOS taking the turn away (kept, partial),
and an unexplained vendor-side interruption (raised, as before), even though
all three reach here as the same ``TurnStatus.interrupted``; ``run()``'s
``self._in_flight`` busy guard rejects a second turn on a chat_id already
running one; and ``aclose()`` interrupts every in-flight turn with a bounded
wait before closing the client regardless (BEP 19 §3.10.2) (Task 7). Task 8
adds the approval handler on top of this.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, TypeVar, cast

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    ImageInput,
    Input,
    InputItem,
    LocalImageInput,
    MentionInput,
    Sandbox,
    TextInput,
    TurnResult,
)
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    McpToolCallThreadItem,
    MessagePhase,
)
from openai_codex.models import ItemCompletedNotification, ItemStartedNotification, Notification
from openai_codex.types import (
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)

from bos.core.agent import (
    ABORTED_TURN_CONTENT,
    AbortTurn,
    AgentEventType,
    AgentResult,
    ChatStore,
    MessageContent,
    StructuredOutputError,
    StructuredValidator,
    TurnEvent,
    TurnEventPhase,
    TurnEventSink,
    _apply_async,
    _compact,
    content_as_parts,
)
from bos.extensions.runtimes._shared import (
    commit_external_turn,
    external_agent_result,
    parse_external_config,
    read_native_session_id,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Patched in tests to inject FakeAsyncCodex — the only seam the double needs.
_CODEX_FACTORY: Callable[..., Any] = AsyncCodex

# BEP 19 §3.5: Codex's confinement is an OS sandbox the vendor enforces, so the
# whole permission story is these two enums — no BOS-side tool interception
# like Claude Code needs (§3.5.2 vs §3.5.3). `full-access` still uses
# `auto_review`, not because approvals matter once the sandbox is open, but
# because `deny_all` would block turns from proceeding at all.
_SANDBOX_AND_APPROVAL: dict[str, tuple[Sandbox, ApprovalMode]] = {
    "read-only": (Sandbox.read_only, ApprovalMode.deny_all),
    "workspace-write": (Sandbox.workspace_write, ApprovalMode.auto_review),
    "full-access": (Sandbox.full_access, ApprovalMode.auto_review),
}

# How long a turn (or aclose(), across all of them) waits for the native side
# to confirm an interrupt before giving up on it — mirrors
# Agent._ABANDON_TEARDOWN_SECONDS (agent.py) and the same reasoning: a codex
# app-server that delays or ignores the request must not be able to hold a
# stop, a timeout, or aclose() open (BEP 19 §3.10.2).
_INTERRUPT_GRACE_SECONDS = 2.0

# The outer bound aclose() puts on draining every in-flight turn. Must stay
# above _settle_interrupted's worst case (3 x the grace: the interrupt RPC,
# the drain wait, the post-cancel wait), or aclose() reports turns that are
# winding down normally as stuck.
_ACLOSE_GRACE_SECONDS = 10.0

# The bound on the auth preflight's account() RPC. Deliberately NOT one of the
# two above: those are teardown graces, measured against a turn that is already
# being given up on, while this is a startup credential check against a live
# service — a slow but working login must not trip it. Bounded at all because
# account() is the same unbounded request path as interrupt(): account() ->
# account_read -> _call_sync -> asyncio.to_thread -> CodexClient._request_raw
# -> `waiter.get()` (client.py:382), a queue read with no timeout. And
# _preflight_auth holds _client_lock while it waits, which is the lock
# aclose() needs before it can reach client.close() — so an unanswered
# credential check would otherwise hold the whole harness shutdown open and
# leave the child unreaped. What the bound frees is the event loop and the
# lock; the to_thread worker stays parked on that queue until the process
# ends, exactly as it does for a wedged interrupt(). That is a thread, not a
# turn, and it is the vendor's to fix.
_PREFLIGHT_AUTH_SECONDS = 30.0

# The rest of the audit those three constants are half of, stated once so the
# next reader does not have to redo it — and stated as it is, not as "every
# wait is bounded", which was the round-2 prose that made an unbounded
# account() a finding rather than a known gap:
#
# - `client.close()` bounds itself: CodexClient.close is proc.terminate() ->
#   proc.wait(timeout=2) -> kill() -> two join(timeout=0.5), ~3s worst case.
# - The stream iteration and `handle.steer()` sit inside _run_turn's
#   `asyncio.timeout(timeout_seconds)` AND are raced against _stop_requested,
#   so _settle_interrupted's cancel bounds them even when timeout_seconds is
#   None.
# - `thread_start` / `thread_resume` / `thread.turn` are awaited *outside*
#   that timeout window — they run before _run_turn exists — so each carries
#   its own `wait_for(..., timeout_seconds)` instead (_bounded_setup, fix
#   round 4). Per *attempt*, which is what timeout_seconds already means
#   here: the schema-retry loop gives every attempt a fresh asyncio.timeout,
#   so a turn with retries can already take a multiple of it. It is not a
#   whole-call deadline, and round 4 did not make it one.
# - With `timeout_seconds = None` nothing above is bounded by it, because the
#   caller declined a deadline. A wedged setup is still recoverable: the
#   busy-guard slot is still None while setup runs, so aclose() waits on no
#   task, takes the uncontended _client_lock and closes the client — which
#   fails the pending RPC.


def _content_to_codex_input(content: MessageContent) -> Input | str:
    """BOS ``MessageContent`` -> a Codex ``Input`` (BEP 19 §3.9).

    Typed ``Input | str`` rather than the wider ``RunInput`` (``Input | str |
    ExternalMessage``) that ``thread.turn()`` accepts: this never produces an
    ``ExternalMessage``, and the narrower type is what ``AsyncTurnHandle.steer()``
    (Task 7 fix round 1) requires, so the same conversion serves both a new
    turn's content and a steering message's, honestly — not by widening
    ``steer()``'s accepted type with a ``cast``.

    A plain string is already valid input (the SDK wraps it in a
    ``TextInput`` itself) and passes straight through unchanged. A list of BOS
    parts is mapped item by item:

    - ``TextPart`` -> ``TextInput``.
    - ``ImagePart`` -> ``ImageInput`` for a url/data source, or
      ``LocalImageInput`` for a path — Codex reads that path itself, since it
      runs against this same filesystem; there is no reason to base64-encode
      it the way a remote-only provider would.
    - ``FilePart`` -> ``MentionInput``, named after the file since a BOS
      ``FilePart`` carries no separate display name. Codex's mention mechanism
      has no wire form for a remote file, so a url-sourced ``FilePart`` is
      rejected rather than silently dropped or mis-sent as a local path.

    ``content_as_parts`` validates as well as normalizes, so every part
    reaching the loop below is already one of exactly ``text``/``image``/
    ``file`` — the three kinds ``_content.py`` currently defines.
    """
    if isinstance(content, str):
        return content
    items: list[InputItem] = []
    for part in content_as_parts(content):
        part_type = part.get("type")
        if part_type == "text":
            items.append(TextInput(text=part["text"]))
        elif part_type == "image":
            source = part["source"]
            if source["kind"] == "url":
                items.append(ImageInput(url=source["value"]))
            else:
                items.append(LocalImageInput(path=source["value"]))
        elif part_type == "file":
            source = part["source"]
            if source["kind"] != "path":
                raise ValueError(
                    f"codex runtime: a FilePart sent to Codex must be a local path, not a "
                    f"{source['kind']!r} source ({source['value']!r}); Codex has no wire form "
                    "for mentioning a remote file."
                )
            items.append(MentionInput(name=Path(source["value"]).name, path=source["value"]))
    return items


def _unwrap_thread_item(item: ThreadItem) -> Any:
    """``ThreadItem`` is a pydantic ``RootModel`` union; unwrap it to the concrete
    variant before an ``isinstance`` check, exactly as ``openai_codex/_run.py``
    (lines 36-40) does ahead of its own ``isinstance`` checks. The ``hasattr``
    guard — rather than assuming ``.root`` is always present — is the vendor's
    own hedge against a future item that arrives unwrapped; mirrored verbatim
    rather than simplified to a bare ``.root``.
    """
    return item.root if hasattr(item, "root") else item


def _tool_name_for(item: Any) -> str | None:
    """The two ``ThreadItem`` variants BEP 19 §3.9 maps to a ``tool`` event.

    ``CommandExecutionThreadItem`` has no separate "tool name" field — the
    command being run *is* the tool identity — so its own ``command`` string
    is what a host has to show. ``McpToolCallThreadItem`` names its tool
    directly. Every other variant (``FileChangeThreadItem``,
    ``ReasoningThreadItem``, a future addition, ...) returns ``None``: BOS has
    no ``tool`` vocabulary for it, so the caller skips the notification
    instead of emitting a half-populated event.
    """
    if isinstance(item, CommandExecutionThreadItem):
        return item.command
    if isinstance(item, McpToolCallThreadItem):
        return item.tool
    return None


def _raise_for_failed_turn(turn: Turn) -> None:
    """Mirrors ``openai_codex._run._raise_for_failed_turn`` exactly.

    ``_collect_async_turn_result`` calls this on the completed ``Turn`` before
    ever building a ``TurnResult``, so a failed turn never reaches its caller
    as a normal result on the streaming path either — the same place
    ``AsyncThread.run()`` raises for Task 5's plain (non-streaming) call.
    Reimplemented rather than imported: this is vendor-internal (leading
    underscore) code, so a citation in a comment is the stable reference,
    not a runtime dependency on a module that owes us no compatibility.
    """
    if turn.status is not TurnStatus.failed:
        return
    if turn.error is not None and turn.error.message:
        raise RuntimeError(turn.error.message)
    raise RuntimeError(f"turn failed with status {turn.status.value}")


def _final_assistant_response_from_items(items: list[ThreadItem]) -> str | None:
    """Mirrors ``openai_codex._run._final_assistant_response_from_items`` exactly:
    the *last* ``AgentMessageThreadItem`` whose ``phase`` is ``final_answer``,
    or — only when none exists — the last one with no phase at all. A
    ``commentary``-phase message is neither: it is not the answer, and it is
    not a fallback candidate either, so it is silently skipped either way.
    """
    last_unknown_phase_response: str | None = None
    for item in reversed(items):
        thread_item = _unwrap_thread_item(item)
        if not isinstance(thread_item, AgentMessageThreadItem):
            continue
        if thread_item.phase is MessagePhase.final_answer:
            return thread_item.text
        if thread_item.phase is None and last_unknown_phase_response is None:
            last_unknown_phase_response = thread_item.text
    return last_unknown_phase_response


class TurnNotCompletedError(RuntimeError):
    """A Codex turn ended without producing a usable answer, on a status Codex
    itself hands back as a normal ``TurnResult`` rather than raising for.

    Today that is only ``interrupted`` — ``failed`` is raised by the vendor
    SDK before it ever constructs a ``TurnResult`` (see the comment in
    ``run()``), so it never reaches this exception. Carries ``turn_result`` so
    a caller has more than a message to work with.

    Not every ``interrupted`` turn raises this since Task 7 (BEP 19 §3.10.2). A
    cooperative stop — ``request_stop()`` racing the turn — returns the kept
    partial answer instead, mirroring ``Agent``'s own contract for the same
    situation; a ``timeout_seconds`` expiry raises a plain ``TimeoutError``
    instead, so a caller can catch a deadline it imposed itself without also
    catching this. What is left for this exception is an ``interrupted``
    status this runtime never itself asked for — see ``run()``'s own
    docstring for the full three-way split.

    The actor's own per-chat ``interrupt`` callback (BEP 19 §3.9's
    ``interrupt`` row) is not one of the three: since fix round 1, a truthy
    return steers the running turn (``AsyncTurnHandle.steer()``) rather than
    ending it, so it never produces ``interrupted`` at all — see
    ``_emit_stream``'s docstring.
    """

    def __init__(self, message: str, *, turn_result: TurnResult) -> None:
        super().__init__(message)
        self.turn_result = turn_result


class CodexAgent:
    """``ExternalRuntime`` adapter over the Codex vendor SDK (BEP 19 §3.4, §3.5.1).

    One instance per ``create_agent`` call. It owns one ``AsyncCodex`` client,
    built lazily on first turn (§3.1) rather than here — constructing an agent
    must never spawn the ``codex app-server`` child.
    """

    def __init__(
        self,
        *,
        kind: str,
        cfg: Mapping[str, Any],
        chat_store: ChatStore | None,
        workspace: Path,
        mcp: Callable[[], Any],
        structured_validator: StructuredValidator,
    ) -> None:
        self._kind = kind
        self._chat_store = chat_store
        self._mcp = mcp
        self._structured_validator = structured_validator
        self._config = parse_external_config(dict(cfg), runtime="codex", workspace=Path(workspace))
        self._client: AsyncCodex | None = None
        self._client_lock = asyncio.Lock()
        self._stop_requested = asyncio.Event()
        # chat_id -> the task consuming that chat's in-flight turn (BEP
        # §3.10.1's busy guard). Reserved (value None) the instant run() is
        # entered, before any await can interleave a second call for the same
        # chat_id; filled in once _run_turn creates the real task, so aclose()
        # has something to wait on and a concurrent run() has something to see.
        self._in_flight: dict[str, asyncio.Task[TurnResult] | None] = {}

    @property
    def name(self) -> str:
        return self._kind

    @property
    def resolved_config(self) -> Mapping[str, Any]:
        """The *resolved* config ``boscli inspect`` reads (BEP 19 §8.2): an
        absolute ``cwd`` and a validated ``permission``, not the raw input the
        constructor was handed."""
        return MappingProxyType(
            {
                "external_runtime": self._config.runtime,
                "cwd": str(self._config.cwd),
                "permission": self._config.permission,
                "system_prompt": self._config.system_prompt,
                "base_instructions": self._config.base_instructions,
                "model": self._config.model,
                "auth": self._config.auth,
                "timeout_seconds": self._config.timeout_seconds,
                "mcp_tools": list(self._config.mcp_tools),
                "native_options": dict(self._config.native_options),
            }
        )

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def _ensure_client(self) -> AsyncCodex:
        """Build the one ``AsyncCodex`` this agent owns, lazily (BEP 19 §3.1, §3.10.1).

        Guarded by a lock so two concurrent turns on different ``chat_id``s
        under the same agent cannot each build and orphan their own client —
        the SDK's own ``AsyncCodex._ensure_initialized`` guards itself the
        same way, for the same reason.
        """
        async with self._client_lock:
            if self._client is None:
                client: AsyncCodex = _CODEX_FACTORY(CodexConfig())
                if self._config.auth == "subscription":
                    await self._preflight_auth(client)
                self._client = client
            return self._client

    async def _preflight_auth(self, client: AsyncCodex) -> None:
        """BEP 19 §3.10.3: with ``auth="subscription"``, fail loudly here —
        once, before the client is cached, never per turn — rather than let a
        missing login silently fall back to billed API-key usage later.

        A client that fails this check is never handed to a caller: on either
        failure below we best-effort close it (it may already have spawned
        the ``codex app-server`` child to answer ``account()`` at all) so a
        later retry, once the operator has actually logged in, starts clean
        instead of piling up abandoned children.
        """
        runtime = self._config.runtime
        try:
            response = await asyncio.wait_for(client.account(), _PREFLIGHT_AUTH_SECONDS)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.close()
            # wait_for's TimeoutError carries no message, and "the account
            # check failed: " with nothing after it tells an operator nothing.
            detail = (
                f"did not answer within {_PREFLIGHT_AUTH_SECONDS}s"
                if isinstance(exc, TimeoutError)
                else f"failed: {exc}"
            )
            raise RuntimeError(
                f'{runtime} runtime {self._kind!r}: auth="subscription" but the account check {detail}'
            ) from exc
        if response.account is None:
            with contextlib.suppress(Exception):
                await client.close()
            raise RuntimeError(
                f'{runtime} runtime {self._kind!r}: auth="subscription" but no Codex account is '
                f'logged in. Run `codex login`, or set auth="api_key" to opt out of this check.'
            )

    async def _bounded_setup(self, awaitable: Awaitable[_T], *, phase: str, chat_id: str, turn_id: str) -> _T:
        """Bound one vendor *setup* RPC with ``timeout_seconds`` (fix round 4).

        ``thread_start`` / ``thread_resume`` / ``thread.turn`` are awaited
        before :meth:`_run_turn` exists to wrap them in ``asyncio.timeout``,
        so without this a child that wedges during setup hangs the turn with
        the caller's own deadline never firing.

        Bounded per *attempt*, which is what ``timeout_seconds`` already means
        here — the schema-retry loop gives every attempt its own fresh
        ``asyncio.timeout``, so a turn with retries can already take a
        multiple of it. This does **not** make it a whole-call deadline; that
        would be a real semantic change and is not what this is.

        Nothing is interrupted on expiry, unlike :meth:`_settle_interrupted`'s
        paths. During setup there is nothing to tell the vendor about: no
        stream task exists, no handle exists yet, and the busy-guard slot is
        still ``None``. ``wait_for`` cancelling the RPC is the whole teardown.

        ``timeout_seconds=None`` means the caller declined a deadline, and
        this declines one too rather than inventing a fallback. A wedge is
        still recoverable in that case, for the same reason there is nothing
        to interrupt: no turn task is registered, so ``aclose()`` waits on
        none, takes the uncontended ``_client_lock`` and closes the client —
        which fails the pending RPC.

        The message names the phase, so a setup timeout is never mistaken for
        a turn that timed out while streaming.
        """
        try:
            return await asyncio.wait_for(awaitable, self._config.timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                f"{self._config.runtime} runtime {self._kind!r}: {phase} for turn {turn_id!r} on "
                f"chat {chat_id!r} exceeded timeout_seconds={self._config.timeout_seconds!r}"
            ) from exc

    async def _thread_for(self, chat_id: str, *, turn_id: str) -> tuple[AsyncThread, bool]:
        """Map *chat_id* onto a Codex thread (BEP 19 §3.6): resume the thread on
        record for this chat, or start a fresh one when there is none.

        Returns ``(thread, started)`` — *started* is True only when a new
        thread was opened. A thread on record that Codex can no longer resume
        (archived, deleted, expired) is raised as an error naming the runtime
        and the thread id; it is never silently replaced with a new thread
        under the same ``chat_id``, which would drop the user's conversation
        mid-way with no signal.
        """
        client = await self._ensure_client()
        native_session_id = (
            await read_native_session_id(self._chat_store, chat_id, runtime=self._config.runtime)
            if self._chat_store is not None
            else None
        )
        sandbox, approval_mode = _SANDBOX_AND_APPROVAL[self._config.permission]
        thread_kwargs: dict[str, Any] = {
            "sandbox": sandbox,
            "approval_mode": approval_mode,
            "cwd": str(self._config.cwd),
            "model": self._config.model,
            "developer_instructions": self._config.system_prompt,
            "base_instructions": self._config.base_instructions,
        }
        if native_session_id is None:
            thread = await self._bounded_setup(
                client.thread_start(**thread_kwargs),
                phase="thread setup (thread_start)",
                chat_id=chat_id,
                turn_id=turn_id,
            )
            return thread, True
        try:
            thread = await self._bounded_setup(
                client.thread_resume(native_session_id, **thread_kwargs),
                phase="thread setup (thread_resume)",
                chat_id=chat_id,
                turn_id=turn_id,
            )
        except TimeoutError:
            # Deliberately ahead of the wrap below: a child that never answers
            # is not a thread BOS can no longer resume. Re-labelling it as the
            # session-continuity error would send an operator hunting for a
            # corrupt or expired session when the real answer is a wedged
            # child.
            raise
        except Exception as exc:
            raise RuntimeError(
                f"{self._config.runtime} runtime {self._kind!r}: thread {native_session_id!r} for "
                f"chat {chat_id!r} could not be resumed, and BOS does not silently start a fresh "
                f"session under the same chat_id (BEP 19 §3.6): {exc}"
            ) from exc
        return thread, False

    def _event(
        self,
        *,
        chat_id: str,
        turn_id: str,
        event_type: str,
        phase: str,
        detail: str | None = None,
        tool_name: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnEvent:
        return TurnEvent(
            event_type=event_type,
            phase=phase,
            chat_id=chat_id,
            turn_id=turn_id,
            agent_name=self._kind,
            detail=detail,
            tool_name=tool_name,
            content=content,
            metadata=dict(metadata or {}),
        )

    def _event_for_notification(
        self,
        notification: Notification,
        *,
        chat_id: str,
        turn_id: str,
        metadata: dict[str, Any] | None,
    ) -> TurnEvent | None:
        """BEP 19 §3.9's mapping. ``Notification.method`` drives it, refined by
        the concrete ``ThreadItem`` variant for ``item/started``/``item/completed``:

        - ``item/started`` on a ``CommandExecutionThreadItem``/``McpToolCallThreadItem``
          -> ``tool``/``start``.
        - the matching ``item/completed`` -> ``tool``/``finish``.
        - ``item/completed`` on an ``AgentMessageThreadItem`` -> ``response``/``finish``.
        - ``turn/completed`` -> ``turn``/``finish``.

        Anything else — an unrecognised ``method``, or an item variant BOS has
        no ``tool`` vocabulary for (a ``FileChangeThreadItem``, a future
        addition, ...) — returns ``None``. The vendor adds notification and
        item kinds between releases; a turn must not die because BOS has not
        heard of one yet, so this is a lookup that misses cleanly, never a
        raise.
        """
        payload = notification.payload
        if notification.method == "item/started" and isinstance(payload, ItemStartedNotification):
            tool_name = _tool_name_for(_unwrap_thread_item(payload.item))
            if tool_name is None:
                return None
            return self._event(
                chat_id=chat_id,
                turn_id=turn_id,
                event_type=AgentEventType.tool,
                phase=TurnEventPhase.start,
                tool_name=tool_name,
                metadata=metadata,
            )
        if notification.method == "item/completed" and isinstance(payload, ItemCompletedNotification):
            item = _unwrap_thread_item(payload.item)
            if isinstance(item, AgentMessageThreadItem):
                return self._event(
                    chat_id=chat_id,
                    turn_id=turn_id,
                    event_type=AgentEventType.response,
                    phase=TurnEventPhase.finish,
                    content=item.text,
                    metadata=metadata,
                )
            tool_name = _tool_name_for(item)
            if tool_name is None:
                return None
            return self._event(
                chat_id=chat_id,
                turn_id=turn_id,
                event_type=AgentEventType.tool,
                phase=TurnEventPhase.finish,
                tool_name=tool_name,
                metadata=metadata,
            )
        if notification.method == "turn/completed" and isinstance(payload, TurnCompletedNotification):
            return self._event(
                chat_id=chat_id,
                turn_id=turn_id,
                event_type=AgentEventType.turn,
                phase=TurnEventPhase.finish,
                metadata=metadata,
            )
        return None

    async def _emit_stream(
        self,
        handle: AsyncTurnHandle,
        sink: TurnEventSink | None,
        *,
        chat_id: str,
        turn_id: str,
        ctx_metadata: dict[str, Any] | None,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
    ) -> TurnResult:
        """Consume ``handle.stream()``, translating each ``Notification`` into a
        ``TurnEvent`` for ``sink`` while accumulating exactly what
        ``openai_codex._run._collect_async_turn_result`` does, so the
        ``TurnResult`` this returns is what a plain ``await handle.run()``
        would have produced (BEP 19 §3.9) — the only difference is that the
        caller also got to watch it happen.

        ``sink`` may be ``None`` (the common case for ``_HarnessAgentRunner``):
        the turn still streams and collects, translation is just skipped.
        Emitting is best-effort — a sink that raises must not end the turn,
        mirroring how ``Agent._emit_event`` guards ``event_sink.emit``
        (``agent.py``).

        ``interrupt`` — ``AgentActor``'s poll-style callback (BEP 19 §3.9's
        `interrupt` row) — is polled once per notification, exactly as
        ``Agent._interrupt`` reads the same callback in ``agent.py``, with one
        exception: the terminal ``turn/completed`` notification. Polling is
        destructive (``AgentActor._make_interrupt`` *pops* the pending
        envelopes off the session), and once the turn has completed there is
        nothing left to steer into — so a poll there would take the user's
        message and have nowhere to put it (fix round 2, I3):

        - **A truthy return is a message to deliver, not a request to stop.**
          Fix round 1: Task 7's first cut read the brief's "`interrupt`
          callback -> ``AsyncTurnHandle.interrupt()``" table row (BEP 19
          §3.9) as "fires -> stop", but ``Agent._interrupt`` itself —
          ``if interrupt and (llm_message := await _apply_async(interrupt, {})):
          ctx.add_message(llm_message, merge=True)`` — merges the returned
          LLM message dict into the *running* turn's context; the turn
          continues. `AgentActor._make_interrupt` (`agent_actor.py:543`)
          returns exactly that shape for a queued ``INTERRUPT_MESSAGE`` (a
          user's follow-up sent while the turn is still going). Ending the
          turn instead — what this used to do — killed that follow-up rather
          than delivering it. Codex's own primitive for "deliver input to the
          turn that's already running" is ``AsyncTurnHandle.steer()``
          ("Send additional user input to this active turn"); only ``review``
          and ``compact`` turn kinds refuse it
          (``NonSteerableTurnKind``/``ActiveTurnNotSteerable``), an ordinary
          chat turn is always steerable, and it does not end or replace the
          turn — the same ``handle``/``turn_id`` keeps streaming afterward.
          So a truthy return's ``"content"`` value — a BOS ``MessageContent``,
          the same shape ``TurnContext.add_message``'s merge branch expects,
          not the whole ``dict`` — is converted with
          :func:`_content_to_codex_input` and handed to ``handle.steer()``.
          The steer call itself is best-effort — a failed steer RPC is a
          network blip, not a turn failure, and the turn may still finish
          normally without it — but it is **logged at WARNING**, not
          swallowed: unlike a failed ``handle.interrupt()`` (a courtesy BOS
          sends on its own behalf), what is lost here is a message a user
          typed, and nothing else in the system records that it existed.
        - **Raising unwinds the turn.** ``Agent._interrupt`` does not catch
          ``AbortTurn`` either — it is meant to unwind the turn, not be
          absorbed here. Nothing in *this* method catches it (or anything
          else the callback raises): it propagates out of the ``async for``
          (through the ``finally`` below, so the stream is still closed),
          out of this coroutine, and — via :meth:`_run_turn`'s
          ``stream_task.result()`` — up to ``run()``, which is where the
          unwinding stops: ``run()`` catches ``AbortTurn`` and returns the
          aborted-turn marker, matching what ``Agent`` hands its own caller
          (see ``run()``'s docstring; fix round 2, I4). Any *other* exception
          the callback raises is wrapped by ``run()`` as a turn failure.
          Either way :meth:`_run_turn` interrupts the native turn on its way
          past (fix round 3, D) — unwinding BOS-side alone would leave the
          child running against ``cwd`` with nothing left that knows about it.
        - **A falsy return does nothing.** No steer, no interrupt, no
          state change — the turn is not even aware the callback fired.

        None of this produces ``TurnStatus.interrupted`` (steering keeps the
        turn running; raising unwinds it some other way), so the callback
        plays no part in the three-way ``interrupted`` split ``run()``'s
        docstring and :class:`TurnNotCompletedError` describe — that split is
        ``request_stop()`` / `timeout_seconds` / unexplained, unchanged from
        before this fix.
        """
        items: list[ThreadItem] = []
        usage: ThreadTokenUsage | None = None
        completed: TurnCompletedNotification | None = None

        # `AsyncTurnHandle.stream()` is annotated `-> AsyncIterator[Notification]`,
        # but its body is an `async def ... yield ...` function, so calling it
        # always produces a real async generator — `AsyncIterator` just doesn't
        # declare the `aclose()` every async generator actually has. A vendor
        # stub gap, not a guess: cast to what it actually is rather than
        # suppress the check.
        stream = cast(AsyncGenerator[Notification, None], handle.stream())
        try:
            async for notification in stream:
                payload = notification.payload
                if isinstance(payload, ItemCompletedNotification) and payload.turn_id == handle.id:
                    items.append(payload.item)
                elif isinstance(payload, ThreadTokenUsageUpdatedNotification) and payload.turn_id == handle.id:
                    usage = payload.token_usage
                elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == handle.id:
                    completed = payload

                if sink is not None:
                    event = self._event_for_notification(
                        notification, chat_id=chat_id, turn_id=turn_id, metadata=ctx_metadata
                    )
                    if event is not None:
                        try:
                            await sink.emit(event)
                        except Exception:
                            logger.debug("Codex event sink emit error", exc_info=True)

                if interrupt is not None and completed is None:
                    # Not polled once the terminal turn/completed has landed:
                    # the callback drains destructively (AgentActor._make_interrupt
                    # pops the pending message), and a steer against a finished
                    # turn is rejected by the vendor — so polling here would
                    # consume the user's message and throw it away.
                    #
                    # The poll itself is not wrapped in try/except: a raise
                    # (AbortTurn or anything else) is meant to propagate, per
                    # this method's own docstring — only the steer RPC below
                    # is best-effort, and even that is logged, not silent.
                    steer_message = await _apply_async(interrupt, {})
                    if steer_message:
                        steer_input = _content_to_codex_input(steer_message.get("content", ""))
                        try:
                            await handle.steer(steer_input)
                        except Exception:
                            # Best-effort, but never silent: this message came
                            # from a user and is now lost.
                            logger.warning(
                                "%s runtime %r: steering turn %r on chat %r failed; "
                                "the mid-turn message was dropped",
                                self._config.runtime,
                                self._kind,
                                turn_id,
                                chat_id,
                                exc_info=True,
                            )
        finally:
            await stream.aclose()

        if completed is None:
            raise RuntimeError("turn completed event not received")
        turn = completed.turn
        _raise_for_failed_turn(turn)

        return TurnResult(
            id=turn.id,
            status=turn.status,
            error=turn.error,
            started_at=turn.started_at,
            completed_at=turn.completed_at,
            duration_ms=turn.duration_ms,
            final_response=_final_assistant_response_from_items(items),
            items=items,
            usage=usage,
        )

    async def _run_turn(
        self,
        handle: AsyncTurnHandle,
        sink: TurnEventSink | None,
        *,
        chat_id: str,
        turn_id: str,
        ctx_metadata: dict[str, Any] | None,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None,
    ) -> tuple[TurnResult, bool]:
        """Consume one native turn, racing it against a cooperative stop and
        ``timeout_seconds`` (BEP 19 §3.10.2) on top of ``_emit_stream``'s own
        per-notification interrupt-callback poll (which, since fix round 1,
        steers rather than stops — see ``_emit_stream``'s docstring; it never
        causes the branch below to be taken).

        Returns ``(result, interrupted_by_host)``. ``interrupted_by_host`` is
        True only when ``self._stop_requested`` is why the turn ended early —
        as opposed to a `timeout_seconds` expiry (raised, never returned) or a
        vendor-reported `interrupted` this method never asked for. ``run()``
        uses it to decide between keeping a partial answer and raising, since
        the vendor's own `TurnStatus.interrupted` cannot tell those apart by
        itself.

        Registers the streaming task in ``self._in_flight[chat_id]`` for the
        busy guard and for ``aclose()`` to find and wait on.

        Whichever way the turn ends early, the native side is told: a stop or
        a timeout through :meth:`_settle_interrupted`, and a stream task that
        ended in an *exception* — an ``AbortTurn`` or anything else the
        interrupt callback raised — through the bounded, best-effort
        interrupt in the ``done()`` branch below (fix round 3, D). The three
        are mutually exclusive, so no turn is interrupted twice.
        """
        stream_task: asyncio.Task[TurnResult] = asyncio.ensure_future(
            self._emit_stream(
                handle, sink, chat_id=chat_id, turn_id=turn_id, ctx_metadata=ctx_metadata, interrupt=interrupt
            )
        )
        self._in_flight[chat_id] = stream_task
        stop_task = asyncio.ensure_future(self._stop_requested.wait())
        try:
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    await asyncio.wait({stream_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            except TimeoutError as exc:
                # Discard whatever _settle_interrupted comes back with: a
                # timeout raises regardless (BEP 19 §3.10.2), so there is
                # nothing to gain from waiting for a clean TurnResult — only
                # from asking the native turn to stop before this returns.
                with contextlib.suppress(Exception):
                    await self._settle_interrupted(handle, stream_task)
                raise TimeoutError(
                    f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                    f"{chat_id!r} exceeded timeout_seconds={self._config.timeout_seconds!r} and was "
                    "interrupted"
                ) from exc
        finally:
            stop_task.cancel()

        if stream_task.done():
            # Either it finished on its own, or it raced stop_task to the
            # finish line and won — either way, nothing was abandoned. (A
            # raise from the callback — see _emit_stream's docstring —
            # surfaces here too: stream_task.result() re-raises it.)
            try:
                return stream_task.result(), False
            except Exception:
                # BOS is giving up on this turn — an AbortTurn from the
                # interrupt callback, or anything else it raised (deliberately
                # unwrapped, see _emit_stream). The native turn does not know
                # that and keeps running against cwd, which BEP 19 §3.10 says
                # must not happen. Best-effort and bounded like every other
                # teardown interrupt here; harmless when the turn has already
                # ended (a vendor `failed`), since the interrupt is suppressed
                # either way.
                #
                # `except Exception`, not BaseException: a cancelled
                # stream_task raises CancelledError, and awaiting inside a
                # cancellation unwind is its own hazard. Only
                # _settle_interrupted cancels this task, and neither of its
                # two callers routes through this branch, so that case cannot
                # arrive here — and this is not a double interrupt for the
                # same reason.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.interrupt(), _INTERRUPT_GRACE_SECONDS)
                raise

        # self._stop_requested fired before the stream finished on its own:
        # a cooperative stop landed mid-turn.
        result = await self._settle_interrupted(handle, stream_task)
        return result, True

    async def _settle_interrupted(
        self, handle: AsyncTurnHandle, stream_task: asyncio.Task[TurnResult]
    ) -> TurnResult:
        """Ask the vendor to stop, then give the stream a bounded window to
        drain into the ``TurnResult`` that produces — rather than waiting it
        out (BEP 19 §3.10.2, the brief's phrasing for `request_stop()`, applied
        equally to a timeout's own best-effort interrupt). Shared by both
        callers in :meth:`_run_turn` so "a native turn that ignores the
        interrupt" is one code path, not two that could drift.

        Raises if the native side never confirms: there is no ``TurnResult``
        to hand back in that case, and manufacturing one would misreport what
        happened. Worst case is three times ``_INTERRUPT_GRACE_SECONDS`` — the
        interrupt RPC, the drain wait, and the post-cancel wait — and on that
        path the task is **abandoned**, not killed: it goes on running, owned
        by the loop rather than by this turn (the same doctrine as
        ``Agent._abandon``). :meth:`aclose` bounds its own wait because of
        that. The vendor's own stream is not what survives the cancel — driven
        directly it dies at once — but the two *host-supplied* awaits inside
        :meth:`_emit_stream`'s loop, ``sink.emit`` and the ``interrupt``
        callback, are arbitrary caller code that can shield, block or swallow
        one (fix round 3, N1).
        """
        with contextlib.suppress(Exception):
            # Bounded, not merely guarded: the vendor's interrupt is an RPC
            # over a blocking queue (asyncio.to_thread) that only wakes when
            # the child answers or dies, and suppress() catches errors, not
            # slowness. wait_for's TimeoutError is an Exception, so a wedged
            # child falls through to the cancel path below like any other
            # failure to confirm.
            await asyncio.wait_for(handle.interrupt(), _INTERRUPT_GRACE_SECONDS)
        done, _ = await asyncio.wait({stream_task}, timeout=_INTERRUPT_GRACE_SECONDS)
        if stream_task in done:
            return stream_task.result()
        stream_task.cancel()
        await asyncio.wait({stream_task}, timeout=_INTERRUPT_GRACE_SECONDS)
        # Not independently mutation-tested: this only fires in the narrow
        # window where stream_task finishes on its own — with a result or an
        # exception — in the instant between the cancel() above landing and
        # this check running, so cancel() did not "win". It exists purely so
        # asyncio's default handler does not log an "exception was never
        # retrieved" warning for that straggler; it changes no observable
        # behavior (the RuntimeError below is raised regardless). Mirrors
        # Agent._abandon's identical line (agent.py:358-360) verbatim.
        if stream_task.done() and not stream_task.cancelled():
            stream_task.exception()  # consumed
        raise RuntimeError(
            f"{self._config.runtime} runtime {self._kind!r}: turn {handle.id!r} did not respond to "
            f"interrupt within {_INTERRUPT_GRACE_SECONDS}s"
        )

    async def ask(
        self,
        chat_id: str,
        content: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: TurnEventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
    ) -> str:
        """Thin wrapper over :meth:`run`, mirroring ``Agent.ask`` (BOS's own agent)."""
        result = await self.run(
            chat_id,
            content,
            interrupt=interrupt,
            ctx_metadata=ctx_metadata,
            llm_args=llm_args,
            event_sink=event_sink,
            turn_id=turn_id,
            commit_observer=commit_observer,
        )
        return str(result.output)

    async def run(
        self,
        chat_id: str,
        content: MessageContent,
        *,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: TurnEventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_schema_retries: int = 1,
    ) -> AgentResult:
        """Run one Codex turn to completion, streaming it, and persist it
        (BEP 19 §3.7, §3.9).

        Every native turn — including each schema-validation retry — is
        started with ``thread.turn()`` and consumed through
        :meth:`_run_turn`/:meth:`_emit_stream`, which stream ``handle.stream()``
        into ``TurnEvent``s for ``event_sink`` while racing the turn against a
        cooperative stop and ``timeout_seconds`` and polling the ``interrupt``
        callback, accumulating the same ``TurnResult`` a plain
        ``await thread.run(...)`` would have produced. Still deliberately
        plain otherwise (stage 4 of 4, see the module docstring): no approval
        handling yet (Task 8).

        ``schema`` maps to ``thread.turn(output_schema=...)`` as a provider
        hint, but that hint is never trusted on its own: the reply is always
        checked locally with ``self._structured_validator`` (the same object
        ``create_agent`` injects into every ``Agent``, so BEP 12 semantics are
        identical regardless of which kind of agent ran the turn — not a
        second, divergent validation path). A validation failure re-sends a
        plain-text correction message on the *same* thread, up to
        ``max_schema_retries`` times; exhausting retries raises
        ``StructuredOutputError`` and — like a native turn failure — commits
        nothing, so a half-validated exchange never looks like turn history
        the next resume can reason from.

        Ending early has three distinct causes that all surface from the
        vendor as the same ``TurnStatus.interrupted``, so this method — not
        the bare status — is what tells them apart, deciding as each happens
        rather than guessing afterwards from the result alone (BEP 19 §3.10.2,
        the decision Task 5 left open):

        - **`timeout_seconds` expiry** is a deadline the *caller* asked for.
          :meth:`_run_turn` interrupts the native turn and raises a fresh
          ``TimeoutError`` (BEP 19 §7 criterion 20: "... raises"). Nothing is
          committed: an answer cut off by the caller's own deadline is a
          failure, not turn history. The same key also bounds the setup RPCs
          that run *before* there is a turn to interrupt
          (:meth:`_bounded_setup`, fix round 4); that flavour names its phase
          in the message, has nothing to interrupt, and is equally uncommitted.
        - **A cooperative stop** — ``request_stop()`` racing the turn — is
          BOS taking the turn away, not the model or the caller failing.
          ``Agent``'s own contract for the same situation
          (``src/bos/core/agent/agent.py:648-657``, ``:831-836``) is to keep
          what the turn established rather than raise and discard it —
          persisting a handoff and returning. This mirrors that: it returns
          normally, persists whatever ``final_response`` the turn had
          produced, and reports ``finish_reason="interrupted"`` so the caller
          can tell. It stops short of ``Agent``'s handoff *summary* — nothing
          hands this runtime a ``Consolidator``, and BEP 19 defines none of
          its own for it — so the kept content is the turn's own last answer,
          not a paraphrase of what happened; Task 7's brief asks for exactly
          this much and no more.
        - **An unexplained `interrupted`** — the vendor reports it without
          this call itself ever having asked for it — keeps Task 5/6's
          original behaviour: raise :class:`TurnNotCompletedError` and commit
          nothing. Nothing today produces this (the native session is private
          to this one ``CodexAgent``), but a status this method did not ask
          for is not one it should silently reinterpret as a stop.

        The ``interrupt`` callback (BEP 19 §3.9's `interrupt` row) is not a
        fourth cause: since fix round 1, ``_emit_stream`` steers a truthy
        return into the running turn instead of ending it (see its own
        docstring), so it never produces ``interrupted``. It can still end a
        turn a different way — a raised ``AbortTurn`` — and that is
        **caught here and returned, not propagated** (fix round 2, I4), after
        :meth:`_run_turn` has told the native side to stop (fix round 3, D).
        ``Agent`` does the same with the identical signal
        (``agent.py:837-840``): it sets ``turn_status = "aborted"``, puts
        ``ABORTED_TURN_CONTENT`` in ``ctx.final_content``, and returns a
        normal ``AgentResult``. Propagating instead would make ``AgentActor``
        report ``status="error"`` and send ``"Turn failed: "`` with no text,
        where a BOS agent sends the marker as an ordinary reply — so this
        returns ``ABORTED_TURN_CONTENT`` with ``finish_reason="aborted"``.
        It commits **nothing**, and that is not an inconsistency with
        ``Agent``: ``Agent`` persists the marker to shape the *model's* own
        history so the next turn does not re-answer a dead request, and
        ``CodexAgent`` never replays BOS history into Codex — the native
        thread is the model's history — so that purpose does not transfer.

        Two concurrent turns on the same ``chat_id`` are rejected with a busy
        ``RuntimeError`` rather than queued — the native session is
        single-threaded (BEP 19 §3.10.1) — tracked via ``self._in_flight``,
        which also gives :meth:`aclose` something to interrupt and wait on.
        """
        turn_id = turn_id or uuid.uuid4().hex
        if chat_id in self._in_flight:
            raise RuntimeError(
                f"Agent {self._kind!r} already has a turn running on chat {chat_id!r}. "
                f"A Codex thread is single-threaded; wait for the turn to finish."
            )
        self._in_flight[chat_id] = None  # reserved synchronously — no await before this line
        try:
            thread, _ = await self._thread_for(chat_id, turn_id=turn_id)
            turn_kwargs = _compact(
                model=(llm_args or {}).get("model"),
                effort=(llm_args or {}).get("reasoning_effort"),
                output_schema=schema,
            )

            codex_input = _content_to_codex_input(content)
            structured_output: Any = None
            structured_ok = False
            retries = 0
            while True:
                try:
                    handle = await self._bounded_setup(
                        thread.turn(codex_input, **turn_kwargs),
                        phase="the turn request (thread.turn)",
                        chat_id=chat_id,
                        turn_id=turn_id,
                    )
                    result, interrupted_by_host = await self._run_turn(
                        handle,
                        event_sink,
                        chat_id=chat_id,
                        turn_id=turn_id,
                        ctx_metadata=ctx_metadata,
                        interrupt=interrupt,
                    )
                except TimeoutError:
                    # Both flavours pass straight through, unwrapped: the
                    # streaming deadline from _run_turn, and the setup
                    # deadline from _bounded_setup above. Each already carries
                    # its own runtime/kind/turn/chat message, and re-wrapping
                    # either as a generic turn failure would bury which one
                    # fired.
                    raise
                except AbortTurn:
                    # Agent CATCHES AbortTurn and returns (agent.py:837-840);
                    # propagating would make AgentActor report status="error"
                    # and send "Turn failed: " with no text, where a BOS agent
                    # sends the marker as an ordinary reply. Commits nothing:
                    # ABORTED_TURN_CONTENT shapes the MODEL's history, and
                    # CodexAgent never replays BOS history into Codex, so the
                    # marker is the caller's answer here, not stored context.
                    return external_agent_result(
                        output=ABORTED_TURN_CONTENT,
                        turn_id=turn_id,
                        usage=None,
                        finish_reason="aborted",
                    )
                except Exception as exc:
                    # A native TurnStatus.failed is raised before a TurnResult is
                    # ever built, on both the plain and the streaming path:
                    # openai_codex._run's own _raise_for_failed_turn runs inside
                    # _collect_async_turn_result ahead of the `TurnResult(...)`
                    # call, and `_emit_stream` mirrors that exact check (this
                    # module's own `_raise_for_failed_turn`) ahead of its own
                    # `TurnResult(...)` call — so neither `thread.turn()` nor
                    # `_emit_stream` ever hands back a `TurnResult` for a failed
                    # turn; this method's own status check below is unreachable
                    # for `failed`. Re-wrapped here so the runtime/agent/turn_id/
                    # chat_id context this method would attach to a
                    # `TurnResult`-shaped failure is not lost on the one path a
                    # real vendor call actually takes. Applies on a retry turn
                    # too: a native failure while sending a correction message is
                    # a new native failure, not one more validation attempt to
                    # retry.
                    raise RuntimeError(
                        f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                        f"{chat_id!r} failed: {exc}"
                    ) from exc

                if result.status is not TurnStatus.completed:
                    if not (interrupted_by_host and result.status is TurnStatus.interrupted):
                        # The only status left that reaches here as a normal
                        # TurnResult is `interrupted` (see TurnNotCompletedError
                        # and this method's own docstring); `failed` is handled
                        # above, and is never returned as a TurnResult by the
                        # real SDK to begin with. Commits nothing, same
                        # reasoning as the raise above: neither is a real
                        # answer BOS should treat as turn history.
                        raise TurnNotCompletedError(
                            f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                            f"{chat_id!r} ended with status {result.status.value!r}",
                            turn_result=result,
                        )
                    # BOS itself ended this turn early (see the docstring's
                    # "cooperative stop" case) — keep what it produced instead
                    # of raising. Never schema-checked below: the turn never
                    # finished normally, so a validation failure here would
                    # only replace one negative outcome with another that says
                    # nothing truer about what the model actually produced.
                    text = result.final_response or ""
                    break

                text = result.final_response or ""
                if schema is None:
                    break
                try:
                    structured_output = self._structured_validator.validate(text, schema)
                    structured_ok = True
                    break
                except StructuredOutputError as e:
                    if retries >= max_schema_retries:
                        # Exhausted: commits nothing, same as a native failure
                        # above — an unvalidated reply is not the answer `schema=`
                        # promised, so it is not turn history either.
                        raise
                    retries += 1
                    codex_input = (
                        f"Your previous response failed schema validation: {e}. "
                        "Reply ONLY with JSON matching the schema."
                    )

            output = structured_output if structured_ok else text
            usage = result.usage.last.model_dump() if result.usage is not None else None

            if self._chat_store is not None:
                commit = await commit_external_turn(
                    self._chat_store,
                    chat_id,
                    turn_id=turn_id,
                    user_content=content,
                    response=text,
                    runtime=self._config.runtime,
                    native_session_id=thread.id,
                    native_turn_id=result.id,
                    usage=usage,
                )
                if commit_observer is not None:
                    observed = commit_observer(commit)
                    if inspect.isawaitable(observed):
                        await observed

            return external_agent_result(
                output=output,
                structured=structured_ok,
                turn_id=turn_id,
                usage=usage,
                finish_reason=result.status.value,
            )
        finally:
            self._in_flight.pop(chat_id, None)

    async def aclose(self) -> None:
        """Interrupt every in-flight turn, then close the client regardless of
        whether they wound down in time (BEP 19 §3.10.2).

        Setting ``_stop_requested`` is enough to make each in-flight
        :meth:`run` call interrupt its own turn and race it, exactly as
        ``request_stop()`` does; this only adds waiting for them and the
        client close. That wait is bounded by ``_ACLOSE_GRACE_SECONDS``
        because :meth:`_settle_interrupted`'s own inner bound does **not**
        guarantee the task is finished — it *abandons* a task it cannot kill,
        which then outlives the turn that owned it. A turn still winding down
        after the bound is reported and left behind; the client is closed
        anyway, since that is the only thing that reaps the child process.
        So this is a bounded drain, not a clean one: a wedged turn is given a
        window, not waited out.

        Also closes Task 4's hole: an ``aclose()`` racing an ``_ensure_client()``
        still in flight no longer leaks a client. Both now take
        ``_client_lock``, so this either finds no client built yet (nothing to
        close), or waits for the one being built and closes that.
        """
        self._stop_requested.set()
        tasks = [task for task in self._in_flight.values() if task is not None]
        if tasks:
            # Bounded because _settle_interrupted ABANDONS a task it cannot
            # kill (it cancels, waits out _INTERRUPT_GRACE_SECONDS, then
            # raises) — so a task can outlive the turn that owned it, and an
            # unbounded wait here would never reach the client.close() below,
            # which is the only thing that reaps the child process.
            _, pending = await asyncio.wait(tasks, timeout=_ACLOSE_GRACE_SECONDS)
            if pending:
                logger.warning(
                    "%s runtime %r: %d turn(s) still running after %ss; closing the client anyway",
                    self._config.runtime,
                    self._kind,
                    len(pending),
                    _ACLOSE_GRACE_SECONDS,
                )
        async with self._client_lock:
            if self._client is not None:
                await self._client.close()
