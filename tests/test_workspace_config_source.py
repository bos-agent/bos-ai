from bos.config.workspace import Workspace, resolve_config_source


def test_workspace_config_source_loads_from_explicit_file(tmp_path):
    """resolve_config_source + Workspace loads config from the given file."""
    workspace_dir = tmp_path / "project"
    workspace_dir.mkdir()
    config_file = tmp_path / "custom.toml"
    config_file.write_text(
        """
[main]
agent = "custom-agent"

[platform]
extensions = ["bos.exts"]

[[platform.agents]]
name = "custom-agent"
system_prompt = "Custom prompt"
tools = []
skills = []
subagents = []
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
    # No .bos/ directory, no bos.toml — workspace has no config of its own
    config_file = tmp_path / "standalone.toml"
    config_file.write_text(
        """
[platform]
extensions = ["bos.exts"]

[harness.consolidator]
model = "test/consolidator"
""".strip(),
        encoding="utf-8",
    )

    _config_path, bos_dir, config = resolve_config_source(str(config_file))
    ws = Workspace(str(workspace_dir), bos_dir, config)
    # Should not raise WorkspaceResolutionError
    assert ws.workspace == workspace_dir.resolve()
    assert ws.bos_dir == config_file.resolve().parent
