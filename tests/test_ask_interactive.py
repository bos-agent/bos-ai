"""Integration tests for boscli ask wiring."""

import pytest

from bos.config.workspace import WorkspaceResolutionError, resolve_config_source


def test_resolve_config_source_builtin_preset():
    """resolve_config_source with a preset name resolves to the built-in preset."""
    config_path, bos_dir, config = resolve_config_source("default")
    assert config_path.name == "default.toml"
    assert config_path.exists()


def test_resolve_config_source_unknown_name_raises():
    """resolve_config_source with an unknown name raises WorkspaceResolutionError."""
    with pytest.raises(WorkspaceResolutionError, match="Unknown config source"):
        resolve_config_source("nonexistent-config-zzz")


def test_build_workspace_for_ask_workspace_override(tmp_path, monkeypatch):
    """-w/--workspace overrides the workspace directory."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    override_dir = tmp_path / "override"
    override_dir.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    ctx = type("Ctx", (), {"obj": {}})
    ws = _build_workspace_for_ask(ctx, workspace_override=str(override_dir))

    assert ws.workspace == override_dir.resolve()
