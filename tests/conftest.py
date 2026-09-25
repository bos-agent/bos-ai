"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

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

    async def run(self) -> Any:
        # Mirrors openai_codex._run._raise_for_failed_turn exactly: the real
        # AsyncTurnHandle.run() raises a bare RuntimeError for a failed turn
        # *before* ever constructing a TurnResult (that function runs inside
        # _collect_async_turn_result, ahead of the `TurnResult(...)` call) —
        # so a caller's `await thread.run(...)` never receives a TurnResult
        # whose status is `failed`. CodexAgent itself has called only
        # .stream() since Task 6 (its own codex.py has the same mirror,
        # reached through _emit_stream instead) — this method is unreached by
        # production code now, but kept as a faithful stand-in for the real
        # AsyncThread.run()/AsyncTurnHandle.run() surface.
        if self._result is not None:
            from openai_codex.generated.v2_all import TurnStatus

            if self._result.status is TurnStatus.failed:
                error = self._result.error
                if error is not None and error.message:
                    raise RuntimeError(error.message)
                raise RuntimeError(f"turn failed with status {self._result.status.value}")
        return self._result


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
                id=f"{turn_id}-response", text=result.final_response, phase=MessagePhase.final_answer,
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
        self.read_calls: list[bool] = []

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

    async def run(self, input: Any, **kwargs: Any) -> Any:
        handle = await self.turn(input, **kwargs)
        return await handle.run()

    async def read(self, *, include_turns: bool = False) -> Any:
        self.read_calls.append(include_turns)
        return self._codex.next_thread_read


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
        self.next_thread_read: Any = None
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
        self._client = SimpleNamespace(_sync=SimpleNamespace(_approval_handler=None))
        # The handler in place when account() was called, so a test can assert
        # the install happens BEFORE the first RPC that can spawn the child.
        self.approval_handler_at_account: Any = "account() not called"

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
