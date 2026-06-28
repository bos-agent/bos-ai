"""Tests for boscli ask (in-process agent execution)."""

import asyncio

from click.testing import CliRunner

from bos.cli.commands.agent import ask
from bos.core.contract import AgentResult


class _StubAgent:
    last_llm_args = None

    async def run(self, chat_id, content, *, event_sink=None, llm_args=None, **kw):
        type(self).last_llm_args = llm_args
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

    async def _fake_create_agent(self, kind=None, agent_cfg=None):
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


def test_ask_model_flag_overrides(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    _patch_harness(monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["--model", "openai/gpt-4o", "hi"], obj={})
    assert result.exit_code == 0, result.output
    assert _StubAgent.last_llm_args == {"model": "openai/gpt-4o"}


def test_ask_unknown_agent_errors(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    monkeypatch.setattr("bos.config.workspace.Workspace.resolve_agents", lambda self: None)
    monkeypatch.setattr("bos.config.workspace.Workspace.bootstrap_platform", lambda self: None)

    result = CliRunner().invoke(ask, ["--agent", "nope", "hi"], obj={})
    assert result.exit_code != 0
    assert "nope" in result.output
