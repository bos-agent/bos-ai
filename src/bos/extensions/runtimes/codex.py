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
injected ``StructuredValidator`` — BEP 19 §3.2, §3.9) (Task 5), and here —
that turn is made observable: ``run()`` starts each native turn with
``thread.turn()`` and consumes it through ``_emit_stream``, which streams
``AsyncTurnHandle.stream()`` into BOS ``TurnEvent``s as they arrive rather
than awaiting one final result (BEP 19 §3.9). Tasks 7-8 add interrupt/
cooperative-stop/timeout and the approval handler on top of this.
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
from typing import Any, Awaitable, cast

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    ImageInput,
    InputItem,
    LocalImageInput,
    MentionInput,
    RunInput,
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
    AgentEventType,
    AgentResult,
    ChatStore,
    MessageContent,
    StructuredOutputError,
    StructuredValidator,
    TurnEvent,
    TurnEventPhase,
    TurnEventSink,
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


def _content_to_codex_input(content: MessageContent) -> RunInput:
    """BOS ``MessageContent`` -> a Codex ``RunInput`` (BEP 19 §3.9).

    A plain string is already a valid ``RunInput`` (the SDK wraps it in a
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
    ``run()``), so it never reaches this exception. Carries ``turn_result``
    so a caller has more than a message to work with: Task 7 needs the full
    result to decide between raising and BOS's own cooperative-stop handoff
    shape, not just the fact that something other than ``completed`` happened.
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
            response = await client.account()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.close()
            raise RuntimeError(
                f'{runtime} runtime {self._kind!r}: auth="subscription" but the account check '
                f"failed: {exc}"
            ) from exc
        if response.account is None:
            with contextlib.suppress(Exception):
                await client.close()
            raise RuntimeError(
                f'{runtime} runtime {self._kind!r}: auth="subscription" but no Codex account is '
                f'logged in. Run `codex login`, or set auth="api_key" to opt out of this check.'
            )

    async def _thread_for(self, chat_id: str) -> tuple[AsyncThread, bool]:
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
            thread = await client.thread_start(**thread_kwargs)
            return thread, True
        try:
            thread = await client.thread_resume(native_session_id, **thread_kwargs)
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
        :meth:`_emit_stream`, which streams ``handle.stream()`` into
        ``TurnEvent``s for ``event_sink`` while accumulating the same
        ``TurnResult`` a plain ``await thread.run(...)`` would have produced.
        Still deliberately plain otherwise (stage 4 of 4, see the module
        docstring): no ``interrupt``/timeout wiring (Task 7), no approval
        handling (Task 8).

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

        A ``TurnStatus.interrupted`` result raises :class:`TurnNotCompletedError`
        (carrying the ``TurnResult``) and commits nothing — right for today's
        only source of it, `timeout_seconds` expiry (BEP 19 §7 criterion 20:
        "... raises"). But Task 7 also makes `request_stop()` produce
        `interrupted`, and BOS's own cooperative-stop contract for that case is
        the opposite: `Agent.run` treats a stop as `turn_status="completed"`,
        persists a handoff, and returns (`agent.py` `_StopRequested` handling
        and `_close_with_handoff`) — it does not raise and discard the partial
        answer. Task 7 must reopen this branch and choose, for a
        cooperatively-stopped turn, between raising (as here) and building the
        equivalent handoff-and-return shape; ``turn_result`` is attached
        precisely so that decision has the real result to work with instead of
        a bare message.
        """
        turn_id = turn_id or uuid.uuid4().hex
        thread, _ = await self._thread_for(chat_id)
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
                handle = await thread.turn(codex_input, **turn_kwargs)
                result = await self._emit_stream(
                    handle, event_sink, chat_id=chat_id, turn_id=turn_id, ctx_metadata=ctx_metadata
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
                # The only status left that reaches here as a normal
                # TurnResult is `interrupted` (see TurnNotCompletedError and
                # this method's own docstring); `failed` is handled above,
                # and is never returned as a TurnResult by the real SDK to
                # begin with. Commits nothing, same reasoning as the raise
                # above: neither is a real answer BOS should treat as turn
                # history.
                raise TurnNotCompletedError(
                    f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                    f"{chat_id!r} ended with status {result.status.value!r}",
                    turn_result=result,
                )

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
                    f"Your previous response failed schema validation: {e}. Reply ONLY with JSON matching the schema."
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
            output=output, structured=structured_ok, turn_id=turn_id, usage=usage, finish_reason=result.status.value
        )

    async def aclose(self) -> None:
        # Task 7 adds interrupting an in-flight turn before this closes the
        # client — nothing here races a live run() yet. Closing the client, if
        # one was ever built, is the whole of it today.
        if self._client is not None:
            await self._client.close()
