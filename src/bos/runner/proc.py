"""Process lifecycle helpers for ``boscli gateway start/stop/status``.

The final gateway runtime stores lifecycle files through
``bos.gateway.state.GatewayRunDir``:

  gateway.pid   — PID of the local gateway launcher process
  gateway.state — JSON status (runtime, pid/container_id, gateway, actors, channels, …)
  gateway.log   — stdout/stderr of the gateway subprocess

``RunDir`` remains as a small legacy-compatible path manager for older tests and
non-gateway callers, but all gateway CLI paths should pass ``GatewayRunDir``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bos.config.workspace import AgentRuntimeConfig, Workspace
    from bos.gateway.state import GatewayRunDir


@dataclass
class RunDir:
    """Path manager for .bos/run/ lifecycle files."""

    bos_dir: Path

    def __post_init__(self) -> None:
        self.bos_dir = Path(self.bos_dir).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self.bos_dir / "run"

    @property
    def pid_file(self) -> Path:
        return self.root / "agent.pid"

    @property
    def state_file(self) -> Path:
        return self.root / "agent.state"

    @property
    def log_file(self) -> Path:
        return self.root / "agent.log"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


if TYPE_CHECKING:
    LifecycleRunDir = RunDir | GatewayRunDir
else:
    LifecycleRunDir = RunDir


# ── state file ─────────────────────────────────────────────────


def read_state(rd: LifecycleRunDir) -> dict:
    """Read a lifecycle state JSON file. Returns empty dict if missing/corrupt."""
    try:
        return json.loads(rd.state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(rd: LifecycleRunDir, **fields) -> None:
    """Atomically update lifecycle state with the given fields (merge with existing)."""
    rd.ensure()
    current = read_state(rd)
    current.update({k: v for k, v in fields.items() if v is not None})
    tmp = rd.root / f".{rd.state_file.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(current, default=str), encoding="utf-8")
        tmp.replace(rd.state_file)
    finally:
        tmp.unlink(missing_ok=True)


# ── process checks ─────────────────────────────────────────────


def _read_pid(rd: LifecycleRunDir) -> int | None:
    try:
        return int(rd.pid_file.read_text().strip())
    except Exception:
        return None


def _signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:
        return str(sig)


def _docker_run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Docker CLI not found. Install Docker or run without --docker.") from exc


def _docker_container_is_running(container_id: str) -> bool:
    proc = _docker_run("inspect", "-f", "{{.State.Running}}", container_id)
    if proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "true"


def is_running(rd: LifecycleRunDir) -> bool:
    """Return True if the recorded process or container is alive."""
    state = read_state(rd)
    if state.get("runtime") == "docker":
        container_id = state.get("container_id")
        return bool(container_id) and _docker_container_is_running(str(container_id))

    pid = _read_pid(rd)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_process(rd: LifecycleRunDir, sig: int = signal.SIGTERM) -> None:
    """Send *sig* to the process recorded in the lifecycle PID file."""
    pid = _read_pid(rd)
    if pid is None:
        raise RuntimeError("No PID file found — is the gateway running?")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already gone


def stop_agent(rd: LifecycleRunDir, sig: int = signal.SIGTERM) -> None:
    """Stop the recorded runtime, whether it is a local process or a Docker container."""
    state = read_state(rd)
    if state.get("runtime") == "docker":
        container_id = state.get("container_id")
        if not container_id:
            raise RuntimeError("No Docker container recorded — is the gateway running?")
        cmd = ["kill", "--signal", _signal_name(sig), str(container_id)] if sig == signal.SIGKILL else [
            "stop",
            "--signal",
            _signal_name(sig),
            str(container_id),
        ]
        proc = _docker_run(*cmd)
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise RuntimeError(proc.stderr.strip() or "Failed to stop Docker container.")
        return

    kill_process(rd, sig)


# ── background launch ──────────────────────────────────────────


def start_background(
    argv: list[str],
    rd: LifecycleRunDir,
    env: dict | None = None,
    cwd: Path | str | None = None,
) -> int:
    """Launch *argv* as a detached background process.

    Stdout/stderr are redirected to the log file. The PID is written to
    *rd.pid_file* and returned.
    """
    rd.ensure()
    merged_env = {**os.environ, **(env or {})}

    log = rd.log_file.open("a")
    proc = subprocess.Popen(
        argv,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach from terminal
        env=merged_env,
        cwd=cwd,
    )
    rd.pid_file.write_text(str(proc.pid))
    return proc.pid


def _path_in_tree(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _default_container_bos_dir(workspace: Workspace, runtime: AgentRuntimeConfig) -> str:
    if runtime.bos_dir:
        return runtime.bos_dir
    try:
        rel = workspace.bos_dir.relative_to(workspace.workspace)
        return str((Path(runtime.workspace_dir) / rel).as_posix())
    except ValueError:
        return "/bos"


def _should_mount_bos_dir(workspace: Workspace, runtime: AgentRuntimeConfig, container_bos_dir: str) -> bool:
    try:
        rel = workspace.bos_dir.relative_to(workspace.workspace)
        expected = str((Path(runtime.workspace_dir) / rel).as_posix())
    except ValueError:
        expected = None
    return expected != container_bos_dir


def _docker_env_file(workspace: Workspace) -> Path | None:
    env_file = workspace.resolve_platform_envfile()
    if env_file is None:
        return None
    return env_file if not _path_in_tree(env_file, workspace.workspace) else None


def build_docker_argv(
    workspace: Workspace,
    runtime: AgentRuntimeConfig,
    *,
    detach: bool,
) -> list[str]:
    """Build the Docker command used to run the BOS agent in a container."""
    if not runtime.image:
        raise RuntimeError("Docker runtime requires `main.runtime.image` in .bos/config.toml.")

    container_bos_dir = _default_container_bos_dir(workspace, runtime)
    argv = ["docker", "run", "--rm"]
    if detach:
        argv.append("--detach")
    if runtime.container_name:
        argv.extend(["--name", runtime.container_name])

    argv.extend(
        [
            "--workdir",
            runtime.workspace_dir,
            "--volume",
            f"{workspace.workspace}:{runtime.workspace_dir}",
            "--env",
            "BOS_RUNTIME=docker",
            "--env",
            f"BOS_CONFIG={container_bos_dir}/{workspace.config_file.name}",
        ]
    )

    if _should_mount_bos_dir(workspace, runtime, container_bos_dir):
        argv.extend(["--volume", f"{workspace.bos_dir}:{container_bos_dir}"])

    if env_file := _docker_env_file(workspace):
        argv.extend(["--env-file", str(env_file)])

    gateway_port = workspace.resolve_gateway_config().port
    if gateway_port > 0:
        argv.extend(["--publish", f"{gateway_port}:{gateway_port}"])

    container_config = f"{container_bos_dir}/{workspace.config_file.name}"
    argv.extend([runtime.image, "--config", container_config])
    return argv


def start_docker(workspace: Workspace, rd: LifecycleRunDir, runtime: AgentRuntimeConfig) -> str:
    """Launch the BOS gateway in a detached Docker container and record container metadata."""
    rd.ensure()
    proc = subprocess.run(
        build_docker_argv(workspace, runtime, detach=True),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to start Docker container.")

    container_id = proc.stdout.strip()
    rd.pid_file.unlink(missing_ok=True)
    write_state(rd, runtime="docker", container_id=container_id, container_name=runtime.container_name)
    return container_id


def run_docker_foreground(workspace: Workspace, runtime: AgentRuntimeConfig) -> int:
    """Run the BOS gateway in a foreground Docker container."""
    proc = subprocess.run(build_docker_argv(workspace, runtime, detach=False), check=False)
    return proc.returncode
