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
async def test_the_runtime_is_closed_with_the_harness(tmp_path, fake_runtimes):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("codex")
    assert agent.closed is True


@pytest.mark.asyncio
async def test_a_missing_extra_names_the_extra_to_install(tmp_path, monkeypatch):
    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "codex", "bos_nonexistent_module:CodexAgent")
    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("codex")
    message = str(excinfo.value)
    assert "bos-ai[codex]" in message
    assert "codex" in message


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
