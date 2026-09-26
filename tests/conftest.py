"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fake_anthropic import FakeAnthropic

from bos.core.agent import Agent, AgentResult, TurnContext
from bos.core.contract import Message, TurnInterceptor, ep_consolidator, ep_tool
from bos.core.harness import ChainInterceptor, ResolvedToolSet, _CompositePluginInterceptor, _PluginPromptProvider
from bos.core.llm import LLMClient
from bos.core.registry import ToolRegistry
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute  # noqa: F401
from bos.extensions.memory_stores.in_memory import InMemMemoryExtension  # noqa: F401


class BlockImport:
    """Meta-path finder that makes *name* unimportable.

    Simulates an install where an optional dependency's extra was never
    installed, without uninstalling anything. Insert at the head of
    ``sys.meta_path`` via monkeypatch and drop *name* from ``sys.modules``.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name or fullname.startswith(f"{self.name}."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def resolve_test_tools(
    *,
    plugins: list[Any] | None = None,
    local_tools: ToolRegistry | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[ToolRegistry, ResolvedToolSet]:
    """Mirror the harness tool resolution for direct-construction tests:
    register plugin tools into a local registry and expose a filtered view
    over [local, global]. Returns (local_registry, resolved) so tests can
    still inspect/extend the local registry."""
    local = local_tools or ToolRegistry("_local_tools:test", "Agent-scoped local tools.")
    for plugin in plugins or []:
        plugin.register_tools(local)
    return local, ResolvedToolSet([local, ep_tool], include=include, exclude=exclude)


def dummy_turn_context() -> TurnContext:
    """A throwaway TurnContext for introspection-style calls — e.g. building the
    system prompt outside a real turn."""
    return TurnContext(agent_name="test", chat_id="test", turn_id="test")


def compose_test_interceptors(
    plugins: list[Any] | None = None, fallback: TurnInterceptor | None = None
) -> _CompositePluginInterceptor:
    """Mirror the harness interceptor assembly for direct-construction tests:
    plugin interceptors (best-effort) ahead of a fallback chain."""
    plugin_interceptors = [i for plugin in plugins or [] for i in plugin.get_interceptors()]
    return _CompositePluginInterceptor(plugin_interceptors, fallback or ChainInterceptor())


def create_test_agent(
    *,
    plugins: list[Any] | None = None,
    local_tools: ToolRegistry | None = None,
    tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
    interceptor: TurnInterceptor | None = None,
    **kwargs: Any,
) -> Agent:
    plugins = plugins or []
    _, resolved = resolve_test_tools(plugins=plugins, local_tools=local_tools, include=tools, exclude=exclude_tools)
    kwargs.setdefault("kind", "test")
    kwargs.setdefault("agent_name", "test")
    kwargs.setdefault("chat_store", InMemChatStore())
    kwargs.setdefault("consolidator", MessageOnlyConsolidator())
    kwargs.setdefault("llm", LLMClient())
    return Agent(
        tools=resolved,
        interceptor=compose_test_interceptors(plugins, interceptor),
        prompt_provider=_PluginPromptProvider(plugins),
        **kwargs,
    )


class RecordingConsolidator:
    """Message-based consolidator double for tests that do not exercise summarization."""

    def __init__(self, summary: str = "recorded summary") -> None:
        self.summary = summary
        self.calls: list[tuple[list[Message], str | None]] = []

    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str:
        self.calls.append((messages, instruction))
        return self.summary


class MessageOnlyConsolidator(RecordingConsolidator):
    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str:
        assert all(isinstance(message, Message) for message in messages)
        return await super().consolidate(messages, instruction)


class CloseTrackingConsolidator(RecordingConsolidator):
    def __init__(self, summary: str = "recorded summary") -> None:
        super().__init__(summary)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@ep_consolidator(name="LLMConsolidator")
def _default_test_consolidator(model=None, llm=None, **kwargs):
    """Default test consolidator factory — returns a MessageOnlyConsolidator."""
    return MessageOnlyConsolidator()


class _FakeRuntime:
    """Stands in for ClaudeCodeAgent / CodexAgent (BEP 19 §6 Layer 1)."""

    def __init__(self, *, kind, cfg, chat_store, workspace, mcp, structured_validator):
        self._kind, self.cfg, self.workspace, self.mcp = kind, cfg, workspace, mcp
        self.structured_validator = structured_validator
        self.closed = False

    @property
    def name(self) -> str:
        return self._kind

    @property
    def resolved_config(self):
        return self.cfg

    def request_stop(self) -> None:
        pass

    async def ask(self, chat_id, content, **kwargs) -> str:
        return "fake"

    async def run(self, chat_id, content, **kwargs) -> AgentResult:
        return AgentResult(output="fake")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_runtimes(monkeypatch):
    """Both reserved kinds resolve to _FakeRuntime, without any vendor SDK."""
    from bos.core import harness as harness_mod

    monkeypatch.setattr(harness_mod, "_load_external_runtime", lambda runtime: _FakeRuntime)


@contextlib.asynccontextmanager
async def serve_asgi(app):
    """Run *app* on uvicorn at an ephemeral port; yield ``"host:port"``.

    Websocket tests need a real server: pre-accept rejections go out through the
    ASGI websocket denial-response extension, which uvicorn implements and an
    in-process transport does not (BEP 17 §3.6.4).

    ``Server.serve()`` is deliberately not used — it installs SIGINT/SIGTERM
    handlers on the main thread, which under pytest is pytest's own.
    """
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    config.load()
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    serving = asyncio.ensure_future(server.main_loop())
    try:
        yield f"127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)
        await server.shutdown()


# ── Codex runtime double (BEP 19 Layer 4a) ──────────────────────────────────
#
# Fakes the *transport* (AsyncCodex/AsyncThread/AsyncTurnHandle), never the
# vendor's data shapes: every object CodexAgent reads out of a call is a real
# openai_codex type, so a shape the vendor changes breaks these tests instead
# of a hand-rolled stand-in silently drifting from it.


class _Hang:
    """Task 7: a sentinel placed in a FakeTurnHandle's armed notifications.

    ``stream()`` pauses on it instead of yielding, simulating a turn still
    being worked on by the vendor — nothing to test interrupt/stop/timeout/
    busy/aclose against without one, since the existing notification list is
    otherwise exhausted (and the fake done) faster than any of those can race
    it. ``release`` — set directly by a test, standing in for the vendor
    eventually confirming an interrupt — is what unblocks it; production
    code's own ``handle.interrupt()`` call is recorded (``interrupted``) but
    deliberately does *not* auto-release, so a test can also model a native
    turn that ignores the interrupt entirely, which timeout and aclose() must
    both tolerate without hanging.
    """


HANG = _Hang()


class FakeTurnHandle:
    """Stands in for openai_codex.AsyncTurnHandle."""

    def __init__(self, thread: FakeThread, turn_id: str, notifications: list[Any], result: Any) -> None:
        self._thread, self.id = thread, turn_id
        self._notifications, self._result = notifications, result
        self.interrupted = False
        # Fix round 1: every input handed to steer(), in call order — a
        # truthy interrupt-callback return delivers into the running turn via
        # steer(), not interrupt(), so a test asserts on this instead.
        self.steered: list[Any] = []
        # See HANG. hang_reached lets a test `wait_for()` the pause reliably
        # instead of guessing how many event-loop turns run() needs to get
        # there; release is what ends it.
        self.hang_reached = asyncio.Event()
        self.release = asyncio.Event()
        # Fix round 2: the ways this double used to be *more reliable than
        # the vendor*, which is what let round 1 delete a real safety bound as
        # "dead code". A real interrupt()/steer() is an RPC to the app-server
        # that can be slow or fail, and a real turn is not guaranteed to
        # release the event loop when BOS cancels it. All default to the old,
        # always-fast, always-succeeds behaviour, so every pre-existing test
        # is unchanged; a test opts in per handle (see
        # FakeAsyncCodex.next_handle_attrs for arming one that does not exist
        # yet).
        self.interrupt_hang: asyncio.Event | None = None  # set -> interrupt() blocks on it
        self.interrupt_error: Exception | None = None  # set -> interrupt() raises it
        self.steer_error: Exception | None = None  # set -> steer() raises it (after recording)
        # True -> the turn ignores cancellation and keeps hanging. See the
        # note in stream() for which production await this actually stands in
        # for — it is NOT the vendor's own stream.
        self.swallow_cancel = False
        self.cancels_swallowed = 0

    async def stream(self):
        for notification in self._notifications:
            if notification is HANG:
                self.hang_reached.set()
                while True:
                    try:
                        await self.release.wait()
                        break
                    except asyncio.CancelledError:
                        # Fix round 3 (N1): the vendor's OWN stream does die
                        # on cancel — driven directly, the consuming task ends
                        # in 0.000s; only the `asyncio.to_thread` worker thread
                        # leaks. So this is not modelling the vendor.
                        #
                        # What it models is the other two awaits inside
                        # _emit_stream's loop: `await sink.emit(event)` and
                        # `await _apply_async(interrupt, {})`. Both are
                        # host-supplied code, arbitrary and able to shield,
                        # block or swallow a cancel — and a stream task parked
                        # in one of them is exactly the task _settle_interrupted
                        # ABANDONS, which is why aclose() needs a bound of its
                        # own. Simulated here at the stream level only because
                        # that is the cheapest seam in this double, not because
                        # the vendor stream behaves this way.
                        if not self.swallow_cancel:
                            raise
                        self.cancels_swallowed += 1
                continue
            yield notification

    async def interrupt(self) -> None:
        self.interrupted = True
        if self.interrupt_hang is not None:
            await self.interrupt_hang.wait()
        if self.interrupt_error is not None:
            raise self.interrupt_error

    async def steer(self, input: Any) -> None:
        self.steered.append(input)
        if self.steer_error is not None:
            raise self.steer_error


def _default_turn_notifications(thread_id: str, turn_id: str, result: Any) -> list[Any]:
    """Task 5's tests arm a ``TurnResult`` via ``_arm_result`` and inspect only
    the ``AgentResult`` ``run()`` returns — none of them pass an ``event_sink``.
    Since Task 6, ``CodexAgent.run()`` no longer takes that ``TurnResult``
    directly: it reconstructs one from the notifications ``handle.stream()``
    yields. Rather than rewrite every one of those pre-existing tests to
    hand-build a ``Notification`` sequence, synthesize the minimal one that
    reconstructs an equivalent ``TurnResult`` from *this* armed result, so the
    streaming path Task 6 introduces is what actually produces their answer.

    Only used when a test never explicitly arms ``next_notifications`` itself
    (see ``FakeThread.turn`` below) — a test exercising the mapping/ordering
    of events arms its own real ``Notification`` sequence instead, and this is
    never consulted.
    """
    from openai_codex.generated.v2_all import AgentMessageThreadItem, MessagePhase, ThreadItem, Turn
    from openai_codex.models import (
        ItemCompletedNotification,
        Notification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )

    if result is None:
        return []
    notifications: list[Any] = []
    if result.final_response:
        item = ThreadItem(
            AgentMessageThreadItem(
                id=f"{turn_id}-response",
                text=result.final_response,
                phase=MessagePhase.final_answer,
                type="agentMessage",
            )
        )
        notifications.append(
            Notification(
                method="item/completed",
                payload=ItemCompletedNotification(item=item, completed_at_ms=0, thread_id=thread_id, turn_id=turn_id),
            )
        )
    if result.usage is not None:
        notifications.append(
            Notification(
                method="thread/tokenUsage/updated",
                payload=ThreadTokenUsageUpdatedNotification(
                    thread_id=thread_id, token_usage=result.usage, turn_id=turn_id
                ),
            )
        )
    turn = Turn(
        id=turn_id,
        items=[],
        status=result.status,
        error=result.error,
        started_at=result.started_at,
        completed_at=result.completed_at,
        duration_ms=result.duration_ms,
    )
    notifications.append(
        Notification(method="turn/completed", payload=TurnCompletedNotification(thread_id=thread_id, turn=turn))
    )
    return notifications


class FakeThread:
    """Stands in for openai_codex.AsyncThread."""

    def __init__(self, codex: FakeAsyncCodex, thread_id: str) -> None:
        self._codex, self.id = codex, thread_id

    async def turn(self, input: Any, **kwargs: Any) -> FakeTurnHandle:
        self._codex.turn_calls.append((self.id, input, kwargs))
        if self._codex.turn_hang is not None:
            await self._codex.turn_hang.wait()
        # A queued result (Task 5 fix round: schema-retry tests need a
        # *different* TurnResult per call within one run() invocation) takes
        # priority; next_result is the pre-existing single persistent slot,
        # unchanged for every caller that never touches next_results.
        result = self._codex.next_results.pop(0) if self._codex.next_results else self._codex.next_result
        # The real AsyncTurnHandle.id IS the native turn id (both come from
        # the same TurnStartResponse.turn.id) — TurnResult.id can never differ
        # from the handle that produced it. Mirror that: when a result is
        # armed, the handle's id (and the turn id notifications carry) is
        # *its* id, not an independent counter; only fall back to a counter
        # when nothing was armed at all.
        turn_id = result.id if result is not None else f"turn-{len(self._codex.turn_calls)}"
        notifications = self._codex.next_notifications or _default_turn_notifications(self.id, turn_id, result)
        handle = FakeTurnHandle(self, turn_id, notifications, result)
        # Fix round 2: the handle is built here, inside production code's own
        # `await thread.turn(...)`, so a test that needs a slow or failing RPC
        # from the very first notification has no seam to reach it afterwards
        # — same staging problem (and same solution) as next_notifications.
        for name, value in self._codex.next_handle_attrs.items():
            setattr(handle, name, value)
        # Task 7: every handle ever created, in creation order, so a test can
        # reach into an in-flight turn (e.g. via HANG above) without CodexAgent
        # itself ever handing the handle back.
        self._codex.turn_handles.append(handle)
        return handle

    # Deliberately no `run()` here, and none on FakeTurnHandle either. The
    # vendor has both, but production has driven `handle.stream()` since Task 6
    # and nothing calls them — a faithful stand-in for a surface nothing uses is
    # a second definition of correctness, free to drift where no test looks.
    # (The failed-turn raise they used to mirror is production's own
    # `_raise_for_failed_turn`, reached through `_emit_stream`.)
    #
    # Task 9 deliberately has no `read()` here either. `CodexAgent.native_messages`
    # does not go through a thread object the client handed it — it builds a
    # REAL `openai_codex.AsyncThread` over the client (BEP 19 §3.7; resuming
    # would be a write on a read), so the vendor's own `AsyncThread.read()`
    # runs and lands on `FakeAsyncCodex._client.thread_read` below. A `read()`
    # on this class would be dead code that looks like the seam under test.


class FakeAsyncCodex:
    """Fakes the transport, not the protocol: every object it returns is a real
    openai_codex type, so a shape the vendor changes breaks these tests."""

    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.thread_start_calls: list[dict] = []
        self.thread_resume_calls: list[tuple[str, dict]] = []
        self.turn_calls: list[tuple] = []
        self.closed = False
        self.account_error: Exception | None = None
        self.resume_error: Exception | None = None
        self.next_notifications: list[Any] = []
        self.next_result: Any = None
        self.next_results: list[Any] = []
        # Task 9: what `_client.thread_read` answers with, and every call it
        # received as `(thread_id, include_turns)`. Armed with a REAL
        # `ThreadReadResponse`; `thread_read_error` (a real `CodexError`
        # subclass) models a missing or deleted thread, which `ThreadReadResponse`
        # has no way to express — its `thread` field is required and not
        # nullable — so the vendor raises instead of answering with an empty one.
        self.next_thread_read: Any = None
        self.thread_read_error: Exception | None = None
        self.thread_read_calls: list[tuple[str, bool]] = []
        self.turn_handles: list[FakeTurnHandle] = []
        # Applied to every FakeTurnHandle this client's threads create (see
        # FakeThread.turn) — the knobs on FakeTurnHandle, armed up front.
        self.next_handle_attrs: dict[str, Any] = {}
        # Task 7: an aclose() racing _ensure_client()'s own preflight-auth call
        # (the one await inside its _client_lock) needs that call held open;
        # None (the default) means account() answers immediately, as every
        # pre-Task-7 test relies on.
        self.account_hang: asyncio.Event | None = None
        # Fix round 4: the same shape as account_hang, for the three *setup*
        # RPCs. The load-bearing property holds for all three — a blocking
        # request with no timeout of its own, cancellable at the asyncio
        # level — so a wedged child stalls these exactly as it stalls
        # account() and interrupt(). None means answer immediately, as every
        # pre-round-4 test relies on.
        #
        # The vendor path is NOT identical, though (fix round 5, N2).
        # thread_start/thread_resume go through _call_sync -> asyncio.to_thread.
        # thread.turn goes through the module-level _TURN_START_EXECUTOR with
        # asyncio.wrap_future and a cancel callback — and cancelling it does
        # NOT stop the submitted work: the native turn starts anyway and only
        # the orphaned subscription is closed. This double cannot express
        # that, because turn_hang is checked BEFORE the handle is built, so a
        # timed-out thread.turn leaves turn_handles empty and every test sees
        # a turn that never started. Deliberate: there is no BOS-side
        # behaviour to assert yet (the turn id needed to interrupt it is what
        # the cancelled call never returned), and a characterization test
        # would only pin a limitation we would rather remove. If you reach
        # for turn_hang to reason about what the child is doing, this is the
        # sixth double-vs-vendor divergence found on this branch, and it is
        # here.
        self.thread_start_hang: asyncio.Event | None = None
        self.thread_resume_hang: asyncio.Event | None = None
        self.turn_hang: asyncio.Event | None = None
        # Task 9's read is bounded by the same `timeout_seconds`, and the
        # vendor path it stands in for is the plain one: `thread_read` ->
        # `_call_sync` -> `asyncio.to_thread` -> a queue read with no timeout,
        # cancellable at the asyncio level. The to_thread WORKER still parks
        # until the process ends, exactly as it does for a wedged `interrupt()`
        # — but that is a thread, not work the child is doing on BOS's behalf,
        # so this has no `turn_hang`-style divergence to declare: a cancelled
        # read leaves no orphaned native turn running against `cwd`.
        self.thread_read_hang: asyncio.Event | None = None
        # Task 8: _ensure_client replaces the approval handler through this
        # exact attribute path, with no getattr guard, so the double has to
        # carry the same shape or every test here would sail past the line
        # under test. Mirrors the vendor's AsyncCodex._client
        # (AsyncCodexClient, api.py:317) -> ._sync (CodexClient,
        # async_client.py:62) -> ._approval_handler (client.py:223).
        #
        # Seeded None, which the vendor's never is — there it defaults to the
        # bound CodexClient._default_approval_handler. Deliberate, and the one
        # divergence in this attribute: None makes "replaced" distinguishable
        # from "never installed", and the vendor's actual default is pinned
        # against the real package by
        # test_codex_approval_handler_attribute_exists instead.
        # `thread_read` sits here, not on FakeThread, because Task 9's read
        # runs the vendor's OWN `AsyncThread.read()` against this client:
        # `AsyncThread.read` awaits `self._codex._ensure_initialized()` and
        # then `self._codex._client.thread_read(self.id, include_turns=...)`
        # (api.py:775-778). Same arity and keyword as the real
        # `AsyncCodexClient.thread_read` (async_client.py:189).
        self._client = SimpleNamespace(
            _sync=SimpleNamespace(_approval_handler=None),
            thread_read=self._thread_read,
        )
        # The handler in place when account() was called, so a test can assert
        # the install happens BEFORE the first RPC that can spawn the child.
        self.approval_handler_at_account: Any = "account() not called"

    async def _ensure_initialized(self) -> None:
        """The real `AsyncCodex._ensure_initialized` starts the child and runs
        `initialize` once, under a lock (api.py:329-344). `AsyncThread.read`
        awaits it before the RPC (api.py:777); this double spawns nothing, so
        it is a no-op — it exists so the vendor's own `AsyncThread.read()` can
        run against this client at all.
        """

    async def _thread_read(self, thread_id: str, include_turns: bool = False) -> Any:
        self.thread_read_calls.append((thread_id, include_turns))
        if self.thread_read_hang is not None:
            await self.thread_read_hang.wait()
        if self.thread_read_error is not None:
            raise self.thread_read_error
        return self.next_thread_read

    async def account(self, *, refresh_token: bool = False) -> Any:
        self.approval_handler_at_account = self._client._sync._approval_handler
        if self.account_hang is not None:
            await self.account_hang.wait()
        if self.account_error is not None:
            raise self.account_error
        # GetAccountResponse.account is optional and ApiKeyAccount needs only its
        # literal discriminator field, so a real, fully-valid response costs
        # nothing here — the brief's documented sentinel fallback was not needed.
        from openai_codex.generated.v2_all import Account, ApiKeyAccount, GetAccountResponse

        return GetAccountResponse(account=Account(root=ApiKeyAccount(type="apiKey")), requires_openai_auth=False)

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        # Recorded before the wait, so a test can poll for "the RPC was
        # reached" instead of guessing how many loop ticks run() needs.
        self.thread_start_calls.append(kwargs)
        if self.thread_start_hang is not None:
            await self.thread_start_hang.wait()
        return FakeThread(self, f"thread-{len(self.thread_start_calls)}")

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        if self.resume_error is not None:
            raise self.resume_error
        self.thread_resume_calls.append((thread_id, kwargs))
        if self.thread_resume_hang is not None:
            await self.thread_resume_hang.wait()
        return FakeThread(self, thread_id)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_codex(monkeypatch):
    """Point CodexAgent's client factory at the double. Records every instance."""
    import bos.extensions.runtimes.codex as codex_mod

    class _Registry:
        def __init__(self) -> None:
            self.instances: list[FakeAsyncCodex] = []
            self._pending: dict[str, Any] = {}

        def arm(self, **attrs: Any) -> None:
            """Set attributes on the *next* instance this registry builds.

            ``CodexAgent`` builds its client lazily inside ``_thread_for`` /
            ``_ensure_client``, with no seam in between for a test to reach in
            after construction but before the client is used — so arming a
            failure (e.g. ``resume_error``) has to happen before that call,
            against the instance that does not exist yet.
            """
            self._pending.update(attrs)

        def __call__(self, config: Any = None) -> FakeAsyncCodex:
            instance = FakeAsyncCodex(config)
            for name, value in self._pending.items():
                setattr(instance, name, value)
            self._pending.clear()
            self.instances.append(instance)
            return instance

    registry = _Registry()
    monkeypatch.setattr(codex_mod, "_CODEX_FACTORY", registry)
    return registry


