from bos.config.workspace import Workspace


def test_workspace_config_source_loads_from_explicit_file(tmp_path):
    """Workspace(config_source=...) loads config from the given file, not discovery."""
    workspace_dir = tmp_path / "project"
    workspace_dir.mkdir()
    config_file = tmp_path / "custom.toml"
    config_file.write_text(
        """
[main]
agent = "custom-agent"

[platform]
extensions = ["bos.extensions.all"]

[[platform.agents]]
name = "custom-agent"
system_prompt = "Custom prompt"
tools = []
skills = []
subagents = []
""".strip(),
        encoding="utf-8",
    )

    ws = Workspace(str(workspace_dir), config_source=str(config_file))
    assert ws.get_main_agent_name() == "custom-agent"


def test_workspace_config_source_skips_discovery(tmp_path):
    """When config_source is set, no .bos/config.toml is needed in the workspace."""
    workspace_dir = tmp_path / "project-no-config"
    workspace_dir.mkdir()
    # No .bos/ directory, no bos.toml — workspace has no config of its own
    config_file = tmp_path / "standalone.toml"
    config_file.write_text(
        """
[platform]
extensions = ["bos.extensions.all"]

[harness.consolidator]
model = "test/consolidator"
""".strip(),
        encoding="utf-8",
    )

    ws = Workspace(str(workspace_dir), config_source=str(config_file))
    # Should not raise WorkspaceResolutionError
    assert ws.workspace == workspace_dir.resolve()
    assert ws.bos_dir == config_file.resolve().parent
