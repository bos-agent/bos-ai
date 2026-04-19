import signal

from bos.config.workspace import Workspace
from bos.runner.proc import RunDir, build_docker_argv, is_running, stop_agent, write_state


def test_build_docker_argv_passes_external_env_file(tmp_path):
    workspace = tmp_path / "workspace"
    bos_dir = workspace / ".bos"
    workspace.mkdir()
    bos_dir.mkdir()
    external_dir = tmp_path / "env"
    external_dir.mkdir()
    env_file = external_dir / "agent.env"
    env_file.write_text("BOT_TOKEN=test\n", encoding="utf-8")
    (bos_dir / "config.toml").write_text(
        """
[platform]
envfile = "../../env/agent.env"

[main]
agent = "main"

[main.runtime]
kind = "docker"
image = "bos:test"
container_name = "bos-main"
workspace_dir = "/workspace"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
port = 8080
""".strip()
        + "\n",
        encoding="utf-8",
    )

    ws = Workspace(workspace)
    argv = build_docker_argv(ws, ws.get_runtime_config(), detach=True)

    assert "--detach" in argv
    assert argv[-3:] == ["bos:test", "--workspace", "/workspace"]
    assert "--env-file" in argv
    assert str(env_file.resolve()) in argv
    assert f"{workspace.resolve()}:/workspace" in argv
    assert "8080:8080" in argv


def test_build_docker_argv_mounts_external_bos_dir(tmp_path):
    repo = tmp_path / "repo"
    workspace = repo / "app"
    bos_dir = repo / ".bos"
    workspace.mkdir(parents=True)
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[main.runtime]
kind = "docker"
image = "bos:test"
workspace_dir = "/workspace"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    ws = Workspace(workspace)
    argv = build_docker_argv(ws, ws.get_runtime_config(), detach=True)

    assert f"{workspace.resolve()}:/workspace" in argv
    assert f"{bos_dir.resolve()}:/bos" in argv
    assert "BOS_DIR=/bos" in argv


def test_is_running_checks_docker_container_state(tmp_path, monkeypatch):
    rd = RunDir(tmp_path / ".bos")
    write_state(rd, runtime="docker", container_id="abc123")
    monkeypatch.setattr("bos.runner.proc._docker_container_is_running", lambda container_id: container_id == "abc123")

    assert is_running(rd) is True


def test_stop_agent_uses_docker_stop(tmp_path, monkeypatch):
    rd = RunDir(tmp_path / ".bos")
    write_state(rd, runtime="docker", container_id="abc123")
    calls: list[tuple[str, ...]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("bos.runner.proc._docker_run", lambda *args: calls.append(args) or Result())

    stop_agent(rd, signal.SIGTERM)

    assert calls == [("stop", "--signal", "SIGTERM", "abc123")]
