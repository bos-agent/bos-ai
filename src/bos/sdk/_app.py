"""BosApp — an embedder's handle on a running BOS (BEP 18 §3.4)."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bos.config import Workspace

from ._bootstrap import open_harness

if TYPE_CHECKING:
    from bos.config import RootConfig
    from bos.core import AgentHarness, AgentPort, Message

logger = logging.getLogger(__name__)

# bootstrap_platform() writes os.environ and clears AgentRegistry, both process
# global, so a second live BosApp would wipe the first's agents while it is still
# running. Refusing beats corrupting, and the guard is released on exit so a
# later app still works.
_ACTIVE: BosApp | None = None


class BosApp:
    """Owns a harness and caches the agents built from it.

    It returns ``Agent`` rather than wrapping ``run()``: dropping to the lower
    layer must be the *same* objects, not a different path, which is what
    ``harness`` and ``workspace`` are for.
    """

    def __init__(
        self, config: dict[str, Any] | RootConfig | Workspace, *, bos_dir: str | Path | None = None
    ) -> None:
        if isinstance(config, Workspace):
            self._workspace = config
        elif bos_dir is None:
            raise ValueError(
                "bos_dir is required unless you pass a Workspace. File-backed adapters "
                "need a path even when the configured stores are in memory."
            )
        else:
            self._workspace = Workspace(workspace=".", bos_dir=bos_dir, config=config)
        self._stack: contextlib.AsyncExitStack | None = None
        self._harness: AgentHarness | None = None
        self._agents: dict[str, AgentPort] = {}

    async def __aenter__(self) -> BosApp:
        global _ACTIVE
        if _ACTIVE is not None:
            raise RuntimeError(
                "Another BosApp is already active in this process. bootstrap_platform() "
                "writes os.environ and rebuilds the agent registry, so two would overwrite "
                "each other. Close the first, or share one BosApp."
            )
        _ACTIVE = self
        stack = contextlib.AsyncExitStack()
        try:
            self._harness = await stack.enter_async_context(open_harness(self._workspace))
            # Every kind the config names, plus the resolved default, so agent()
            # can stay synchronous. The default needs building separately because
            # it often is not in `config.agents` at all — the shipped `default`
            # preset sets `default_agent = "BOS"` with an empty [agents] and lets
            # an @ep_agent factory supply it, which is the shape `app.agent()` in
            # the docs has to work on.
            #
            # Tried, not required — but only where resolution is *inference*. A
            # project that never wrote `default_agent` should not fail to start
            # just because the only-agent/`main` guesses are ambiguous, so that
            # ValueError is swallowed. A project that did write the key meant it:
            # `default_agent = "typoo"` is a config error, and startup is what it
            # should surface at, not the first agent() deep in a request handler.
            # Everything else propagates either way, including a kind that
            # resolves but cannot be built.
            for kind in self._workspace.config.agents or {}:
                self._agents[kind] = await self._harness.create_agent(kind=kind)
            try:
                default_kind: str | None = self._workspace.resolve_default_agent()
            except ValueError:
                if self._workspace.config.default_agent:
                    raise
                default_kind = None
            if default_kind is not None and default_kind not in self._agents:
                self._agents[default_kind] = await self._harness.create_agent(kind=default_kind)
        except BaseException:
            # _aclose() (bos/core/_utils.py) already catches and logs an ordinary
            # Exception from a resource's own close, so this rarely fires. It stays
            # defensive because a BaseException from a close, or a failure in the
            # exit-stack machinery itself, could still escape — and a leaked _ACTIVE
            # bricks every later BosApp in the process, which is bad enough to guard
            # against even on a low-probability path. `except Exception`, not
            # BaseException: a CancelledError landing here must keep propagating, not
            # get logged and dropped. Either way, the `finally` below is what
            # guarantees the reset — not this except — and the bare `raise` after it
            # re-raises the original entry failure, not a teardown error masking it.
            try:
                await stack.aclose()
            except Exception:
                logger.error("Error tearing down BosApp after a failed __aenter__", exc_info=True)
            finally:
                _ACTIVE = None
                self._harness = None
                self._agents.clear()
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc: object) -> None:
        global _ACTIVE
        try:
            if self._stack is not None:
                await self._stack.aclose()
        finally:
            self._stack = None
            self._harness = None
            self._agents.clear()
            if _ACTIVE is self:
                _ACTIVE = None

    def agent(self, kind: str | None = None) -> AgentPort:
        """A cached agent. With no *kind*, the workspace's default."""
        self._require_open()
        if kind is None:
            kind = self._workspace.resolve_default_agent()
        if kind in self._agents:
            return self._agents[kind]
        from bos.core import AgentRegistry

        if AgentRegistry.has_registered(kind):
            raise RuntimeError(
                f"Agent {kind!r} is registered but not built: your config does not name it, "
                f"and building one is async. Use `await app.build_agent({kind!r})`."
            )
        known = ", ".join(sorted(set(self._agents) | set(AgentRegistry.describe()))) or "none"
        raise RuntimeError(f"Unknown agent {kind!r}. Available: {known}.")

    async def build_agent(self, kind: str, agent_cfg: dict[str, Any] | None = None) -> AgentPort:
        """Build, cache and return an agent the config does not name.

        *agent_cfg* is the highest-precedence config layer (BEP 19 §3.4.1.1) —
        it is how an embedder supplies per-agent options, including an external
        runtime's `cwd`, `permission`, `system_prompt` and `mcp_tools`, without
        writing TOML.

        Caching is **per kind**. A kind already cached — because `__aenter__`
        pre-built every kind your config names in `[agents]`, or because an
        earlier `build_agent` call did — cannot take new config: passing
        *agent_cfg* for one raises rather than silently discarding it. Call
        `build_agent(kind)` with no *agent_cfg* to get the cached agent
        unchanged; give an override its own kind (e.g. a named `_parent`
        instance, BEP 19 §3.4) for a second configuration of the same runtime.
        """
        harness = self._require_open()
        if kind in self._agents:
            if agent_cfg is not None:
                raise RuntimeError(
                    f"Agent {kind!r} is already built — either your config names it in "
                    f"`[agents]` (built at app entry, before this call) or an earlier "
                    f"`build_agent({kind!r}, ...)` call cached it. A cached agent cannot "
                    f"take new config: {agent_cfg!r} would be silently discarded. Call "
                    f"`build_agent({kind!r})` with no `agent_cfg` for the cached agent, or "
                    "give the override its own kind (e.g. a `_parent`-inheriting agent file)."
                )
            return self._agents[kind]
        self._agents[kind] = await harness.create_agent(kind=kind, agent_cfg=agent_cfg)
        return self._agents[kind]

    async def get_messages(
        self, chat_id: str, *, source: Literal["auto", "bos", "native"] = "auto"
    ) -> list[Message]:
        """A chat's messages, from BOS or from the runtime that owns the session.

        ``"bos"`` is the record BOS persists and guarantees: two messages per
        turn for an externally-backed chat, always available, and changed by
        nothing outside BOS. It is the *active-context* window, so a summary
        written over the chat does shorten it — that is BOS's own compaction,
        not the runtime's. ``"auto"`` picks
        ``"native"`` when the chat's stored turn metadata names an external
        runtime, and ``"bos"`` otherwise.

        ``"native"`` delegates to that runtime's own ``native_messages``, and
        **it promises nothing** (BEP 19 §3.7). It is a live read of a store BOS
        does not own: each runtime compacts and prunes on its own schedule, a
        Codex thread can be archived or deleted, and Claude Code's transcripts
        live under the invoking user's home directory. So a native read can
        return fewer messages than it did last time, can contain markers where
        the runtime declined to load part of its own history, and can fail
        outright — none of which is a bug in BOS. ``"bos"`` is the only read
        BOS stands behind.

        **It is the conversation, not the work: user and assistant messages
        only, no tool activity.** A runtime's tool calls, results, reasoning
        and file edits are not projected, because a BOS ``Message`` carries
        tool activity as a *pair* — an assistant message advertising
        ``tool_calls``, then a ``role="tool"`` message whose ``tool_call_id``
        matches it — and a native transcript has no such pairing, so building
        one would mean inventing call ids the runtime never issued. BOS has
        that activity nowhere else either: it is streamed live to an
        ``event_sink`` while BOS runs a turn and is never persisted, and for a
        session BOS did not run there was no stream. If you need it, read the
        runtime's own store with the runtime's own tools.

        Which runtime is asked is decided by the chat's stored metadata, and
        which *agent* speaks for that runtime is decided by matching
        ``resolved_config["external_runtime"]`` against it. That match is not
        guessed at: no built agent for the runtime, more than one, or one that
        cannot read its transcript back each raise rather than quietly reading
        the wrong thing or falling back to the BOS record, which would answer a
        different question than the one asked.
        """
        harness = self._require_open()
        store = harness.chat_store
        if store is None:
            raise RuntimeError("BosApp requires an active AgentHarness with a chat_store service.")
        messages = await store.get_messages(chat_id)
        if source == "bos":
            return messages
        # active_only=False: same reasoning as _shared.read_native_session_id — a
        # summary written over this chat must not hide the message carrying the
        # routing metadata from this scan. Read separately from the "bos" return
        # above, which BEP 19 §3.7 pins to the active-window read, unchanged.
        routing_messages = await store.get_messages(chat_id, active_only=False)
        runtime = next(
            (m.metadata["external_runtime"] for m in reversed(routing_messages) if m.metadata.get("external_runtime")),
            None,
        )
        if source == "auto" and runtime is None:
            return messages
        if runtime is None:
            raise RuntimeError(
                f'get_messages({chat_id!r}, source="native") has no runtime to ask: nothing in that '
                f"chat's stored metadata names an external runtime, so either no external agent ever "
                f'served it or the chat does not exist. Use source="bos" for the record BOS persists.'
            )
        # `resolved_config` is part of what every ExternalRuntime promises
        # (BEP 19 §3.3), and the only part of it that names the vendor behind
        # the agent. Reached with `getattr` because `AgentPort` does not
        # promise it and BOS's own `Agent` does not have it — `self._agents`
        # holds both kinds.
        candidates = [
            (kind, agent)
            for kind, agent in self._agents.items()
            if (getattr(agent, "resolved_config", None) or {}).get("external_runtime") == runtime
        ]
        if not candidates:
            raise RuntimeError(
                f"Chat {chat_id!r} was served by the {runtime!r} runtime, but no agent built on this "
                f"BosApp declares it. Build one (e.g. `await app.build_agent({runtime!r})`) before "
                f'reading its native transcript, or use source="bos".'
            )
        if len(candidates) > 1:
            # BOS stores the runtime per turn, not the agent kind, so two
            # agents on the same runtime are indistinguishable from here — and
            # they are not interchangeable in general: a Claude Code transcript
            # is keyed by the agent's `cwd`, so the wrong one reads a different
            # conversation. Named rather than picked (BEP 19 §8.2).
            kinds = ", ".join(sorted(kind for kind, _ in candidates))
            raise RuntimeError(
                f"Chat {chat_id!r} was served by the {runtime!r} runtime and {len(candidates)} built "
                f"agents declare it ({kinds}). BOS does not record which one served the chat and will "
                f"not guess, because the wrong one can read a different transcript. Call "
                f"`app.agent(<kind>).native_messages({chat_id!r})` on the one you mean."
            )
        kind, agent = candidates[0]
        # Duck-typed, not an AgentPort/ExternalRuntime method: only a runtime
        # that has a native transcript can offer this, and widening either
        # protocol would oblige every agent — including BOS's own `Agent` — to
        # implement it. The cost of that choice is this check, which turns a
        # bare AttributeError into the sentence below.
        native_messages = getattr(agent, "native_messages", None)
        if native_messages is None:
            raise RuntimeError(
                f"Agent {kind!r} backs chat {chat_id!r} with the {runtime!r} runtime but does not "
                f'implement native_messages(), so its transcript cannot be read back. Use source="bos".'
            )
        return await native_messages(chat_id)

    @property
    def harness(self) -> AgentHarness:
        return self._require_open()

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    def _require_open(self) -> AgentHarness:
        if self._harness is None:
            raise RuntimeError("BosApp is not open. Use `async with BosApp(...) as app:`.")
        return self._harness