# ── Claude Code real-CLI support (BEP 19 Layer 4b) ──────────────────────────
#
# The model is the only fake: tests drive the real `claude` CLI bundled in
# claude-agent-sdk against FakeAnthropic, so every permission decision in them is
# the vendor's own.


# Each of these, when set, moves some of the CLI's per-user config, data, cache or
# state out of HOME — its Anthropic credentials lookup ($XDG_CONFIG_HOME/anthropic)
# among them.
_XDG_HOMES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


def _move_claude_code_host_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ClaudeCodeAgent`` refuses construction on two host files: the CLI's well-known API key
    file (under ``auth = "subscription"``) and the administrator's enterprise ``managed-mcp.json``
    (always). A developer's machine may have either, which would fail every Claude Code test at
    construction, so both point at a path that cannot exist — a child of ``/dev/null``. Tests
    that want a file there point the path at one themselves. Without the ``claude-code`` extra
    there is no runtime to patch, and nothing is done."""
    try:
        claude_code = importlib.import_module("bos.extensions.runtimes.claude_code")
    except ImportError:
        return
    monkeypatch.setattr(claude_code, "_WELL_KNOWN_API_KEY_FILE", Path(os.devnull, "no-well-known-api-key"))
    monkeypatch.setattr(claude_code, "_MANAGED_MCP_FILE", Path(os.devnull, "no-managed-mcp.json"))


@pytest.fixture(autouse=True)
def _claude_code_isolation(monkeypatch, tmp_path):
    """Every test, whether or not it is about Claude Code: ``CLAUDE_CONFIG_DIR`` points under the
    test's own *tmp_path* — the directory ``claude_cli_env`` also names — so nothing that computes
    the CLI's config directory (the hook's ``_cli_config_dir``, ``get_session_messages``) resolves
    the developer's own ``~/.claude``; tests of the unset fallback unset it and patch ``HOME``
    themselves. And the host files ``_move_claude_code_host_files`` names are moved away."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    _move_claude_code_host_files(monkeypatch)


