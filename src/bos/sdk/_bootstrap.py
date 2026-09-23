"""The bootstrap sequence, in one place (BEP 18 §3.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bos.config import Workspace
    from bos.core import AgentHarness


def bootstrap(workspace: Workspace) -> None:
    """Load the agent files, then register everything the platform declares.

    Split from ``open_harness`` because four callers need exactly this — three
    want no harness at all, and one wants the registries filled *before* it
    opens its own:

    - ``boscli ask`` — resolves the agent kind against the registry this fills
      (and, on ``--actor``, the actor table), then opens ``ws.harness()`` itself.
    - ``boscli gateway start``, background only — a pre-flight in the parent
      process, since the work then happens in a child and a bad agent file
      should fail in front of the operator rather than in the daemon log.
    - ``bos.cli.commands.inspect._collect`` — the registries the capability
      report reads from; best-effort, so a broken extension does not blank it.
    - ``bos.cli.commands.scaffolding._bootstrapped_workspace`` — the model probe
      needs registered providers, not a harness.
    """
    workspace.resolve_agents()
    workspace.bootstrap_platform()


@asynccontextmanager
async def open_harness(workspace: Workspace) -> AsyncIterator[AgentHarness]:
    """Resolve agents, bootstrap the platform, and open the harness.

    The order is the contract, not a detail: ``bootstrap_platform`` registers the
    agents ``resolve_agents`` loaded from ``agent_dirs``, so calling them the
    other way round drops every agent file with no error. The sequence existed in
    three copies — ``boscli ask``, the gateway-start pre-flight and
    ``GatewayMount._bring_up_runtime`` — and the ordering was stated in none of
    them. Of the three, only ``GatewayMount._bring_up_runtime`` wants a harness
    out of it, so it is this function's one caller outside ``BosApp``; the other
    two call ``bootstrap`` above.
    """
    bootstrap(workspace)
    async with workspace.harness() as harness:
        yield harness
