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

Stage 2 of 4 over ``CodexAgent``'s turn path: construction (Task 3) plus the
client and thread lifecycle built here — lazily starting one ``AsyncCodex``
and mapping a BOS ``chat_id`` onto a Codex thread. Task 5 makes a turn
actually run, Tasks 6-8 add streaming, interrupt/timeout and the approval
handler. ``ask()``/``run()`` raise ``NotImplementedError`` below for that
reason — it is a stage boundary, not a gap.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox

from bos.core.agent import AgentResult, ChatStore, MessageContent, TurnEventSink
from bos.extensions.runtimes._shared import parse_external_config, read_native_session_id

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
    ) -> None:
        self._kind = kind
        self._chat_store = chat_store
        self._mcp = mcp
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
        # Task 5 makes a turn actually run (BEP 19 Layer 4a, stage 3 of 4).
        raise NotImplementedError("CodexAgent.ask lands in Task 5")

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
        # Task 5 makes a turn actually run (BEP 19 Layer 4a, stage 3 of 4).
        raise NotImplementedError("CodexAgent.run lands in Task 5")

    async def aclose(self) -> None:
        # Task 7 adds interrupting an in-flight turn before this. There is
        # never one yet — ask()/run() are not implemented until Task 5 — so
        # closing the client, if one was ever built, is the whole of it today.
        if self._client is not None:
            await self._client.close()
