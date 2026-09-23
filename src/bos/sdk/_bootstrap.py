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

    Split from ``open_harness`` because one caller needs exactly this and no
    harness: ``boscli gateway start`` validates in the parent process, since the
    work then happens in a child and a bad agent file should fail in front of
    the operator rather than in the daemon log.
    """
    workspace.resolve_agents()
    workspace.bootstrap_platform()


@asynccontextmanager
async def open_harness(workspace: Workspace) -> AsyncIterator[AgentHarness]:
    """Resolve agents, bootstrap the platform, and open the harness.

    The order is the contract, not a detail: ``bootstrap_platform`` registers the
    agents ``resolve_agents`` loaded from ``agent_dirs``, so calling them the
    other way round drops every agent file with no error. This existed in three
    copies — ``boscli ask``, the gateway-start pre-flight, and
    ``GatewayMount._bring_up_runtime`` — and the ordering was stated in none of
    them.
    """
    bootstrap(workspace)
    async with workspace.harness() as harness:
        yield harness
