"""Process lifecycle helpers for bos start/stop/status.

Manages the `.bos/run/` directory:
  agent.pid   — PID of the running actor process
  agent.state — JSON status (pid, started_at, last_active, channels, …)
  agent.log   — stdout/stderr of the actor subprocess
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


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


# ── state file ─────────────────────────────────────────────────


def read_state(rd: RunDir) -> dict:
    """Read agent.state JSON. Returns empty dict if missing or corrupt."""
    try:
        return json.loads(rd.state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(rd: RunDir, **fields) -> None:
    """Atomically update agent.state with the given fields (merge with existing)."""
    rd.ensure()
    current = read_state(rd)
    current.update({k: v for k, v in fields.items() if v is not None})
    tmp = rd.root / f".agent.state.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(current, default=str), encoding="utf-8")
        tmp.replace(rd.state_file)
    finally:
        tmp.unlink(missing_ok=True)


# ── process checks ─────────────────────────────────────────────


def _read_pid(rd: RunDir) -> int | None:
    try:
        return int(rd.pid_file.read_text().strip())
    except Exception:
        return None


def is_running(rd: RunDir) -> bool:
    """Return True if the PID in agent.pid is alive."""
    pid = _read_pid(rd)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_process(rd: RunDir, sig: int = signal.SIGTERM) -> None:
    """Send *sig* to the process recorded in agent.pid."""
    pid = _read_pid(rd)
    if pid is None:
        raise RuntimeError("No PID file found — is the agent running?")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already gone


# ── background launch ──────────────────────────────────────────


def start_background(argv: list[str], rd: RunDir, env: dict | None = None) -> int:
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
    )
    rd.pid_file.write_text(str(proc.pid))
    return proc.pid
