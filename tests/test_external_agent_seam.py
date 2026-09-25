"""BEP 19 §3.2–§3.4: the external-runtime seam."""

from __future__ import annotations

import pytest
from conftest import _FakeRuntime, create_test_agent


def test_agent_satisfies_agent_port():
    from bos.core.agent import AgentPort

    assert isinstance(create_test_agent(), AgentPort)


def test_agent_port_accepts_a_minimal_duck_type():
    from bos.core.agent import AgentPort, AgentResult

    class Minimal:
        @property
        def name(self) -> str:
            return "minimal"

        def request_stop(self) -> None:
            pass

        async def ask(self, chat_id, content, **kwargs) -> str:
            return "ok"

        async def run(self, chat_id, content, **kwargs) -> AgentResult:
            return AgentResult(output="ok")

    assert isinstance(Minimal(), AgentPort)


def test_agent_port_rejects_an_object_missing_run():
    from bos.core.agent import AgentPort

    class NoRun:
        @property
        def name(self) -> str:
            return "x"

        def request_stop(self) -> None:
            pass

        async def ask(self, chat_id, content, **kwargs) -> str:
            return ""

    assert not isinstance(NoRun(), AgentPort)


@pytest.mark.asyncio
async def test_a_reserved_kind_builds_the_runtime_not_an_agent(tmp_path, fake_runtimes):
    from bos.core.agent import Agent, AgentPort
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("codex", agent_cfg={"permission": "read-only"})
        assert isinstance(agent, _FakeRuntime)
        assert not isinstance(agent, Agent)
        # AgentPort is the whole point of the seam (BEP 19 §3.3): create_agent's
        # dispatch must return something a host can actually use through it, not
        # merely something that happens to be a _FakeRuntime and not an Agent.
        assert isinstance(agent, AgentPort)
        assert agent.name == "codex"


@pytest.mark.asyncio
async def test_an_external_runtime_key_dispatches_and_is_not_passed_on(tmp_path, fake_runtimes):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("george", agent_cfg={"external_runtime": "codex"})
        assert isinstance(agent, _FakeRuntime)
        assert agent.name == "george", "kind stays the agent's own name, BEP 19 §3.2"
        assert agent.cfg["external_runtime"] == "codex", "read, not consumed: inspect reports it"


@pytest.mark.asyncio
async def test_a_reserved_parent_in_agent_cfg_dispatches_like_one_in_config(tmp_path, fake_runtimes):
    """`agent_cfg` bypasses the workspace resolver, so a `_parent` arriving here
    used to be dropped without a word — `_apply` filters it out of `Agent`'s
    kwargs — and a caller asking for a Codex agent got a BOS one, its
    `permission` ignored."""
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("martha", agent_cfg={"_parent": "codex", "permission": "read-only"})
        assert isinstance(agent, _FakeRuntime)
        assert agent.name == "martha"
        assert agent.cfg["external_runtime"] == "codex"
        assert agent.cfg["permission"] == "read-only"
        assert "_parent" not in agent.cfg, "an inheritance directive, not runtime config"


@pytest.mark.asyncio
async def test_a_reserved_parents_own_config_reaches_an_agent_cfg_child(tmp_path, fake_runtimes):
    """Same inheritance as `test_a_reserved_parents_own_config_reaches_the_child`,
    through `agent_cfg` instead of a config table."""
    ws = _write_workspace(tmp_path, '[agents.codex]\ncwd = "services"\npermission = "read-only"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = await harness.create_agent(
            "martha", agent_cfg={"_parent": "codex", "permission": "workspace-write"}
        )
    assert agent.cfg["cwd"] == "services", "inherited from [agents.codex]"
    assert agent.cfg["permission"] == "workspace-write", "agent_cfg wins"


@pytest.mark.asyncio
async def test_a_null_parent_in_agent_cfg_is_no_parent(tmp_path, fake_runtimes):
    from bos.core.agent import Agent
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent(agent_cfg={"_parent": None, "system_prompt": "hi", "tools": []})
        assert isinstance(agent, Agent)


