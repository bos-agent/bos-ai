import signal

from bos.runner.proc import RunDir, is_running, stop_agent, write_state


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
