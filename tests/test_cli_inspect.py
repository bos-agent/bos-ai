"""``boscli inspect`` CLI tests."""

import json

import pytest
from click.testing import CliRunner

from bos.cli.entry import cli
from bos.core import AgentRegistry


@pytest.fixture(autouse=True)
def _isolate_agent_registry():
    """``inspect`` runs ``bootstrap_platform`` which registers agents into the
    process-global ``AgentRegistry``. Snapshot and restore it so registrations
    do not leak into other tests (e.g. subagent/prompt-cap counts)."""
    snapshot = dict(AgentRegistry._registry)
    try:
        yield
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(snapshot)


def _invoke(args, **kwargs):
    return CliRunner().invoke(cli, args, **kwargs)


def _init_project(workspace, *extra_args):
    result = _invoke(["init", str(workspace), "--yes", "--no-git", *extra_args])
    assert result.exit_code == 0, result.output
    return result


def test_inspect_text_reports_harness_and_capabilities(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect"])

    assert result.exit_code == 0, result.output
    assert "Harness" in result.output
    assert "JsonlChatStore" in result.output  # selected chat_store impl
    assert "Gateway" in result.output
    assert "not running" in result.output
    assert "Agents" in result.output
    assert "Plugins" in result.output
    assert "SkillsPlugin" in result.output
    assert "Extension points" in result.output


def test_inspect_text_surfaces_capabilities_error(tmp_path, monkeypatch):
    """A bootstrap failure must be visible in text mode, not render as empty sections."""
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    config = tmp_path / ".bos" / "config.toml"
    config.write_text(
        config.read_text() + '\n[agents.broken]\n_parent = "ghost"\n',
        encoding="utf-8",
    )

    result = _invoke(["inspect"])

    assert result.exit_code == 0, result.output
    assert "ghost" in result.output  # the bootstrap error is shown, not swallowed


def test_inspect_json_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert set(report) >= {"harness", "gateway", "default_models", "actors", "capabilities", "extension_points"}

    harness = report["harness"]
    assert harness["source"] == "project"
    assert harness["is_preset"] is False
    assert harness["implementations"]["chat_store"] == "JsonlChatStore"

    assert report["gateway"]["running"] is False
    caps = report["capabilities"]
    assert "main" in caps["agents"]
    assert "SkillsPlugin" in caps["plugins"]
    assert "ReadFile" in caps["tools"]
    # The default scaffold ships built-in skills.
    assert caps["skills"]


def test_inspect_preset_flagged(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["-c", "default", "inspect", "--json"])

    assert result.exit_code == 0, result.output
    harness = json.loads(result.output)["harness"]
    assert harness["is_preset"] is True
    assert harness["source"] == "preset"
    assert harness["preset_name"] == "default"


def test_inspect_agent_reports_resolved_capabilities(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect", "--json", "agent", "main"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    # Agent mode replaces the global capability/extension-point sections.
    assert "agent" in report
    assert "capabilities" not in report
    agent = report["agent"]
    assert agent["kind"] == "main"
    assert "SkillsPlugin" in agent["plugins"]
    # The resolved tool set includes plugin-contributed tools (e.g. LoadSkill),
    # not just the globally registered ones.
    assert "LoadSkill" in agent["tools"]
    assert "ReadFile" in agent["tools"]
    assert isinstance(agent["skills"], dict)


def test_inspect_agent_unknown_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect", "agent", "does-not-exist"])

    assert result.exit_code != 0
    assert "not a registered agent kind" in result.output


def test_inspect_actor_reports_agent_and_capabilities(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect", "--json", "actor", "main"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    # Actor mode reuses the agent report, scoped to the actor's agent.
    assert "agent" in report
    assert "capabilities" not in report
    agent = report["agent"]
    assert agent["kind"] == "main"
    assert agent["actor"] == {"actor": "main", "agent": "main", "display_name": "Main", "is_main": True}
    assert "ReadFile" in agent["tools"]


def test_inspect_actor_applies_per_actor_agent_cfg_override(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    # A worker actor that narrows the agent's tools to WebSearch only.
    config = tmp_path / ".bos" / "config.toml"
    config.write_text(
        config.read_text()
        + (
            "\n[runtime.actors.worker]\n"
            'agent = "main"\n'
            'display_name = "Tony"\n'
            "\n[runtime.actors.worker.agent_cfg.tools]\n"
            'enabled = ["WebSearch"]\n'
        ),
        encoding="utf-8",
    )

    result = _invoke(["inspect", "--json", "actor", "worker"])

    assert result.exit_code == 0, result.output
    agent = json.loads(result.output)["agent"]
    assert agent["actor"]["is_main"] is False
    # The per-actor override wins: only WebSearch survives, not the agent's
    # full default tool set.
    assert list(agent["tools"]) == ["WebSearch"]


def test_inspect_actor_unknown_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect", "actor", "does-not-exist"])

    assert result.exit_code != 0
    assert "not a configured actor" in result.output
