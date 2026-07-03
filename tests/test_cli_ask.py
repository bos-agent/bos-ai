"""Tests for boscli ask (in-process agent execution)."""

import asyncio

from click.testing import CliRunner

from bos.cli.commands.agent import _TaskProgressDisplay, ask
from bos.core.contract import AgentResult


class _StubAgent:
    last_llm_args = None
    last_kind = None
    last_agent_cfg = None
    last_event_sink = None

    async def run(self, chat_id, content, *, event_sink=None, llm_args=None, **kw):
        type(self).last_llm_args = llm_args
        type(self).last_event_sink = event_sink
        return AgentResult(
            output=f"echo: {content}",
            structured=False,
            iterations=1,
            usage={},
            turn_id="t",
            finish_reason="stop",
        )


def _project(tmp_path, monkeypatch):
    """Write a minimal project config with one actor and chdir in."""
    (tmp_path / ".bos").mkdir()
    (tmp_path / ".bos" / "config.toml").write_text(
        '[runtime]\nmain_actor = "main"\n[runtime.actors.main]\nagent = "react"\n'
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _patch_harness(monkeypatch):
    """Patch AgentHarness to use _StubAgent without a real LLM call."""
    _StubAgent.last_llm_args = None
    _StubAgent.last_kind = None
    _StubAgent.last_agent_cfg = None
    _StubAgent.last_event_sink = None

    async def _fake_create_agent(self, kind=None, agent_cfg=None):
        _StubAgent.last_kind = kind
        _StubAgent.last_agent_cfg = agent_cfg
        return _StubAgent()

    monkeypatch.setattr("bos.core.harness.AgentHarness.create_agent", _fake_create_agent)
    monkeypatch.setattr(
        "bos.core.harness.AgentHarness.__aenter__",
        lambda self: asyncio.sleep(0, result=self),
    )
    monkeypatch.setattr(
        "bos.core.harness.AgentHarness.__aexit__",
        lambda self, *a: asyncio.sleep(0, result=False),
    )


def test_ask_runs_in_process_and_prints_reply(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_harness(monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["hello"], obj={})
    assert result.exit_code == 0, result.output
    assert "echo: hello" in result.output
    # Default path: the main actor ("main") locates its agent kind ("react"),
    # passing the actor's agent_cfg.
    assert _StubAgent.last_kind == "react"
    assert isinstance(_StubAgent.last_agent_cfg, dict)


def test_ask_agent_flag_selects_agent_kind(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_harness(monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)
    # --agent names an agent kind directly (validated against the registry),
    # bypassing actor lookup and its agent_cfg.
    monkeypatch.setattr("bos.core.AgentRegistry.has_registered", staticmethod(lambda kind: True))

    result = CliRunner().invoke(ask, ["--agent", "researcher", "hi"], obj={})
    assert result.exit_code == 0, result.output
    assert "echo: hi" in result.output
    assert _StubAgent.last_kind == "researcher"
    assert _StubAgent.last_agent_cfg is None


def test_ask_model_flag_overrides(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_harness(monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["--model", "openai/gpt-4o", "hi"], obj={})
    assert result.exit_code == 0, result.output
    assert _StubAgent.last_llm_args == {"model": "openai/gpt-4o"}


def test_ask_attaches_progress_display_by_default(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_harness(monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["hello"], obj={})
    assert result.exit_code == 0, result.output
    assert isinstance(_StubAgent.last_event_sink, _TaskProgressDisplay)


def test_ask_no_steps_prints_only_final_output(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_harness(monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["--no-steps", "hello"], obj={})
    assert result.exit_code == 0, result.output
    assert "echo: hello" in result.output
    # No progress display is attached, so no step lines can ever be emitted.
    assert _StubAgent.last_event_sink is None


def test_ask_unknown_agent_errors(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["--agent", "nope", "hi"], obj={})
    assert result.exit_code != 0
    assert "nope" in result.output
