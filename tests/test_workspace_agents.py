"""Tests for BEP6 agent resolution: inline [agents.<name>], external files, resolve_agents()."""

from contextlib import contextmanager
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
    """The BOS agent is registered from the built-in ep_agent factory when no TOML override."""
    ws = Workspace(
        tmp_path,
        tmp_path / ".bos",
        {"runtime": {"location": "process", "actors": {"main": {"agent": "BOS"}}}},
    )
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered("BOS")
    defaults = AgentRegistry.get_defaults("BOS")
    assert defaults["kind"] == "BOS"
    assert defaults["tools"] is None  # "*" → None (all)


def test_default_agent_binds_subagent_plugin_to_all(tmp_path):
    """The BOS agent ships an active SubagentPlugin allow-list (final-review Fix 1):
    the plugin's own default is an empty allow-list (no delegation at all), so
    without this binding AskSubagent is never even registered on BOS."""
    ws = Workspace(
        tmp_path,
        tmp_path / ".bos",
        {"runtime": {"location": "process", "actors": {"main": {"agent": "BOS"}}}},
    )
    ws.bootstrap_platform()
    defaults = AgentRegistry.get_defaults("BOS")
    assert defaults["plugin-bindings"]["SubagentPlugin"]["enabled"] == ["*"]


