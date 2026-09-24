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

Stage 3 of 4 over ``CodexAgent``'s turn path: construction (Task 3), the
client and thread lifecycle (Task 4), and here — a turn actually runs and is
persisted, including schema-validated structured output (BEP 12 semantics,
via the injected ``StructuredValidator`` — BEP 19 §3.2, §3.9). Tasks 6-8 add
event streaming, interrupt/timeout and the approval handler on top of this;
``run()`` is deliberately plain until then (no streaming, no interrupt
wiring).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
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
from openai_codex.types import TurnStatus

from bos.core.agent import (
    AgentResult,
    ChatStore,
    MessageContent,
    StructuredOutputError,
    StructuredValidator,
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
        """Run one Codex turn to completion and persist it (BEP 19 §3.7, §3.9).

        Deliberately plain otherwise (stage 3 of 4, see the module docstring):
        no event streaming (``event_sink`` is accepted but not yet fed — Task
        6), no ``interrupt``/timeout wiring (Task 7), no approval handling
        (Task 8).

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
                result = await thread.run(codex_input, **turn_kwargs)
            except Exception as exc:
                # A native TurnStatus.failed is raised by the SDK itself,
                # before it ever constructs a TurnResult: openai_codex._run's
                # _raise_for_failed_turn runs inside _collect_async_turn_result
                # ahead of the `TurnResult(...)` call, so `await thread.run(...)`
                # never returns one for a failed turn — this method's own
                # status check below is unreachable for `failed` against the
                # real SDK. Re-wrapped here so the runtime/agent/turn_id/chat_id
                # context this method would attach to a `TurnResult`-shaped
                # failure is not lost on the one path a real vendor call
                # actually takes. Applies on a retry turn too: a native
                # failure while sending a correction message is a new native
                # failure, not one more validation attempt to retry.
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