@pytest.mark.asyncio
async def test_a_registered_bos_parent_in_agent_cfg_is_inherited(tmp_path, fake_runtimes):
    """A registered agent's defaults already hold its resolved chain, so a BOS
    parent resolves through agent_cfg the way it does in config."""
    from bos.core.agent import Agent

    ws = _write_workspace(tmp_path, '[agents.solo]\nsystem_prompt = "solo prompt"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = await harness.create_agent("solo2", agent_cfg={"_parent": "solo"})
        assert isinstance(agent, Agent)
        assert agent._system_prompt == "solo prompt"


@pytest.mark.asyncio
async def test_a_registered_runtime_instance_is_a_parent_in_agent_cfg(tmp_path, fake_runtimes):
    """A variant of a named runtime instance, e.g. the same agent in another cwd."""
    ws = _write_workspace(
        tmp_path, '[agents.george]\n_parent = "codex"\npermission = "read-only"\ncwd = "a"\n'
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = await harness.create_agent("george2", agent_cfg={"_parent": "george", "cwd": "b"})
    assert isinstance(agent, _FakeRuntime)
    assert agent.name == "george2"
    assert agent.cfg["external_runtime"] == "codex"
    assert agent.cfg["permission"] == "read-only", "inherited from george"
    assert agent.cfg["cwd"] == "b", "agent_cfg wins"


@pytest.mark.asyncio
async def test_bos_variants_bind_plugins_under_their_own_names(tmp_path, fake_runtimes, monkeypatch):
    """Per-agent plugin state — MemoryPlugin's store — is keyed by
    `agent_name or kind or "default"`. Two variants of one parent must not both
    bind as "default" and share it, which is what dropping the parent's `kind`
    without writing the child's did."""
    from bos.core.harness import AgentHarness

    ws = _write_workspace(tmp_path, '[agents.solo]\nsystem_prompt = "hi"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()
    identities: list[str] = []
    original = AgentHarness._bind_plugins_for_agent

    async def spy(self, agent_cfg):
        identities.append(agent_cfg.get("agent_name") or agent_cfg.get("kind") or "default")
        return await original(self, agent_cfg)

    monkeypatch.setattr(AgentHarness, "_bind_plugins_for_agent", spy)
    async with ws.harness() as harness:
        await harness.create_agent("solo2", agent_cfg={"_parent": "solo"})
        await harness.create_agent("solo3", agent_cfg={"_parent": "solo"})
    assert identities == ["solo2", "solo3"]


@pytest.mark.asyncio
async def test_an_agent_cfg_child_does_not_inherit_agent_name(tmp_path, fake_runtimes):
    """Same rule as a config child: the parent's `agent_name` stays the parent's."""
    ws = _write_workspace(tmp_path, '[agents.solo]\nsystem_prompt = "hi"\nagent_name = "Solo"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = await harness.create_agent("solo2", agent_cfg={"_parent": "solo"})
        own = await harness.create_agent("solo3", agent_cfg={"_parent": "solo", "agent_name": "Third"})
    assert agent.name == "solo2"
    assert own.name == "Third", "a child may still name itself"


@pytest.mark.asyncio
async def test_resolving_an_agent_cfg_parent_writes_through_to_nothing(tmp_path, fake_runtimes):
    """`_deep_merge` mutates its base in place: neither the parent's registry
    entry nor the caller's dict may change. Popping `_parent` from the caller's
    own dict would make its next reuse build a plain Agent again."""
    import copy

    from bos.core import AgentRegistry

    ws = _write_workspace(
        tmp_path,
        '[agents.codex]\npermission = "read-only"\n\n[agents.codex.native_options.config]\ny = 2\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()
    registry_before = copy.deepcopy(AgentRegistry.get_defaults("codex"))
    agent_cfg = {"_parent": "codex", "native_options": {"config": {"x": 1}}}
    caller_before = copy.deepcopy(agent_cfg)

    async with ws.harness() as harness:
        agent = await harness.create_agent("martha", agent_cfg=agent_cfg)
    assert agent.cfg["native_options"]["config"] == {"x": 1, "y": 2}
    assert AgentRegistry.get_defaults("codex") == registry_before
    assert agent_cfg == caller_before
    assert agent.cfg["kind"] == "martha", "the child's name, not the parent's"


@pytest.mark.asyncio
async def test_a_null_parent_is_stripped_on_the_runtime_path(tmp_path, fake_runtimes):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("codex", agent_cfg={"_parent": None, "permission": "read-only"})
    assert "_parent" not in agent.cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["no-such-parent", ["codex"]])
async def test_an_unknown_parent_in_agent_cfg_is_refused_not_dropped(tmp_path, fake_runtimes, parent):
    ws = _write_workspace(tmp_path, '[agents.solo]\nsystem_prompt = "hi"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        with pytest.raises(ValueError) as excinfo:
            await harness.create_agent("martha", agent_cfg={"_parent": parent, "system_prompt": "hi"})
    message = str(excinfo.value)
    assert repr(parent) in message
    assert "solo" in message and "codex" in message, "lists what it could have been"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "agent_cfg", "expected"),
    [
        ("martha", {"_parent": "codex", "external_runtime": "claude-code"}, "external_runtime = 'claude-code'"),
        # An explicit None used to slip past the check and build a plain Agent
        # with `permission` dropped — the defect this whole path exists to stop.
        ("martha", {"_parent": "codex", "external_runtime": None}, "external_runtime = None"),
        ("claude-code", {"_parent": "codex"}, "is the 'claude-code' runtime"),
    ],
)
async def test_a_reserved_parent_that_contradicts_the_runtime_is_refused(
    tmp_path, fake_runtimes, kind, agent_cfg, expected
):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(ValueError, match=expected):
            await harness.create_agent(kind, agent_cfg={**agent_cfg, "permission": "read-only"})


@pytest.mark.asyncio
async def test_agent_cfg_cannot_reparent_a_configured_agent(tmp_path, fake_runtimes):
    ws = _write_workspace(tmp_path, '[agents.plain]\nsystem_prompt = "hi"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        with pytest.raises(ValueError) as excinfo:
            await harness.create_agent("plain", agent_cfg={"_parent": "codex", "permission": "read-only"})
    assert "'plain'" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_runtime_is_closed_with_the_harness(tmp_path, fake_runtimes):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("codex")
    assert agent.closed is True


@pytest.mark.asyncio
async def test_a_missing_extra_names_the_extra_to_install(tmp_path, monkeypatch):
    """A failure inside the vendor package's own namespace is still the vendor's
    problem to install (the `missing.startswith(f"{vendor}.")` half of the
    classification) — `openai_codex` is a real dependency in this dev venv, so a
    genuinely absent *submodule* of it, not the package itself, is what's faked
    here. test_a_missing_vendor_module_names_the_extra below covers the other
    half: the package itself missing.
    """
    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    fake_target = "openai_codex.bos_nonexistent_submodule:CodexAgent"
    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "codex", fake_target)
    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("codex")
    message = str(excinfo.value)
    assert "bos-ai[codex]" in message
    assert "codex" in message


@pytest.mark.asyncio
async def test_a_missing_vendor_module_names_the_extra(tmp_path, monkeypatch):
    """The friendly message is for a missing VENDOR module, and only that.

    `bos.extensions.runtimes.codex` doesn't exist yet (Task 3 adds it), so
    there is no real module whose own `import openai_codex` can fail here.
    Standing in with a dotted path that *is* the vendor module makes
    `_load_external_runtime` see the same `exc.name == "openai_codex"` that a
    real codex.py's failed import would produce once that module exists.
    """
    import sys

    from conftest import BlockImport

    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    monkeypatch.setattr(sys, "meta_path", [BlockImport("openai_codex"), *sys.meta_path])
    monkeypatch.delitem(sys.modules, "openai_codex", raising=False)
    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "codex", "openai_codex:CodexAgent")

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("codex", agent_cfg={"permission": "read-only"})
    assert "bos-ai[codex]" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_missing_claude_agent_sdk_names_the_claude_code_extra(tmp_path, monkeypatch):
    """The claude-code half of the test above. The kind points at a throwaway
    module whose first line is ``import claude_agent_sdk`` — the shape of a
    runtime module whose vendor import fails — so the ``ModuleNotFoundError``
    reaches ``_load_external_runtime`` from inside a runtime module, carrying
    ``name == "claude_agent_sdk"``, whether or not the real runtime module exists.
    """
    import sys

    from conftest import BlockImport

    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "bos_t1_claude_runtime_probe.py").write_text("import claude_agent_sdk  # noqa: F401\n")
    monkeypatch.syspath_prepend(str(probe_dir))
    monkeypatch.setattr(sys, "meta_path", [BlockImport("claude_agent_sdk"), *sys.meta_path])
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
    monkeypatch.delitem(sys.modules, "bos_t1_claude_runtime_probe", raising=False)
    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "claude-code", "bos_t1_claude_runtime_probe:ClaudeCodeAgent")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with AgentHarness(bos_dir=workspace, workspace=workspace) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("claude-code", agent_cfg={"permission": "read-only"})
    assert "bos-ai[claude-code]" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_broken_import_inside_the_runtime_module_is_not_relabelled(tmp_path, monkeypatch):
    """A typo'd import inside codex.py must report itself, not 'install the extra'.

    No BlockImport needed: `bos_totally_absent_helper` names no real package
    anywhere, so `importlib.import_module` fails on its own — this is exactly
    the shape of a typo'd `import` statement inside an installed runtime module.
    """
    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "codex", "bos_totally_absent_helper:CodexAgent")

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("codex", agent_cfg={"permission": "read-only"})
    message = str(excinfo.value)
    assert "bos_totally_absent_helper" in message, "the real cause must be visible"
    assert "bos-ai[codex]" not in message, "an unrelated import failure must not be relabelled as the missing extra"


