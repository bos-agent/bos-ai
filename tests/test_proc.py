import signal

from bos.config.workspace import AgentRuntimeConfig, Workspace
from bos.gateway.state import GatewayRunDir, read_gateway_state, write_gateway_state
from bos.runner.proc import RunDir, build_docker_argv, is_running, stop_agent, write_state


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


def test_build_docker_argv_publishes_gateway_port_not_http_channel(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[runtime]
location = "docker"
default_actor = "main"
image = "bos:test"

[runtime.gateway]
port = 7001

[runtime.actors.main]
agent = "main"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    ws = Workspace.from_discovery(tmp_path)
    runtime = AgentRuntimeConfig(kind="docker", image="bos:test")

    argv = build_docker_argv(ws, runtime, detach=True)

    assert "--publish" in argv
    assert "7001:7001" in argv
    assert "HttpChannel" not in " ".join(argv)


def test_gateway_state_merge_preserves_container_metadata(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")
    write_state(rd, runtime="docker", container_id="abc123")

    write_gateway_state(
        rd,
        {
            "runtime": "docker",
            "gateway": {"host": "0.0.0.0", "port": 7001},
            "actors": {},
            "channels": {},
            "active_turns": {},
        },
    )

    assert read_gateway_state(rd)["container_id"] == "abc123"
