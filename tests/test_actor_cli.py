from click.testing import CliRunner

from bos.cli.entry import cli
from bos.runner.proc import RunDir, write_state


def test_actor_command_shows_configured_actors_when_stopped(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.actors]]
name = "researcher"
agent = "researcher"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["--workspace", str(tmp_path), "actor"])

    assert result.exit_code == 0
    assert "Name" in result.output
    assert "main" in result.output
    assert "coordinator" in result.output
    assert "agent@main" in result.output
    assert "researcher" in result.output
    assert "worker" in result.output
    assert "stopped" in result.output


def test_actor_command_overlays_runtime_actor_status(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.actors]]
name = "researcher"
agent = "researcher"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    write_state(
        RunDir(bos_dir),
        actors=[
            {
                "name": "main",
                "agent": "main",
                "address": "agent@main",
                "role": "coordinator",
                "status": "idle",
            },
            {
                "name": "researcher",
                "agent": "researcher",
                "address": "agent@researcher",
                "role": "worker",
                "status": "waiting_input",
            },
        ],
    )
    monkeypatch.setattr("bos.runner.proc.is_running", lambda rd: True)

    result = CliRunner().invoke(cli, ["--workspace", str(tmp_path), "actor"])

    assert result.exit_code == 0
    assert "main" in result.output
    assert "idle" in result.output
    assert "researcher" in result.output
    assert "waiting_input" in result.output
