import os
import signal

from bos.config.workspace import AgentRuntimeConfig, Workspace, resolve_config_source
from bos.gateway.state import GatewayRunDir, read_gateway_state, write_gateway_state
from bos.runner.proc import (
    acquire_singleton_lock,
    build_docker_argv,
    is_running,
    lock_is_free,
    lock_still_owned,
    reap_stale,
    start_background,
    stop_gateway,
    write_state,
)


def test_is_running_checks_docker_container_state(tmp_path, monkeypatch):
    rd = GatewayRunDir(tmp_path / ".bos")
    write_state(rd, runtime="docker", container_id="abc123")
    monkeypatch.setattr("bos.runner.proc._docker_container_is_running", lambda container_id: container_id == "abc123")

    assert is_running(rd) is True


def test_stop_gateway_uses_docker_stop(tmp_path, monkeypatch):
    rd = GatewayRunDir(tmp_path / ".bos")
    write_state(rd, runtime="docker", container_id="abc123")
    calls: list[tuple[str, ...]] = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("bos.runner.proc._docker_run", lambda *args: calls.append(args) or Result())

    stop_gateway(rd, signal.SIGTERM)

    assert calls == [("stop", "--signal", "SIGTERM", "abc123")]


def test_build_docker_argv_publishes_gateway_port_not_http_channel(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[runtime]
location = "docker"
main_actor = "main"
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


def test_build_docker_argv_preserves_preset_config_arg(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    config_path, bos_dir, config = resolve_config_source("default")
    ws = Workspace(tmp_path / "project", bos_dir, config, config_file=config_path)
    runtime = AgentRuntimeConfig(kind="docker", image="bos:test")

    argv = build_docker_argv(ws, runtime, detach=True, config_arg="default")

    assert "BOS_CONFIG=default" in argv
    assert argv[-2:] == ["--config", "default"]


def test_singleton_lock_blocks_a_second_holder(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")

    first = acquire_singleton_lock(rd)
    assert first is not None
    # A second acquisition for the same run dir must be refused while held.
    assert acquire_singleton_lock(rd) is None

    first.close()
    # Once released, the lock is acquirable again.
    again = acquire_singleton_lock(rd)
    assert again is not None
    again.close()


def test_lock_is_free_tracks_a_live_holder(tmp_path):
    # restart polls lock_is_free() to wait for a dying gateway to actually drop
    # the flock — not is_running(), which flips as soon as the pid file is gone
    # (well before the OS releases the lock). Guard that the probe reflects the
    # real holder and never steals or releases it.
    rd = GatewayRunDir(tmp_path / ".bos")

    assert lock_is_free(rd) is True  # nobody holds it yet

    holder = acquire_singleton_lock(rd)
    assert holder is not None
    assert lock_is_free(rd) is False  # busy while a live gateway holds it
    assert lock_still_owned(rd, holder) is True  # probing must not disturb the holder

    holder.close()
    assert lock_is_free(rd) is True  # released → acquirable again


def test_lock_still_owned_detects_recreated_lock_file(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")

    handle = acquire_singleton_lock(rd)
    assert handle is not None
    assert lock_still_owned(rd, handle) is True

    # An external wipe + recreate leaves the handle locking an orphaned inode.
    rd.lock_file.unlink()
    assert lock_still_owned(rd, handle) is False  # file gone
    rd.lock_file.write_text("")  # fresh inode at the same path
    assert lock_still_owned(rd, handle) is False  # different inode

    handle.close()


def test_acquire_singleton_lock_recovers_after_lock_file_deleted(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")

    first = acquire_singleton_lock(rd)
    assert first is not None

    # The lock file is wiped externally; `first` now locks an orphaned inode.
    rd.lock_file.unlink()

    # A fresh acquire succeeds against the recreated file and owns the path...
    second = acquire_singleton_lock(rd)
    assert second is not None
    assert lock_still_owned(rd, second) is True
    # ...while the orphaned `first` no longer owns it — the signal the live
    # gateway's watchdog uses to stand down instead of double-polling.
    assert lock_still_owned(rd, first) is False

    first.close()
    second.close()


def test_start_background_does_not_clobber_live_pid_file(tmp_path, monkeypatch):
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    rd.pid_file.write_text("726902")  # a live gateway already recorded here

    class _FakeProc:
        pid = 999001

    monkeypatch.setattr("bos.runner.proc.subprocess.Popen", lambda *a, **k: _FakeProc())

    pid = start_background(["python", "-m", "bos.runner"], rd)

    assert pid == 999001
    # The spawned (not-yet-locked) child must NOT overwrite the live gateway's
    # pid file — only a child that wins the singleton lock records its own PID.
    assert rd.pid_file.read_text() == "726902"


def test_is_running_false_for_dead_pid(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    rd.pid_file.write_text("999999")  # not a live process
    assert is_running(rd) is False


def test_is_running_false_for_reused_non_gateway_pid(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    # A live PID that is not a bos.runner (this test process) must not register
    # as a running gateway — guards against PID reuse blocking a fresh start.
    rd.pid_file.write_text(str(os.getpid()))
    assert is_running(rd) is False


def test_reap_stale_clears_leftover_files(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    rd.pid_file.write_text("999999")
    rd.state_file.write_text('{"gateway": {"host": "127.0.0.1", "port": 12345}}')

    assert reap_stale(rd) is True
    assert not rd.pid_file.exists()
    assert not rd.state_file.exists()
    # Idempotent: nothing left to clean.
    assert reap_stale(rd) is False


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
