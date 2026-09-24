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

        ``"bos"`` is the record BOS persists and guarantees. ``"native"`` reads the
        external runtime's own transcript, which BOS does not own — it can be
        compacted or deleted by that runtime (BEP 19 §3.7). ``"auto"`` picks
        ``"native"`` when the stored turn metadata names an external runtime.
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
        raise NotImplementedError(
            "Reading a native transcript needs the runtime adapters (BEP 19 §6 Layer 4). "
            'Use source="bos" for the record BOS persists.'
        )

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