@pytest.mark.asyncio
async def test_a_missing_symbol_in_an_installed_vendor_module_is_not_relabelled(tmp_path, monkeypatch):
    """Fix round 1: a renamed/removed symbol in an *installed* vendor package
    raises a plain ImportError whose `.name` is just the package
    ("openai_codex") — the same `.name` a genuinely-missing package would set.
    Only `ModuleNotFoundError` means "not found"; a plain `ImportError` here
    means the package was found and something inside it wasn't, which must
    report itself and not be relabelled as a missing extra.

    Uses the real, installed `openai_codex` (no BlockImport) via a throwaway
    module on `sys.path` that does what codex.py (Task 3) will do — `from
    openai_codex import <symbol>` — with a symbol that doesn't exist, standing
    in for a vendor rename or version skew.
    """
    import sys

    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "bos_task2_symbol_probe.py").write_text(
        "from openai_codex import _totally_bogus_attr_or_submodule\n"
    )
    monkeypatch.syspath_prepend(str(probe_dir))
    monkeypatch.delitem(sys.modules, "bos_task2_symbol_probe", raising=False)
    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "codex", "bos_task2_symbol_probe:CodexAgent")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with AgentHarness(bos_dir=workspace, workspace=workspace) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("codex", agent_cfg={"permission": "read-only"})
    message = str(excinfo.value)
    assert "_totally_bogus_attr_or_submodule" in message, "the real cause must be visible"
    assert "bos-ai[codex]" not in message, "a symbol missing from an installed package is not a missing extra"


