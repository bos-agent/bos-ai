"""`boscli ask` selects an agent explicitly (BEP 18 §3.6)."""

from __future__ import annotations

from click.testing import CliRunner

from bos.cli.entry import cli


def _project(tmp_path, body: str) -> str:
    (tmp_path / ".bos").mkdir(parents=True, exist_ok=True)
    config = tmp_path / ".bos" / "config.toml"
    config.write_text(body, encoding="utf-8")
    return str(config)


_MODE_1 = """
default_agent = "writer"

[platform]
extensions = []

[agents.writer]
system_prompt = "You write."

[agents.researcher]
system_prompt = "You research."
"""


def test_agent_and_actor_together_are_refused(tmp_path):
    """Review Focus 4: mutually exclusive, and it must say so before doing work."""
    config = _project(tmp_path, _MODE_1)
    result = CliRunner().invoke(cli, ["-c", config, "ask", "--agent", "writer", "--actor", "main", "hi"])

    assert result.exit_code != 0
    assert "--agent" in result.output and "--actor" in result.output


def test_actor_in_a_project_with_none_says_so(tmp_path):
    """Review Focus 5: no KeyError, and the message names the situation."""
    config = _project(tmp_path, _MODE_1)
    result = CliRunner().invoke(cli, ["-c", config, "ask", "--actor", "main", "hi"])

    assert result.exit_code != 0
    assert "actor" in result.output.lower()
    assert "Traceback" not in result.output


def test_unknown_actor_lists_the_ones_that_exist(tmp_path):
    """Review Focus 5, the other half."""
    config = _project(tmp_path, _MODE_1 + """
[runtime]
main_actor = "main"

[runtime.actors.main]
agent = "writer"
""")
    result = CliRunner().invoke(cli, ["-c", config, "ask", "--actor", "nope", "hi"])

    assert result.exit_code != 0
    assert "nope" in result.output and "main" in result.output
