"""Tests for the built-in bos_config agent (BEP 15)."""

import pytest
from conftest import dummy_turn_context

from bos.config.workspace import Workspace
from bos.core import AgentRegistry, ep_agent
from bos.extensions.agents.bos_config import BOS_CONFIG_AGENT_NAME


@pytest.fixture(autouse=True)
def _reset_ep_agent_defaults():
    """Tests here run ``bootstrap_platform`` with ``[exts.ep_agent.*]`` config,
    which writes into the process-global ``ep_agent`` extension defaults (before
    the factory can even reject a bad value). Snapshot and restore them so a
    test's workflow setting does not leak into other tests."""
    saved_defaults = {name: dict(ext.defaults) for name, ext in ep_agent._extensions.items()}
    try:
        yield
    finally:
        for name, ext in ep_agent._extensions.items():
            ext.defaults.clear()
            ext.defaults.update(saved_defaults.get(name, {}))


def test_bos_config_registered_at_bootstrap(tmp_path):
    """The bos_config agent registers from the built-in ep_agent factory."""
    ws = Workspace(tmp_path, tmp_path / ".bos", {})
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered(BOS_CONFIG_AGENT_NAME)
    defaults = AgentRegistry.get_defaults(BOS_CONFIG_AGENT_NAME)
    assert defaults["kind"] == "bos_config"
    # Explicit tool allowlist — no wildcard, no web tools (BEP 15 §3.3).
    assert defaults["tools"] == ["Bash", "ReadFile", "EditFile", "WriteFile", "GrepSearch", "GlobSearch"]
    # Lean subagent: no plugins.
    assert defaults["plugins"]["enabled"] == []


def test_bos_config_description_is_routing_rule(tmp_path):
    """The registry description carries the ALWAYS-delegate routing rule (BEP 15 §3.7)."""
    ws = Workspace(tmp_path, tmp_path / ".bos", {})
    ws.bootstrap_platform()
    description = AgentRegistry.describe()[BOS_CONFIG_AGENT_NAME]
    assert "ALWAYS delegate" in description
    assert ".bos/config.toml" in description


def test_bos_config_prompt_contains_workflow_contract(tmp_path):
    """Load-bearing contract strings survive in the default (worktree) prompt (BEP 15 §3.4)."""
    ws = Workspace(tmp_path, tmp_path / ".bos", {})
    ws.bootstrap_platform()
    prompt = AgentRegistry.get_defaults(BOS_CONFIG_AGENT_NAME)["system_prompt"]
    assert "llm-full.md" in prompt                          # grounding step
    assert "bos-config/" in prompt                          # worktree branch prefix
    assert "uv run boscli doctor" in prompt                 # static validation gate
    assert 'uv run boscli ask "say hello to me"' in prompt  # smoke-turn gate
    assert "NEVER run" in prompt                            # restart prohibition
    assert "uv run boscli gateway restart" in prompt        # ...told to the user instead


def test_bos_config_toml_override_merges(tmp_path):
    """[agents.bos_config] composes over the factory spec (existing merge chain)."""
    config = {"agents": {"bos_config": {"model": "openai/gpt-4o"}}}
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    defaults = AgentRegistry.get_defaults(BOS_CONFIG_AGENT_NAME)
    assert defaults["model"] == "openai/gpt-4o"             # TOML wins
    assert "boscli doctor" in defaults["system_prompt"]     # factory term survives


def test_bos_config_parentable(tmp_path):
    """_parent = "bos_config" inherits the factory spec (regression vs #69)."""
    config = {"agents": {"my_config": {"_parent": "bos_config", "model": "openai/gpt-4o"}}}
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    child = AgentRegistry.get_defaults("my_config")
    assert child["model"] == "openai/gpt-4o"
    assert "boscli doctor" in child["system_prompt"]
    assert "_parent" not in child


def test_bos_config_workflow_in_place(tmp_path):
    """[exts.ep_agent.bos_config] workflow="in_place" swaps the isolation section (BEP 15 §3.6)."""
    config = {"exts": {"ep_agent": {"bos_config": {"workflow": "in_place"}}}}
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    prompt = AgentRegistry.get_defaults(BOS_CONFIG_AGENT_NAME)["system_prompt"]
    assert "worktree" not in prompt                          # no worktree steps
    assert ".bak." in prompt                                 # timestamped backups
    assert "uv run boscli doctor" in prompt                  # validation gates still apply
    assert 'uv run boscli ask "say hello to me"' in prompt
    assert "NEVER run" in prompt                             # stop-before-restart still applies
    assert "merge" not in prompt.lower()                     # no merge-back semantics leak in
    # The routing description is the contract shown to delegating agents; it must
    # describe the in_place workflow, not promise worktree isolation or a merge.
    description = AgentRegistry.describe()[BOS_CONFIG_AGENT_NAME]
    assert "ALWAYS delegate" in description                  # routing rule survives
    assert "worktree" not in description
    assert "merg" not in description.lower()                 # no "merge"/"merging"
    assert "backup" in description.lower() or "backups" in description.lower()


def test_bos_config_invalid_workflow_raises(tmp_path):
    """An unknown workflow value fails bootstrap loudly, naming the key."""
    config = {"exts": {"ep_agent": {"bos_config": {"workflow": "yolo"}}}}
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    with pytest.raises(ValueError, match="workflow"):
        ws.bootstrap_platform()


@pytest.mark.asyncio
async def test_bos_agent_can_delegate_to_bos_config_by_default(tmp_path):
    """Regression for final-review Fix 1: the BOS agent's default
    ``plugin-bindings.SubagentPlugin.enabled = ["*"]`` binding is what actually makes
    ``bos_config`` reachable via ``AskSubagent`` — not just registering the kind. Before
    the fix, ``SubagentPlugin``'s own default allow-list is empty, so ``AskSubagent``
    would never even register on the built-in ``BOS`` agent."""
    ws = Workspace(tmp_path, tmp_path / ".bos", {})
    ws.bos_dir.mkdir(parents=True, exist_ok=True)
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = await harness.create_agent("BOS")
        prompt = await agent._build_system_prompt(dummy_turn_context())

    assert agent._tools.has("AskSubagent")
    assert f'<agent role="{BOS_CONFIG_AGENT_NAME}">' in prompt
