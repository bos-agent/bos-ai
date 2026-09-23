"""bos.sdk — the embedding contract (BEP 18)."""

from __future__ import annotations

import pytest

from bos.sdk import open_harness


def _config() -> dict:
    # bos.exts must load: InMemChatStore/InMemMailRoute are registered by it, and
    # with extensions=[] `workspace.harness()` raises
    # "Extension 'InMemMailRoute' not found". Task 4 also needs `BOS` registered.
    return {
        "platform": {"extensions": ["bos.exts"]},
        "harness": {"chat_store": "InMemChatStore", "mail_route": "InMemMailRoute"},
        "agents": {"solo": {"system_prompt": "hi"}},
    }


@pytest.mark.asyncio
async def test_open_harness_bootstraps_and_yields_a_usable_harness(tmp_path):
    from bos.config import Workspace
    from bos.core import AgentRegistry

    ws = Workspace(tmp_path, tmp_path / ".bos", _config())
    async with open_harness(ws) as harness:
        # bootstrap_platform ran: the config's agents are registered.
        assert AgentRegistry.has_registered("solo")
        agent = await harness.create_agent(kind="solo")
        assert agent is not None


@pytest.mark.asyncio
async def test_open_harness_resolves_agent_files_before_registering(tmp_path):
    """The order is the point. bootstrap_platform registers what resolve_agents
    loaded, so reversing them drops every agent file silently (BEP 18 §3.3)."""
    from bos.config import Workspace
    from bos.core import AgentRegistry

    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fromfile.md").write_text("---\nname: fromfile\n---\n\nYou are from a file.\n")

    ws = Workspace(tmp_path, tmp_path / ".bos", _config())
    async with open_harness(ws):
        assert AgentRegistry.has_registered("fromfile")
