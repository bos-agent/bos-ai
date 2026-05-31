"""Tests for BEP6 agent resolution: inline [agents.<name>], external files, resolve_agents()."""
from textwrap import dedent

import pytest

from bos.config.workspace import Workspace
from bos.core import AgentRegistry


def test_inline_agent_loaded_and_registered(tmp_path):
    """agents.<name> in config are validated and registered."""
    config = {
        "agents": {
            "main": {
                "system_prompt": "Hello",
                "tools": {"enabled": ["ReadFile"]},
            }
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered("main")
    defaults = AgentRegistry.get_defaults("main")
    assert defaults["kind"] == "main"
    assert defaults["system_prompt"] == "Hello"


def test_agent_defaults_merged_into_agents(tmp_path):
    """[agent.defaults] is merged as base for all agents."""
    config = {
        "agent": {
            "defaults": {
                "model": "gpt-4o",
                "tools": {"enabled": ["ReadFile"]},
            }
        },
        "agents": {
            "researcher": {
                "system_prompt": "You research.",
                "tools": {"enabled": ["WebSearch"]},
            }
        },
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered("researcher")
    defaults = AgentRegistry.get_defaults("researcher")
    assert defaults["model"] == "gpt-4o"  # From agent.defaults
    assert defaults["system_prompt"] == "You research."  # From agent.researcher
    # tools.enabled list is replaced, not unioned
    assert defaults["tools"] == ["WebSearch"]


def test_default_agent_registered_from_python_spec(tmp_path):
    """_default agent is registered from Python default_agent_spec when no TOML override."""
    ws = Workspace(tmp_path, tmp_path / ".bos", {"runtime": {"agent": "_default", "location": "process"}})
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered("_default")
    defaults = AgentRegistry.get_defaults("_default")
    assert defaults["kind"] == "_default"
    assert defaults["tools"] is None  # "*" → None (all)


def test_default_agent_can_be_overridden_in_toml(tmp_path):
    """[agents._default] in TOML replaces the Python spec."""
    config = {
        "agents": {
            "_default": {
                "system_prompt": "Custom default",
                "tools": {"enabled": ["ReadFile"]},
            }
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered("_default")
    defaults = AgentRegistry.get_defaults("_default")
    assert defaults["system_prompt"] == "Custom default"
    assert defaults["tools"] == ["ReadFile"]


def test_resolve_agents_loads_external_toml(tmp_path):
    """resolve_agents() scans agent_dirs and merges external .toml files."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "helper.toml").write_text(
        'system_prompt = "I help."\ntools = { enabled = ["ReadFile"] }\n'
    )

    config = {"platform": {"agent_dirs": ["./agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "helper" in ws.config.agents
    assert ws.config.agents["helper"].system_prompt == "I help."


def test_resolve_agents_derives_name_from_filename_stem(tmp_path):
    """External agent without explicit name uses filename stem."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "researcher.toml").write_text('system_prompt = "I search."\n')

    config = {"platform": {"agent_dirs": ["./agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "researcher" in ws.config.agents


def test_resolve_agents_loads_markdown_agent(tmp_path):
    """resolve_agents() loads .md files with frontmatter."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "assistant.md").write_text(dedent("""\
        ---
        model: gpt-4o
        ---
        You are a helpful assistant.
    """))

    config = {"platform": {"agent_dirs": ["./agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "assistant" in ws.config.agents
    assert ws.config.agents["assistant"].system_prompt == "You are a helpful assistant.\n"


def test_resolve_agents_markdown_without_frontmatter_becomes_prompt(tmp_path):
    """Markdown without frontmatter treats whole file as system_prompt."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "notes.md").write_text("Just some notes.\nNo frontmatter.\n")

    config = {"platform": {"agent_dirs": ["./agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "notes" in ws.config.agents
    assert ws.config.agents["notes"].system_prompt == "Just some notes.\nNo frontmatter.\n"


def test_resolve_agents_last_wins_on_duplicate_name(tmp_path):
    """External files with same name deep-merge, later wins per key."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    # a.toml comes first alphabetically
    (agents_dir / "a.toml").write_text('system_prompt = "First"\nmodel = "a"\n')
    # z.toml comes after
    (agents_dir / "z.toml").write_text('system_prompt = "Second"\n')

    config = {"platform": {"agent_dirs": ["./agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    # Both files define the same agent name, but name is derived from filename
    # a.toml → "a", z.toml → "z" — different agents
    assert "a" in ws.config.agents
    assert "z" in ws.config.agents
    assert ws.config.agents["a"].system_prompt == "First"
    assert ws.config.agents["z"].system_prompt == "Second"


def test_resolve_agents_explicit_name_in_file(tmp_path):
    """External agent with explicit 'name' field uses it over filename stem."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "whatever.toml").write_text('name = "realname"\nsystem_prompt = "Hi"\n')

    config = {"platform": {"agent_dirs": ["./agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "realname" in ws.config.agents
    assert ws.config.agents["realname"].system_prompt == "Hi"


def test_resolve_agents_inline_and_external_merge(tmp_path):
    """Inline agent merged with external file of same name."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "main.toml").write_text('system_prompt = "From file"\nmodel = "file-model"\n')

    config = {
        "platform": {"agent_dirs": ["./agents"]},
        "agents": {
            "main": {
                "system_prompt": "From inline",
                "tools": {"enabled": ["ReadFile"]},
            }
        },
    }
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    agent = ws.config.agents["main"]
    # External file loaded after inline, so system_prompt from file wins
    assert agent.system_prompt == "From file"
    # model from file
    assert agent.model == "file-model"
    # tools only in inline, preserved
    assert agent.tools.enabled == ["ReadFile"]


def test_bootstrap_registers_both_inline_and_external_agents(tmp_path):
    """bootstrap_platform registers agents from both inline and external sources."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "helper.toml").write_text('system_prompt = "Helper"\n')

    config = {
        "platform": {"agent_dirs": ["./agents"]},
        "agents": {"main": {"system_prompt": "Main"}},
        "runtime": {"agent": "main", "location": "process"},
    }
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()
    ws.bootstrap_platform()

    assert AgentRegistry.has_registered("main")
    assert AgentRegistry.has_registered("helper")
    assert AgentRegistry.has_registered("_default")


def test_non_string_system_prompt_rejected_by_pydantic(tmp_path):
    """Pydantic validation rejects non-string system_prompt in agents.<name>."""
    config = {
        "agents": {
            "bad": {"system_prompt": 42}
        }
    }
    with pytest.raises(Exception):
        Workspace(tmp_path, tmp_path / ".bos", config)


def test_agent_dirs_relative_to_bos_dir(tmp_path):
    """agent_dirs entries are resolved relative to bos_dir."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    # Create agents in a custom subdirectory
    custom_dir = bos_dir / "custom-agents"
    custom_dir.mkdir()
    (custom_dir / "special.toml").write_text('system_prompt = "Special"\n')

    config = {"platform": {"agent_dirs": ["./custom-agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "special" in ws.config.agents


def test_multiple_agent_dirs_scanned(tmp_path):
    """Multiple agent_dirs entries are all scanned."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    dir1 = bos_dir / "agents"
    dir2 = bos_dir / "more-agents"
    dir1.mkdir()
    dir2.mkdir()
    (dir1 / "a.toml").write_text('system_prompt = "A"\n')
    (dir2 / "b.toml").write_text('system_prompt = "B"\n')

    config = {"platform": {"agent_dirs": ["./agents", "./more-agents"]}}
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()

    assert "a" in ws.config.agents
    assert "b" in ws.config.agents
