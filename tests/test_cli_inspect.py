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


def test_inspect_json_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["inspect", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert set(report) >= {"harness", "gateway", "runtime", "capabilities", "extension_points"}

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
