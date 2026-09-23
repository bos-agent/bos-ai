"""BosApp — an embedder's handle on a running BOS (BEP 18 §3.4)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bos.config import Workspace

from ._bootstrap import open_harness

if TYPE_CHECKING:
    from bos.core import Agent, AgentHarness

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

    def __init__(self, config: Any, *, bos_dir: str | Path | None = None) -> None:
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
        self._agents: dict[str, Agent] = {}

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
            # Every kind the config names, so agent() can stay synchronous. The
            # default is resolved lazily in agent(), not here: a project that
            # always names its agent should not fail to start just because the
            # default would be ambiguous.
            for kind in self._workspace.config.agents or {}:
                self._agents[kind] = await self._harness.create_agent(kind=kind)
        except BaseException:
            await stack.aclose()
            _ACTIVE = None
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

    def agent(self, kind: str | None = None) -> Agent:
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

    async def build_agent(self, kind: str) -> Agent:
        """Build, cache and return an agent the config does not name."""
        harness = self._require_open()
        if kind not in self._agents:
            self._agents[kind] = await harness.create_agent(kind=kind)
        return self._agents[kind]

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