@pytest.mark.asyncio
async def test_a_normal_kind_is_untouched(tmp_path, fake_runtimes):
    from bos.core.agent import Agent
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent(agent_cfg={"system_prompt": "hi", "tools": []})
        assert isinstance(agent, Agent)


def _write_workspace(tmp_path, config: str, agent_files: dict[str, str] | None = None):
    """A Workspace on disk with a config.toml and optional agents/*.md."""
    import tomllib

    from bos.config import Workspace

    (tmp_path / "config.toml").write_text(config)
    if agent_files:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        for name, body in agent_files.items():
            (agents_dir / name).write_text(body)
    return Workspace(
        workspace=tmp_path, bos_dir=tmp_path, config=tomllib.loads(config)
    )


def test_a_child_of_a_reserved_kind_inherits_external_runtime(tmp_path):
    ws = _write_workspace(
        tmp_path,
        '[agents.george]\n_parent = "codex"\npermission = "read-only"\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    defaults = AgentRegistry.get_defaults("george")
    assert defaults["external_runtime"] == "codex"
    assert defaults["permission"] == "read-only"


def test_a_reserved_parents_own_config_reaches_the_child(tmp_path):
    ws = _write_workspace(
        tmp_path,
        '[agents.codex]\ncwd = "services"\npermission = "read-only"\n\n'
        '[agents.george]\n_parent = "codex"\npermission = "workspace-write"\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    defaults = AgentRegistry.get_defaults("george")
    assert defaults["cwd"] == "services", "inherited from [agents.codex]"
    assert defaults["permission"] == "workspace-write", "child wins"
    assert defaults["external_runtime"] == "codex"


def test_no_phantom_reserved_agent_is_registered(tmp_path):
    ws = _write_workspace(tmp_path, '[agents.plain]\nsystem_prompt = "hi"\n')
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    assert "codex" not in AgentRegistry.describe()
    assert "claude-code" not in AgentRegistry.describe()


def test_agent_defaults_do_not_reach_an_externally_backed_agent(tmp_path):
    ws = _write_workspace(
        tmp_path,
        '[agent.defaults]\nmodel = "gpt-4o"\nmax_iterations = 7\n\n'
        '[agents.george]\n_parent = "codex"\n\n'
        '[agents.plain]\nsystem_prompt = "hi"\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    assert "model" not in AgentRegistry.get_defaults("george")
    assert "max_iterations" not in AgentRegistry.get_defaults("george")
    assert AgentRegistry.get_defaults("plain")["model"] == "gpt-4o", "normal agents unaffected"


def test_agent_defaults_do_not_reach_a_bare_reserved_kind(tmp_path):
    ws = _write_workspace(
        tmp_path,
        '[agent.defaults]\nmodel = "gpt-4o"\n\n[agents.codex]\npermission = "read-only"\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    assert "model" not in AgentRegistry.get_defaults("codex")


def test_a_markdown_agent_file_inherits_and_carries_its_body_as_the_prompt(tmp_path):
    ws = _write_workspace(
        tmp_path,
        "",
        {
            "george.md": (
                "---\n_parent: codex\npermission: workspace-write\n---\n"
                "You are George, the implementer.\n"
            )
        },
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    defaults = AgentRegistry.get_defaults("george")
    assert defaults["external_runtime"] == "codex"
    assert defaults["permission"] == "workspace-write"
    assert defaults["system_prompt"].strip() == "You are George, the implementer."


def test_an_empty_markdown_body_is_an_empty_prompt_not_a_crash(tmp_path):
    """Review Focus 5: frontmatter with no body is a legitimate 'inherit all'."""
    ws = _write_workspace(tmp_path, "", {"george.md": "---\n_parent: codex\n---\n"})
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    defaults = AgentRegistry.get_defaults("george")
    assert defaults["system_prompt"] == ""
    assert defaults["external_runtime"] == "codex"


def test_a_hand_written_external_runtime_names_parent(tmp_path):
    """The resolver writes this key; a config that writes it is bypassing _parent.

    It has to be caught here, before inheritance runs — afterwards a
    resolver-written key and a hand-written one are indistinguishable.
    """
    ws = _write_workspace(tmp_path, '[agents.george]\nexternal_runtime = "codex"\n')
    ws.resolve_agents()
    with pytest.raises(ValueError) as excinfo:
        ws.bootstrap_platform()
    message = str(excinfo.value)
    assert "george" in message
    assert "_parent" in message


def test_the_reserved_kind_table_matches_the_harness(tmp_path):
    """Two modules name the same two kinds; drift between them is a silent bug."""
    from bos.config.workspace import _EXTERNAL_RUNTIME_SPECS
    from bos.core.harness import EXTERNAL_AGENT_KINDS

    assert set(_EXTERNAL_RUNTIME_SPECS) == set(EXTERNAL_AGENT_KINDS)


@pytest.mark.asyncio
async def test_inspect_reports_an_external_agent_without_touching_agent_internals(tmp_path, fake_runtimes):
    from bos.cli.commands.inspect import _agent_capabilities

    ws = _write_workspace(
        tmp_path,
        '[agents.george]\n_parent = "codex"\ncwd = "."\npermission = "read-only"\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    info = await _agent_capabilities(ws, "george")

    assert info["name"] == "george"
    assert info["runtime"] == "codex"
    assert info["permission"] == "read-only"
    assert info["plugins"] == []
    assert info["skills"] == {}


@pytest.mark.asyncio
async def test_inspect_reports_a_malformed_mcp_tools_instead_of_crashing(tmp_path, fake_runtimes):
    """Final review, item 2: `mcp_tools = 7` is an ordinary config typo — extra="allow"
    on AgentConfig lets it reach here unchanged, and `sorted(7)` raises an unhandled
    TypeError. `boscli inspect agent` must report the malformed value, not crash."""
    from bos.cli.commands.inspect import _agent_capabilities

    ws = _write_workspace(
        tmp_path,
        '[agents.george]\n_parent = "codex"\ncwd = "."\npermission = "read-only"\nmcp_tools = 7\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    info = await _agent_capabilities(ws, "george")

    assert info["name"] == "george"
    assert info["mcp_tools"] != []
    assert "7" in str(info["mcp_tools"])


@pytest.mark.asyncio
async def test_inspect_text_render_shows_external_fields_and_hides_the_model_hint(tmp_path, fake_runtimes):
    """BEP 19 §4.3: text mode is the default operator view, not `--json`.

    An external agent's report has no "model" — [agent.defaults] is
    deliberately not merged into it — so the renderer must not print the
    BOS_MODEL hint, and must instead show the fields this task added.
    """
    import io

    from rich.console import Console

    from bos.cli.commands.inspect import _agent_capabilities, _render_agent

    ws = _write_workspace(
        tmp_path,
        '[agents.george]\n_parent = "codex"\ncwd = "."\npermission = "read-only"\nmcp_tools = ["search"]\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    info = await _agent_capabilities(ws, "george")

    buffer = io.StringIO()
    _render_agent(Console(file=buffer, width=200), info)
    output = buffer.getvalue()

    assert "codex" in output, "the resolved runtime must be shown"
    assert "read-only" in output, "the permission level must be shown"
    assert "search" in output, "the resolved mcp_tools must be shown"
    assert "BOS_MODEL" not in output, "no such setting exists for an external runtime"


def test_external_runtime_requires_aclose_and_resolved_config():
    """BEP 19 §8.2: a runtime without aclose() is silently skipped by _aclose and
    leaks its child process. The protocol is what makes pyright catch that."""
    from conftest import _FakeRuntime

    from bos.core.agent import ExternalRuntime

    fake = _FakeRuntime(kind="k", cfg={}, chat_store=None, workspace=".", mcp=None, structured_validator=None)
    assert isinstance(fake, ExternalRuntime)


def test_external_runtime_rejects_a_runtime_without_aclose():
    from bos.core.agent import AgentPort, AgentResult, ExternalRuntime

    class NoClose:
        @property
        def name(self) -> str:
            return "x"

        @property
        def resolved_config(self):
            return {}

        def request_stop(self) -> None: ...

        async def ask(self, chat_id, content, **kwargs) -> str:
            return ""

        async def run(self, chat_id, content, **kwargs) -> AgentResult:
            return AgentResult(output="")

    instance = NoClose()
    assert isinstance(instance, AgentPort), "still a valid host-facing agent"
    assert not isinstance(instance, ExternalRuntime), "but not a valid external runtime"


def test_a_parent_in_an_actors_agent_cfg_is_refused_at_load_naming_the_actor():
    """It used to pass validation and be dropped; with agent_cfg now resolving
    `_parent`, an actor's copy would otherwise start resolving (or failing) at
    gateway start. Refuse it where the config is read."""
    from pydantic import ValidationError

    from bos.config import validate_config

    with pytest.raises(ValidationError) as excinfo:
        validate_config({"runtime": {"actors": {"coder": {"agent": "codex", "agent_cfg": {"_parent": "codex"}}}}})
    message = str(excinfo.value)
    assert "coder" in message
    assert "_parent" in message
