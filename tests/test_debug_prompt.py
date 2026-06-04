from click.testing import CliRunner

from bos.cli.commands.debug import debug
from bos.core import AgentRegistry


def test_debug_prompt_uses_default_actor_agent_cfg(tmp_path):
    config_path = tmp_path / "team.toml"
    config_path.write_text(
        """
[agents.helper]
description = "Configured helper"
system_prompt = "You help."

[runtime]
default_actor = "main"

[runtime.actors.main]
agent = "_default"
display_name = "Main"

[runtime.actors.main.agent_cfg.plugin-bindings.SubagentPlugin]
enabled = ["*"]
""".strip(),
        encoding="utf-8",
    )

    snapshot = dict(AgentRegistry._registry)
    AgentRegistry._registry.clear()
    try:
        result = CliRunner().invoke(debug, ["prompt"], obj={"CONFIG": str(config_path)})
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(snapshot)

    assert result.exit_code == 0, result.output
    assert "Use subagents for broad exploration" in result.output
    assert '<agent role="helper">Configured helper</agent>' in result.output
    assert '<tool name="AskSubagent">' in result.output