def test_default_agent_can_be_overridden_in_toml(tmp_path):
    """[agents.BOS] in TOML composes over the built-in factory spec.

    Fields set in TOML win; fields it omits (e.g. plugins) are inherited from
    the factory rather than dropped — the [agent.defaults] -> factory ->
    [agents.BOS] resolution chain, same as any other agent.
    """
    config = {
        "agents": {
            "BOS": {
                "system_prompt": "Custom default",
                "tools": {"enabled": ["ReadFile"]},
            }
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    assert AgentRegistry.has_registered("BOS")
    defaults = AgentRegistry.get_defaults("BOS")
    assert defaults["system_prompt"] == "Custom default"
    assert defaults["tools"] == ["ReadFile"]
    # Omitted in TOML → inherited from the built-in factory spec.
    assert "MemoryPlugin" in defaults["plugins"]["enabled"]


def test_resolve_agents_loads_external_toml(tmp_path):
    """resolve_agents() scans agent_dirs and merges external .toml files."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True)
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir()
    (agents_dir / "helper.toml").write_text('system_prompt = "I help."\ntools = { enabled = ["ReadFile"] }\n')

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
    (agents_dir / "assistant.md").write_text(
        dedent("""\
        ---
        model: gpt-4o
        ---
        You are a helpful assistant.
    """)
    )

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


def test_resolve_agents_external_replaces_inline(tmp_path):
    """External file with same name replaces inline agent entirely (not merge)."""
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
    # External file replaces inline — only file fields survive
    assert agent.system_prompt == "From file"
    assert agent.model == "file-model"
    # tools from inline are gone — replaced, not merged
    assert agent.tools.enabled == []


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
        "runtime": {"location": "process", "actors": {"main": {"agent": "main"}}},
    }
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()
    ws.bootstrap_platform()

    assert AgentRegistry.has_registered("main")
    assert AgentRegistry.has_registered("helper")
    assert AgentRegistry.has_registered("BOS")


def test_non_string_system_prompt_rejected_by_pydantic(tmp_path):
    """Pydantic validation rejects non-string system_prompt in agents.<name>."""
    config = {"agents": {"bad": {"system_prompt": 42}}}
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


# ── ep_agent spec factories ─────────────────────────────────────────


@contextmanager
def _agent_factory(name, fn, description=""):
    """Register an ep_agent factory and clean up registry state afterwards."""
    from bos.core import ep_agent

    ep_agent(name=name, description=description)(fn)
    try:
        yield
    finally:
        ep_agent._extensions.pop(name, None)
        AgentRegistry._registry.pop(name, None)


def test_ep_agent_factory_registered(tmp_path):
    """An ep_agent factory's spec is validated, resolved, and registered."""

    def pkg_agent():
        return {"system_prompt": "From factory", "tools": {"enabled": ["ReadFile"]}}

    with _agent_factory("pkg_agent", pkg_agent, description="Packaged agent"):
        ws = Workspace(tmp_path, tmp_path / ".bos", {})
        ws.bootstrap_platform()
        assert AgentRegistry.has_registered("pkg_agent")
        defaults = AgentRegistry.get_defaults("pkg_agent")
        assert defaults["system_prompt"] == "From factory"
        assert defaults["tools"] == ["ReadFile"]
        # Static extension description is exposed without invoking the factory again
        assert AgentRegistry.describe()["pkg_agent"] == "Packaged agent"


def test_ep_agent_factory_receives_exts_params(tmp_path):
    """[exts.ep_agent.<name>] values are passed into the factory as kwargs."""

    def param_agent(region: str = "us"):
        return {"system_prompt": f"region={region}"}

    config = {"exts": {"ep_agent": {"param_agent": {"region": "eu"}}}}
    with _agent_factory("param_agent", param_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", config)
        ws.bootstrap_platform()
        assert AgentRegistry.get_defaults("param_agent")["system_prompt"] == "region=eu"


def test_ep_agent_merge_chain(tmp_path):
    """[agent.defaults] -> factory result -> [agents.<name>] deep-merge order."""

    def chain_agent():
        return {"system_prompt": "From factory", "model": "factory-model"}

    config = {
        "agent": {"defaults": {"model": "gpt-4o", "max_tokens": 1234}},
        "agents": {"chain_agent": {"model": "toml-model"}},
    }
    with _agent_factory("chain_agent", chain_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", config)
        ws.bootstrap_platform()
        defaults = AgentRegistry.get_defaults("chain_agent")
        assert defaults["model"] == "toml-model"  # [agents.<name>] wins
        assert defaults["system_prompt"] == "From factory"  # factory term survives
        assert defaults["max_tokens"] == 1234  # [agent.defaults] base


def test_ep_agent_async_factory(tmp_path):
    """Async factories run via asyncio.run during bootstrap."""

    async def async_agent():
        return {"system_prompt": "async spec"}

    with _agent_factory("async_agent", async_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", {})
        ws.bootstrap_platform()
        assert AgentRegistry.get_defaults("async_agent")["system_prompt"] == "async spec"


def test_ep_agent_factory_invoked_once(tmp_path):
    """A factory is invoked exactly once per bootstrap."""
    calls = []

    def counted_agent():
        calls.append(1)
        return {"system_prompt": "counted"}

    with _agent_factory("counted_agent", counted_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", {})
        ws.bootstrap_platform()
        assert len(calls) == 1


def test_ep_agent_factory_non_dict_raises(tmp_path):
    """A factory returning a non-dict crashes bootstrap with the factory name."""

    def bad_agent():
        return ["not", "a", "spec"]

    with _agent_factory("bad_agent", bad_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", {})
        with pytest.raises(ValueError, match="bad_agent"):
            ws.bootstrap_platform()


def test_ep_agent_factory_invalid_spec_raises(tmp_path):
    """A factory returning a spec that fails AgentConfig validation crashes bootstrap."""

    def invalid_agent():
        return {"max_tokens": "not-an-int"}

    with _agent_factory("invalid_agent", invalid_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", {})
        with pytest.raises(ValueError, match="invalid_agent"):
            ws.bootstrap_platform()


# ── _parent inheritance (BEP 6 addendum) ────────────────────────────


def test_parent_inheritance_basic(tmp_path):
    """A child inherits the parent's fields and overrides only what it sets."""
    config = {
        "agents": {
            "leader": {
                "system_prompt": "You coordinate.",
                "model": "anthropic/claude-opus-4-8",
                "tools": {"enabled": ["ReadFile", "AskSubagent"]},
            },
            "niceleader": {
                "_parent": "leader",
                "system_prompt": "You coordinate, warmly.",
            },
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    nice = AgentRegistry.get_defaults("niceleader")
    assert nice["system_prompt"] == "You coordinate, warmly."  # child override
    assert nice["model"] == "anthropic/claude-opus-4-8"  # inherited
    assert nice["tools"] == ["ReadFile", "AskSubagent"]  # inherited
    # _parent is an inheritance directive, never an Agent kwarg.
    assert "_parent" not in nice
    assert "parent" not in nice


def test_parent_inheritance_list_replace(tmp_path):
    """A child's list (tools.enabled) replaces the parent's, not unions it."""
    config = {
        "agents": {
            "base": {"tools": {"enabled": ["ReadFile", "GrepSearch", "AskSubagent"]}},
            "leaf": {"_parent": "base", "tools": {"enabled": ["ReadFile"]}},
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    assert AgentRegistry.get_defaults("leaf")["tools"] == ["ReadFile"]


def test_parent_inheritance_dict_merge(tmp_path):
    """Dict values (plugin-bindings) merge recursively across inheritance."""
    config = {
        "agents": {
            "base": {
                "plugin-bindings": {"SubagentPlugin": {"enabled": ["writer"], "task_template": "T"}},
            },
            "leaf": {
                "_parent": "base",
                "plugin-bindings": {"SubagentPlugin": {"enabled": ["reviewer"]}},
            },
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    binding = AgentRegistry.get_defaults("leaf")["plugin-bindings"]["SubagentPlugin"]
    assert binding["enabled"] == ["reviewer"]  # child replaces the list
    assert binding["task_template"] == "T"  # but inherits the sibling key


def test_parent_inheritance_multilevel(tmp_path):
    """Inheritance resolves transitively through a chain."""
    config = {
        "agents": {
            "a": {"model": "m-a", "system_prompt": "A"},
            "b": {"_parent": "a", "system_prompt": "B"},
            "c": {"_parent": "b", "max_iterations": 5},
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    c = AgentRegistry.get_defaults("c")
    assert c["model"] == "m-a"  # from a
    assert c["system_prompt"] == "B"  # b overrode a
    assert c["max_iterations"] == 5  # c's own


def test_parent_inheritance_defaults_remain_floor(tmp_path):
    """[agent.defaults] stays the global base under the _parent chain."""
    config = {
        "agent": {"defaults": {"model": "default-model", "max_iterations": 9}},
        "agents": {
            "base": {"system_prompt": "Base"},
            "leaf": {"_parent": "base"},
        },
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    leaf = AgentRegistry.get_defaults("leaf")
    assert leaf["model"] == "default-model"  # from agent.defaults
    assert leaf["max_iterations"] == 9
    assert leaf["system_prompt"] == "Base"  # from parent


def test_parent_inheritance_from_factory_agent(tmp_path):
    """_parent may name an ep_agent factory agent (e.g. the built-in BOS)."""

    def base_agent():
        return {"system_prompt": "From factory", "model": "factory-model"}

    config = {
        "agents": {
            "main": {"_parent": "base_agent", "description": "Main agent"},
        }
    }
    with _agent_factory("base_agent", base_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", config)
        ws.bootstrap_platform()
        main = AgentRegistry.get_defaults("main")
        assert main["system_prompt"] == "From factory"
        assert main["model"] == "factory-model"
        assert AgentRegistry.describe()["main"] == "Main agent"
        assert "_parent" not in main


def test_parent_inheritance_factory_parent_with_toml_override(tmp_path):
    """A child of a factory agent also inherits the parent's [agents.<parent>] overrides."""

    def base_agent():
        return {"system_prompt": "From factory", "model": "factory-model"}

    config = {
        "agents": {
            "base_agent": {"model": "toml-model"},
            "main": {"_parent": "base_agent", "description": "Main agent"},
        }
    }
    with _agent_factory("base_agent", base_agent):
        ws = Workspace(tmp_path, tmp_path / ".bos", config)
        ws.bootstrap_platform()
        main = AgentRegistry.get_defaults("main")
        assert main["system_prompt"] == "From factory"  # factory term survives
        assert main["model"] == "toml-model"  # [agents.base_agent] override wins
        assert AgentRegistry.describe()["main"] == "Main agent"


def test_parent_inheritance_cycle_raises(tmp_path):
    """A _parent cycle raises a clear error at bootstrap."""
    config = {
        "agents": {
            "a": {"_parent": "b"},
            "b": {"_parent": "a"},
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    with pytest.raises(ValueError, match="cycle"):
        ws.bootstrap_platform()


def test_parent_inheritance_unknown_parent_raises(tmp_path):
    """Referencing an undefined parent raises at bootstrap."""
    config = {"agents": {"leaf": {"_parent": "ghost"}}}
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    with pytest.raises(ValueError, match="ghost"):
        ws.bootstrap_platform()


def test_parent_inheritance_sibling_isolation(tmp_path):
    """Two children of one parent don't corrupt each other via shared nested dicts."""
    config = {
        "agents": {
            "base": {"plugin-bindings": {"SubagentPlugin": {"enabled": ["w"], "task_template": "T"}}},
            "x": {"_parent": "base", "plugin-bindings": {"SubagentPlugin": {"enabled": ["x-only"]}}},
            "y": {"_parent": "base"},
        }
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()
    # x's override must not leak into y (which inherits the parent verbatim).
    assert AgentRegistry.get_defaults("x")["plugin-bindings"]["SubagentPlugin"]["enabled"] == ["x-only"]
    assert AgentRegistry.get_defaults("y")["plugin-bindings"]["SubagentPlugin"]["enabled"] == ["w"]


def test_parent_inheritance_from_external_file(tmp_path):
    """An external agent file can use _parent against an inline [agents.*] base."""
    bos_dir = tmp_path / ".bos"
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "leaf.toml").write_text('_parent = "base"\nsystem_prompt = "Leaf."\n')

    config = {
        "platform": {"agent_dirs": ["./agents"]},
        "agents": {"base": {"model": "base-model", "tools": {"enabled": ["ReadFile"]}}},
    }
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()
    ws.bootstrap_platform()
    leaf = AgentRegistry.get_defaults("leaf")
    assert leaf["system_prompt"] == "Leaf."
    assert leaf["model"] == "base-model"  # inherited from inline base
    assert leaf["tools"] == ["ReadFile"]


def test_per_agent_plugins_isolated_when_defaults_declare_plugins(tmp_path):
    """Each agent keeps its own plugins.enabled on top of a shared [agent.defaults.plugins].

    Registration deep-merges into a copy of the defaults; a shallow copy would let
    every agent write through the same nested `plugins` dict, so the last agent
    registered would win for all of them.
    """
    config = {
        "agent": {"defaults": {"plugins": {"enabled": ["*"]}}},
        "agents": {
            "alpha": {"system_prompt": "alpha", "plugins": {"enabled": ["MemoryPlugin", "SubagentPlugin"]}},
            "omega": {"system_prompt": "omega", "plugins": {"enabled": ["SkillsPlugin"]}},
        },
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()

    alpha = AgentRegistry.get_defaults("alpha")["plugins"]
    omega = AgentRegistry.get_defaults("omega")["plugins"]
    assert alpha["enabled"] == ["MemoryPlugin", "SubagentPlugin"]
    assert omega["enabled"] == ["SkillsPlugin"]
    # Distinct objects: aliasing cannot be caught by asserting on one agent alone.
    assert alpha is not omega


def test_per_agent_plugin_bindings_isolated_when_defaults_declare_bindings(tmp_path):
    """plugin-bindings resolve per agent on top of a shared [agent.defaults.plugin-bindings]."""
    config = {
        "agent": {"defaults": {"plugin-bindings": {"MemoryPlugin": {"scope": "base"}}}},
        "agents": {
            "alpha": {"plugin-bindings": {"MemoryPlugin": {"scope": "alpha"}}},
            "omega": {"plugin-bindings": {"MemoryPlugin": {"scope": "omega"}}},
        },
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()

    alpha = AgentRegistry.get_defaults("alpha")["plugin-bindings"]
    omega = AgentRegistry.get_defaults("omega")["plugin-bindings"]
    assert alpha["MemoryPlugin"]["scope"] == "alpha"
    assert omega["MemoryPlugin"]["scope"] == "omega"
    assert alpha is not omega


def test_factory_agent_plugin_config_does_not_leak_into_config_agents(tmp_path):
    """An ep_agent factory's plugin config stays on that agent.

    The built-in BOS factory agent is registered in the same loop; its plugin
    terms must not reach [agents.*] agents through the shared defaults.
    """
    config = {
        "agent": {"defaults": {"plugins": {"enabled": ["*"]}}},
        "agents": {"alpha": {"system_prompt": "alpha", "plugins": {"enabled": ["MemoryPlugin"]}}},
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()

    bos_plugins = AgentRegistry.get_defaults("BOS")["plugins"]
    alpha = AgentRegistry.get_defaults("alpha")
    assert alpha["plugins"]["enabled"] == ["MemoryPlugin"]
    assert alpha["plugins"] is not bos_plugins
    assert "SkillsPlugin" in bos_plugins["enabled"]  # the factory agent keeps its own set
    assert alpha.get("plugin-bindings", {}) == {}  # no bindings inherited from BOS


def test_explicit_empty_enabled_overrides_defaults(tmp_path):
    """`enabled = []` means "none", not "unset" — it must not fall back to the defaults' `*`.

    The conversion to core kwargs used to drop any value equal to its field
    default, so an explicit empty list vanished before the deep merge and the
    agent silently resolved to every plugin and every tool.
    """
    config = {
        "agent": {"defaults": {"plugins": {"enabled": ["*"]}, "tools": {"enabled": ["*"]}}},
        "agents": {"aud": {"system_prompt": "aud", "plugins": {"enabled": []}, "tools": {"enabled": []}}},
    }
    ws = Workspace(tmp_path, tmp_path / ".bos", config)
    ws.bootstrap_platform()

    aud = AgentRegistry.get_defaults("aud")
    assert aud["plugins"]["enabled"] == []
    assert aud["tools"] == []  # not None — None is the "all tools" sentinel


def test_explicit_null_does_not_clobber_an_inherited_value(tmp_path):
    """A bare `model:` is "not configured", not "clear the inherited value".

    Markdown frontmatter renders an empty scalar as None (as does an ep_agent
    factory reading an absent env var), and `_deep_merge` overwrites
    unconditionally — so an explicit null must not survive the dump or it would
    wipe [agent.defaults].model for that agent.
    """
    bos_dir = tmp_path / ".bos"
    agents_dir = bos_dir / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "helper.md").write_text("---\nmodel:\n---\nI help.\n")

    config = {
        "platform": {"agent_dirs": ["./agents"]},
        "agent": {"defaults": {"model": "gpt-4o"}},
    }
    ws = Workspace(tmp_path, bos_dir, config)
    ws.resolve_agents()  # bootstrap_platform does not scan agent_dirs on its own
    ws.bootstrap_platform()

    assert AgentRegistry.get_defaults("helper")["model"] == "gpt-4o"
