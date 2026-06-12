"""Scaffold engine tests (BEP 9): every archetype must produce a config that loads."""

import pytest

from bos.cli.commands.project import _build_context, _fallback_specialists, _specialist_files
from bos.cli.scaffold import ARCHETYPES, scaffold_workspace
from bos.config import Workspace, WorkspaceResolutionError

PURPOSE = "A research assistant that monitors fixed-income markets"


def _scaffold(tmp_path, archetype, *, dotbos=False, model="anthropic/claude-sonnet-4-6"):
    specialists = _fallback_specialists(PURPOSE)
    context = _build_context("my-agent", PURPOSE, archetype, model, dotbos, specialists)
    agent_files = _specialist_files(specialists, PURPOSE) if archetype == "team" else {}
    return scaffold_workspace(
        tmp_path, archetype, context, dotbos=dotbos, env_content="X=1\n", agent_files=agent_files
    )


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_archetype_scaffold_loads_and_resolves_agents(tmp_path, archetype):
    """Each archetype validates against the config schema and its agent specs load."""
    result = _scaffold(tmp_path, archetype)

    assert result.config_file.is_file()
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / "extensions" / "project_tools.py").is_file()
    assert (tmp_path / "skills" / "example-skill" / "SKILL.md").is_file()

    ws = Workspace.from_discovery(tmp_path)
    ws.resolve_agents()
    assert "main" in ws.config.agents
    assert ws.config.runtime.actors["main"].agent == "main"


def test_team_scaffold_wires_specialists(tmp_path):
    _scaffold(tmp_path, "team")

    ws = Workspace.from_discovery(tmp_path)
    ws.resolve_agents()
    assert {"main", "researcher", "writer"} <= set(ws.config.agents)
    assert "researcher" in ws.config.runtime.actors
    bindings = ws.config.agents["main"].plugin_bindings.root
    assert bindings["SubagentPlugin"]["enabled"] == ["researcher", "writer"]


def test_telegram_scaffold_declares_channel(tmp_path):
    _scaffold(tmp_path, "telegram-bot")

    ws = Workspace.from_discovery(tmp_path)
    channels = ws.config.runtime.channels
    assert len(channels) == 1
    assert channels[0].type == "TelegramChannel"
    assert channels[0].settings["token_env"] == "TELEGRAM_BOT_TOKEN"


def test_service_scaffold_requires_api_key_env(tmp_path):
    _scaffold(tmp_path, "service")

    ws = Workspace.from_discovery(tmp_path)
    assert ws.config.runtime.gateway.api_key_env == "BOS_GATEWAY_API_KEY"


def test_scaffold_dotbos_layout(tmp_path):
    result = _scaffold(tmp_path, "assistant", dotbos=True)

    assert result.config_file == tmp_path / ".bos" / "config.toml"
    assert (tmp_path / ".bos" / "extensions" / "project_tools.py").is_file()
    assert (tmp_path / "README.md").is_file()  # user-facing files stay at the root
    ws = Workspace.from_discovery(tmp_path)
    ws.resolve_agents()
    assert "main" in ws.config.agents


def test_scaffold_skipped_model_keeps_config_valid(tmp_path):
    _scaffold(tmp_path, "assistant", model=None)

    ws = Workspace.from_discovery(tmp_path)
    assert getattr(ws.config.agent.defaults, "model", None) is None


def test_scaffold_rejects_initialized_workspace(tmp_path):
    (tmp_path / "bos.toml").write_text("", encoding="utf-8")

    with pytest.raises(WorkspaceResolutionError, match="already initialized"):
        _scaffold(tmp_path, "assistant")


def test_scaffold_sanitizes_hostile_purpose(tmp_path):
    """Purpose text with TOML-hostile content must not break the rendered config."""
    hostile = 'monitor "spreads" \\ and """quotes"""\nover multiple lines'
    specialists = _fallback_specialists(hostile)
    context = _build_context("my-agent", hostile, "assistant", None, False, specialists)
    scaffold_workspace(tmp_path, "assistant", context)

    ws = Workspace.from_discovery(tmp_path)
    ws.resolve_agents()
    assert "main" in ws.config.agents
