"""Tests for resolve_config_source with BEP6 config format."""
from bos.config.workspace import Workspace, resolve_config_source


def test_workspace_config_source_uses_preset_bos_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))

    config_path, bos_dir, config = resolve_config_source("default")
    ws = Workspace(str(tmp_path / "project"), bos_dir, config, config_file=config_path)

    assert ws.bos_dir == (tmp_path / "home" / "presets" / "default").resolve()
    assert ws.bos_dir.is_dir()
    assert ws.get_main_agent_kind() == "_default"


def test_workspace_config_source_loads_from_explicit_file(tmp_path):
    """resolve_config_source + Workspace loads BEP6 config from the given file."""
    workspace_dir = tmp_path / "project"
    workspace_dir.mkdir()
    config_file = tmp_path / "custom.toml"
    config_file.write_text(
        """
[platform]
extensions = ["bos.exts"]

[agents.custom-agent]
system_prompt = "Custom prompt"
tools = { enabled = ["ReadFile"] }

[runtime]
location = "process"
default_actor = "main"

[runtime.actors.main]
agent = "custom-agent"
""".strip(),
        encoding="utf-8",
    )

    _config_path, bos_dir, config = resolve_config_source(str(config_file))
    ws = Workspace(str(workspace_dir), bos_dir, config)
    assert ws.get_main_agent_kind() == "custom-agent"


def test_workspace_config_source_skips_discovery(tmp_path):
    """When config_source is an explicit file, no .bos/config.toml is needed."""
    workspace_dir = tmp_path / "project-no-config"
    workspace_dir.mkdir()
    config_file = tmp_path / "standalone.toml"
    config_file.write_text(
        """
[platform]
extensions = ["bos.exts"]

[exts.ep_consolidator._default]
model = "test/consolidator"
""".strip(),
        encoding="utf-8",
    )

    _config_path, bos_dir, config = resolve_config_source(str(config_file))
    ws = Workspace(str(workspace_dir), bos_dir, config)
    assert ws.workspace == workspace_dir.resolve()
    assert ws.bos_dir == config_file.resolve().parent
