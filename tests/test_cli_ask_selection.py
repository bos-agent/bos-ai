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

# Same two agents, but no default_agent — bare `ask` cannot tell which to use.
_NO_DEFAULT_AGENT = """
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


def test_agent_and_actor_refused_before_workspace_is_built(tmp_path):
    """Finding 3: pin the ordering. A valid config would still pass this check
    if it moved below _get_ws_and_rd, since resolving a real config also
    succeeds. A config path that doesn't exist at all is the only thing that
    discriminates: if the refusal happens first, the output names the two
    flags; if workspace-building ran first, it would instead be a config
    resolution error that never mentions --agent/--actor.
    """
    result = CliRunner().invoke(
        cli, ["-c", "/nonexistent/nope.toml", "ask", "--agent", "writer", "--actor", "main", "hi"]
    )

    assert result.exit_code != 0
    assert "--agent" in result.output and "--actor" in result.output


def test_actor_in_a_project_with_none_says_so(tmp_path):
    """Review Focus 5: no KeyError, and the message names the situation.

    Finding 1: asserting only "actor" in the output is satisfied by click's own
    "Error: No such option: --actor" when --actor doesn't exist at all, so this
    could pass with the whole feature reverted. Assert on the branch's own
    words instead — text only this code can produce.
    """
    config = _project(tmp_path, _MODE_1)
    result = CliRunner().invoke(cli, ["-c", config, "ask", "--actor", "main", "hi"])

    assert result.exit_code != 0
    assert "defines no actors" in result.output
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


def test_bare_ask_with_no_default_agent_reports_one_line(tmp_path):
    """Finding 2: the try/except ValueError around resolve_default_agent() has
    no coverage — deleting it leaves all other tests green while a real
    ambiguous project gets a raw traceback instead of one line. Its message's
    freedom from "actor"/"gateway"/"runtime" is the point of Task 1 and 3
    together (BEP 18 §3.5): those words would reintroduce the very framing
    this change removes, so assert their absence too.
    """
    config = _project(tmp_path, _NO_DEFAULT_AGENT)
    result = CliRunner().invoke(cli, ["-c", config, "ask", "hi"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Cannot tell which agent to use" in result.output
    lowered = result.output.lower()
    for word in ("actor", "gateway", "runtime"):
        assert word not in lowered