@pytest.fixture
def fake_anthropic(monkeypatch):
    """A fresh FakeAnthropic for one test (tests/fake_anthropic.py), closed after it.

    Also removes every ``CLAUDE*``, ``ANTHROPIC*`` and ``MCP_*`` variable, and the XDG
    base directories in ``_XDG_HOMES``, from this process for the test's duration. The
    SDK builds the child's environment from ``os.environ`` with ``options.env``
    layered on top, so ``claude_cli_env`` can override a variable but never unset one.
    A pytest run started from inside a Claude Code session inherits that session's
    variables — ``CLAUDE_CODE_SESSION_ID``, ``CLAUDE_CODE_MESSAGING_SOCKET``,
    ``MCP_CONNECTION_NONBLOCKING`` among them, all read by the CLI — and a developer's
    shell may export ``ANTHROPIC_*`` credentials or an ``XDG_CONFIG_HOME`` holding real
    ones. CI is not clean either: GitHub's Ubuntu runner image sets ``XDG_CONFIG_HOME``.
    Scrubbing makes every run hand the child the same environment. A test that needs one
    of these set for the test process itself sets it after this fixture has run. Kept:
    ``CLAUDE_CONFIG_DIR``, which ``_claude_code_isolation`` has already pointed under the
    test's own directory, replacing any inherited value.
    """
    for name in list(os.environ):
        if name == "CLAUDE_CONFIG_DIR":
            continue
        if name.startswith(("CLAUDE", "ANTHROPIC", "MCP_")) or name in _XDG_HOMES:
            monkeypatch.delenv(name)
    fake = FakeAnthropic()
    yield fake
    fake.close()


