from click.testing import CliRunner

from bos.cli.entry import cli
from bos.core import ep_agent


def test_cli_prompt_prints_built_system_prompt_for_selected_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "prompt-inspector"

[platform]

[[platform.agents]]
name = "prompt-inspector"
description = "Prompt inspector"
system_prompt = "Inspect this prompt."
tools = []
skills = []
memories = []
subagents = []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    try:
        result = CliRunner().invoke(cli, ["--workspace", str(tmp_path), "prompt"])
    finally:
        ep_agent._extensions.pop("prompt-inspector", None)

    assert result.exit_code == 0
    assert result.output.startswith("--- SYSTEM PROMPT ---\n\nInspect this prompt.\n\n--- SYSTEM INFORMATION ---")
    assert "CreateTask" not in result.output


def test_cli_prompt_peer_tools_enables_peer_task_tools_for_main_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "prompt-peer-inspector"

[platform]

[[platform.agents]]
name = "prompt-peer-inspector"
description = "Prompt peer inspector"
system_prompt = "Inspect peer tools."
tools = []
skills = []
memories = []
subagents = []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    try:
        result = CliRunner().invoke(cli, ["--workspace", str(tmp_path), "prompt", "--peer-tools"])
    finally:
        ep_agent._extensions.pop("prompt-peer-inspector", None)

    assert result.exit_code == 0
    assert "### ListActors ###" in result.output
    assert "### CreateTask ###" in result.output
    assert "### ProvideTaskInput ###" in result.output
    assert "### AbortTask ###" in result.output
