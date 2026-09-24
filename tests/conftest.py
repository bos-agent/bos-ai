"""Shared test fixtures and lightweight in-memory doubles."""

from __future__ import annotations

import asyncio
import contextlib
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


class FakeTurnHandle:
    """Stands in for openai_codex.AsyncTurnHandle."""

    def __init__(self, thread: FakeThread, turn_id: str, notifications: list[Any], result: Any) -> None:
        self._thread, self.id = thread, turn_id
        self._notifications, self._result = notifications, result
        self.interrupted = False

    async def stream(self):
        for notification in self._notifications:
            yield notification

    async def interrupt(self) -> None:
        self.interrupted = True

    async def run(self) -> Any:
        # Mirrors openai_codex._run._raise_for_failed_turn exactly: the real
        # AsyncTurnHandle.run() raises a bare RuntimeError for a failed turn
        # *before* ever constructing a TurnResult (that function runs inside
        # _collect_async_turn_result, ahead of the `TurnResult(...)` call) —
        # so a caller's `await thread.run(...)` never receives a TurnResult
        # whose status is `failed`. Only mirrored here, not in .stream():
        # CodexAgent doesn't call .stream() until Task 6.
        if self._result is not None:
            from openai_codex.generated.v2_all import TurnStatus

            if self._result.status is TurnStatus.failed:
                error = self._result.error
                if error is not None and error.message:
                    raise RuntimeError(error.message)
                raise RuntimeError(f"turn failed with status {self._result.status.value}")
        return self._result


class FakeThread:
    """Stands in for openai_codex.AsyncThread."""

    def __init__(self, codex: FakeAsyncCodex, thread_id: str) -> None:
        self._codex, self.id = codex, thread_id
        self.read_calls: list[bool] = []

    async def turn(self, input: Any, **kwargs: Any) -> FakeTurnHandle:
        self._codex.turn_calls.append((self.id, input, kwargs))
        # A queued result (Task 5 fix round: schema-retry tests need a
        # *different* TurnResult per call within one run() invocation) takes
        # priority; next_result is the pre-existing single persistent slot,
        # unchanged for every caller that never touches next_results.
        result = self._codex.next_results.pop(0) if self._codex.next_results else self._codex.next_result
        return FakeTurnHandle(self, f"turn-{len(self._codex.turn_calls)}", self._codex.next_notifications, result)

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

    async def account(self, *, refresh_token: bool = False) -> Any:
        if self.account_error is not None:
            raise self.account_error
        # GetAccountResponse.account is optional and ApiKeyAccount needs only its
        # literal discriminator field, so a real, fully-valid response costs
        # nothing here — the brief's documented sentinel fallback was not needed.
        from openai_codex.generated.v2_all import Account, ApiKeyAccount, GetAccountResponse

        return GetAccountResponse(account=Account(root=ApiKeyAccount(type="apiKey")), requires_openai_auth=False)

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.thread_start_calls.append(kwargs)
        return FakeThread(self, f"thread-{len(self.thread_start_calls)}")

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        if self.resume_error is not None:
            raise self.resume_error
        self.thread_resume_calls.append((thread_id, kwargs))
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