class FakeClaudeClient:
    """Stands in for ``claude_agent_sdk.ClaudeSDKClient`` at ``claude_code._CLIENT_FACTORY``.

    Fakes the transport, never the vendor's data: ``receive_response`` yields what a test
    armed in ``messages``, and those are the SDK's own message dataclasses
    (``SystemMessage``, ``AssistantMessage``, ``ResultMessage``, …), so a shape the vendor
    changes breaks the tests instead of a look-alike drifting from it. Only the methods
    ``ClaudeCodeAgent`` calls exist here.

    One instance per turn, as the runtime builds one client per turn (BEP 19 §3.10.1) — a
    schema retry included, since it re-queries the same connected client rather than building a
    new one — so every knob is per instance: arm it through the ``fake_claude`` fixture before
    the turn that builds it. ``connect_error`` is raised by ``connect()`` — where the real SDK
    raises a startup refusal, such as a ``resume`` the CLI cannot honour. ``release``, when
    set, holds ``receive_response`` open until the test sets it, with ``waiting`` set once
    it is held.

    ``messages`` may hold more than one round concatenated — each round is whatever a real
    ``query()``/``receive_response()`` cycle would stream, ending in its own ``ResultMessage``
    — for a test that arms a schema retry: the first ``receive_response()`` call consumes up to
    the first ``ResultMessage``, and a second ``query()`` call (BOS's own correction message)
    makes the next ``receive_response()`` continue from there, through the second round's own
    ``ResultMessage``. Every test written before schema retries existed calls it only once, so
    this is additive and changes no existing test's behaviour.

    Three markers may sit among ``messages`` (BEP 19 §3.9, §3.10.2). ``HANG`` — the one the
    Codex double uses — pauses the stream with ``hang_reached`` set, a turn the CLI is still
    working on, until the test sets ``hang_release``: that stands in for the CLI confirming an
    interrupt, and BOS's own interrupt request never sets it, so a test can also model a CLI
    that never confirms. ``ECHO`` is the CLI's ``--replay-user-messages`` echo of the oldest
    mid-turn message BOS sent and nothing has echoed yet — a ``UserMessage`` carrying the uuid
    BOS put on it — or nothing, when there is none; where a test puts it says when the CLI took
    that message into a turn. A ``Pause(seconds)`` is the CLI taking that long before its next
    message.

    The interrupt BOS sends is the CLI's ``interrupt`` control request, which it reaches through
    ``client._query._send_control_request`` (``claude_code._interrupt`` says why), so this
    double is its own ``_query`` and records each request in ``control_requests``.
    ``interrupt_hang`` blocks that request, ``interrupt_error`` fails it, ``steer_error`` fails a
    mid-turn message's ``query()`` and ``steer_hang`` blocks it, ``connect_hang`` blocks
    ``connect()``, ``disconnect_hang`` blocks ``disconnect()`` and ``disconnect_error`` fails it
    (``disconnect_started`` is set on the way in; ``disconnect_calls`` counts calls, ``disconnected``
    is set once one completes), and
    ``swallow_cancel`` makes a ``HANG`` ignore cancellation — standing in, as in the Codex double,
    for the host code a stream task awaits (the sink, the interrupt callback), which can swallow
    one. ``writes`` records "steer" and "interrupt" in the order they reach the CLI.
    """

    def __init__(self, options: Any) -> None:
        self.options = options
        self.messages: list[Any] = []
        self._consumed = 0  # index into `messages`; advances across query()/receive_response() rounds
        self.connect_error: BaseException | None = None
        self.connect_hang: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.waiting = asyncio.Event()
        self.hang_reached = asyncio.Event()
        self.hang_release = asyncio.Event()
        self.swallow_cancel = False
        self.cancels_swallowed = 0
        self.prompts: list[Any] = []
        self.steers: list[dict[str, Any]] = []  # mid-turn messages: a user message dict with a uuid
        self._unechoed: list[dict[str, Any]] = []
        self.steer_error: Exception | None = None
        self.steer_hang: asyncio.Event | None = None
        self.control_requests: list[dict[str, Any]] = []
        self.interrupt_hang: asyncio.Event | None = None
        self.interrupt_error: Exception | None = None
        self.writes: list[str] = []
        self._query = self
        self.connected = False
        self.disconnect_hang: asyncio.Event | None = None
        self.disconnect_error: Exception | None = None
        self.disconnect_started = asyncio.Event()
        self.disconnect_calls = 0
        self.disconnected = False

    async def connect(self, prompt: Any = None) -> None:
        if self.connect_hang is not None:
            await self.connect_hang.wait()
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        # An AsyncIterable prompt is collected into the message dicts it yields.
        collected = prompt if isinstance(prompt, str) else [message async for message in prompt]
        self.prompts.append(collected)
        steers = [message for message in collected if "uuid" in message] if isinstance(collected, list) else []
        if steers and self.steer_hang is not None:
            await self.steer_hang.wait()
        if steers and self.steer_error is not None:
            raise self.steer_error
        self.steers += steers
        self._unechoed += steers
        self.writes += ["steer"] * len(steers)

    async def receive_response(self):
        from claude_agent_sdk import ResultMessage, UserMessage

        if self.release is not None:
            self.waiting.set()
            await self.release.wait()
        while self._consumed < len(self.messages):
            message = self.messages[self._consumed]
            self._consumed += 1
            if message is HANG:
                self.hang_reached.set()
                while True:
                    try:
                        await self.hang_release.wait()
                        break
                    except asyncio.CancelledError:
                        if not self.swallow_cancel:
                            raise
                        self.cancels_swallowed += 1
                continue
            if isinstance(message, Pause):
                await asyncio.sleep(message.seconds)
                continue
            if message is ECHO:
                if not self._unechoed:
                    continue
                steer = self._unechoed.pop(0)
                message = UserMessage(content=steer["message"]["content"], uuid=steer["uuid"])
            yield message
            if isinstance(message, ResultMessage):  # as the SDK's own receive_response stops
                return

    async def _send_control_request(self, request: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        self.control_requests.append(request)
        self.writes.append(request.get("subtype", "?"))
        if self.interrupt_hang is not None:
            await self.interrupt_hang.wait()
        if self.interrupt_error is not None:
            raise self.interrupt_error
        return {}

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_started.set()
        if self.disconnect_hang is not None:
            await self.disconnect_hang.wait()
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.disconnected = True


class _Echo:
    """See ``FakeClaudeClient``: the CLI echoing a mid-turn message BOS sent."""


ECHO = _Echo()


class Pause:
    """Among ``FakeClaudeClient.messages``: the CLI taking *seconds* before its next message."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


@pytest.fixture
def fake_claude(monkeypatch):
    """Point ClaudeCodeAgent's client factory at FakeClaudeClient. Records every instance
    in ``instances``; ``arm(**knobs)`` sets knobs on the next instance built."""
    import bos.extensions.runtimes.claude_code as claude_code_mod

    class _Registry:
        def __init__(self) -> None:
            self.instances: list[FakeClaudeClient] = []
            self._pending: dict[str, Any] = {}

        def arm(self, **knobs: Any) -> None:
            self._pending.update(knobs)

        def __call__(self, options: Any) -> FakeClaudeClient:
            instance = FakeClaudeClient(options)
            for name, value in self._pending.items():
                setattr(instance, name, value)
            self._pending.clear()
            self.instances.append(instance)
            return instance

    registry = _Registry()
    monkeypatch.setattr(claude_code_mod, "_CLIENT_FACTORY", registry)
    return registry


def claude_cli_env(tmp_path: Path, fake: FakeAnthropic) -> dict[str, str]:
    """The environment a real-CLI test hands the ``claude`` child, as ``options.env``.

    ``HOME`` and ``CLAUDE_CONFIG_DIR`` are two directories under *tmp_path*, created if
    missing, so a second call with the same *tmp_path* returns the same environment —
    what a resumed session needs to find its transcript. With the ``fake_anthropic``
    fixture's scrub, the child's config, credentials and transcripts all resolve under
    them, never in the developer's ``~/.claude`` or ``~/.claude.json``. In these fresh
    directories the CLI keeps its global config — workspace trust included — at
    ``<CLAUDE_CONFIG_DIR>/.claude.json``, not ``<HOME>/.claude.json``;
    ``test_fact_4_*`` in test_claude_code_vendor_facts.py pins that. It is not the general
    rule: the CLI prefers a legacy ``.config.json`` in the config directory when one exists,
    and names the file ``.claude-custom-oauth.json`` when ``CLAUDE_CODE_CUSTOM_OAUTH_URL``
    is set. The API key is a placeholder only the fake ever sees.
    """
    home, config = tmp_path / "home", tmp_path / "claude-config"
    home.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(config),
        "ANTHROPIC_BASE_URL": fake.url,
        "ANTHROPIC_API_KEY": "sk-ant-fake",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }
