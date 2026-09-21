"""Process lifecycle helpers for ``boscli gateway start/stop/status``.

The final gateway runtime stores lifecycle files through
``bos.gateway.state.GatewayRunDir``:

  gateway.pid   — PID of the local gateway launcher process
  gateway.state — JSON status (runtime, pid/container_id, gateway, actors, channels, …)
  gateway.log   — stdout/stderr of the gateway subprocess
  gateway.lock  — advisory flock held by the live gateway process (singleton guard)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from bos.gateway.state import (
    GatewayRunDir,
    acquire_singleton_lock,
    lock_is_free,
    lock_still_owned,
)

LifecycleRunDir = GatewayRunDir

# Re-exported for bos.cli, which imports them from here (gateway restart/stop).
__all__ = [
    "GatewayRunDir",
    "LifecycleRunDir",
    "_pid_alive",
    "_pid_is_gateway",
    "acquire_singleton_lock",
    "is_running",
    "kill_process",
    "lock_is_free",
    "lock_still_owned",
    "read_state",
    "reap_stale",
    "start_background",
    "stop_gateway",
    "write_state",
]


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The PID exists but is owned by another user — alive, but almost
        # certainly not our gateway (PID reuse). Let _pid_is_gateway decide.
        return True


def _pid_is_gateway(pid: int) -> bool:
    """Best-effort check that *pid* is actually a ``bos.runner`` process.

    Guards against PID reuse: a stale recorded PID may have been recycled by an
    unrelated process, which would otherwise make ``is_running`` report a
    phantom gateway and block startup forever. On platforms without a readable
    ``/proc`` we cannot verify, so we assume True and rely on the flock guard.
    """
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = cmdline_path.read_bytes()
    except FileNotFoundError:
        return False  # process gone between checks
    except OSError:
        return True  # /proc unavailable/unreadable — cannot disprove; assume ours
    # Both tokens must be adjacent. Matching them independently made any
    # cmdline holding "bos" and "runner" anywhere — e.g. a CI checkout at
    # /home/runner/work/bos-ai — look like a gateway, which is exactly the
    # PID reuse this guard exists to catch.
    return b"bos.runner" in raw or b"bos/runner" in raw


def is_running(rd: LifecycleRunDir) -> bool:
    """Return True if the recorded gateway process is alive *and ours*."""
    pid = _read_pid(rd)
    if pid is None:
        return False
    return _pid_alive(pid) and _pid_is_gateway(pid)


def reap_stale(rd: LifecycleRunDir) -> bool:
    """Remove leftover pid/state files when no live gateway owns them.

    Handles the case where a gateway process was killed (or crashed) without
    cleaning up — a stale ``gateway.pid``/``gateway.state`` must not block a
    fresh start or hand a stale endpoint to ``boscli ask``. No-op (returns
    False) when a gateway is actually running.
    """
    if is_running(rd):
        return False
    cleaned = False
    for path in (rd.pid_file, rd.state_file):
        try:
            path.unlink()
            cleaned = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return cleaned


def kill_process(rd: LifecycleRunDir, sig: int = signal.SIGTERM) -> None:
    """Send *sig* to the process recorded in the lifecycle PID file."""
    pid = _read_pid(rd)
    if pid is None:
        raise RuntimeError("No PID file found — is the gateway running?")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass  # already gone


def stop_gateway(rd: LifecycleRunDir, sig: int = signal.SIGTERM) -> None:
    """Stop the recorded gateway process."""
    kill_process(rd, sig)


# ── background launch ──────────────────────────────────────────


def start_background(
    argv: list[str],
    rd: LifecycleRunDir,
    env: dict | None = None,
    cwd: Path | str | None = None,
) -> int:
    """Launch *argv* as a detached background process and return its PID.

    Stdout/stderr are redirected to the log file. The PID file is intentionally
    NOT written here: the spawned gateway records it itself only after winning
    the singleton lock. Writing it eagerly would let a refused start (one that
    loses the lock and exits) clobber the live gateway's pid file, divorcing
    ``stop``/``restart`` from the process that actually holds the lock.
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
    return proc.pid
