from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GatewayRunDir:
    bos_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "bos_dir", Path(self.bos_dir).expanduser().resolve())

    @property
    def root(self) -> Path:
        return self.bos_dir / "run"

    @property
    def pid_file(self) -> Path:
        return self.root / "gateway.pid"

    @property
    def state_file(self) -> Path:
        return self.root / "gateway.state"

    @property
    def cursors_file(self) -> Path:
        """Persisted channel-conversation → chat_id cursors, so persistent channels
        (Telegram, Lark, …) resume their existing chat after a gateway restart."""
        return self.root / "chat_cursors.json"

    @property
    def log_file(self) -> Path:
        return self.root / "gateway.log"

    @property
    def lock_file(self) -> Path:
        return self.root / "gateway.lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


def read_gateway_state(run_dir: GatewayRunDir) -> dict[str, Any]:
    try:
        return json.loads(run_dir.state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_gateway_state(run_dir: GatewayRunDir, snapshot: dict[str, Any]) -> None:
    run_dir.ensure()
    payload = read_gateway_state(run_dir)
    payload.update(snapshot)
    payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    tmp = run_dir.root / f".gateway.state.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(run_dir.state_file)
    finally:
        tmp.unlink(missing_ok=True)


# ── singleton lock ─────────────────────────────────────────────
# Advisory flock guarding "one live gateway per run dir". It lives here rather
# than in bos.runner because the gateway reports lock ownership as part of its
# own state, and bos.gateway may not import bos.runner (BEP 13 ring guard).


def lock_still_owned(rd: GatewayRunDir, handle) -> bool:
    """Return True if *handle* still locks the live ``gateway.lock`` inode.

    ``flock`` binds to an inode, not a path. If the lock file is unlinked and
    recreated — a stale run dir wiped by hand, or a racing starter — the handle
    keeps locking an orphaned inode while a fresh process can lock the new file,
    so both would believe they are the singleton. Comparing the handle's inode
    to the file currently at the path detects that divergence. Returns False if
    either stat fails (file gone), which callers treat as lost ownership.
    """
    try:
        held = os.fstat(handle.fileno())
        on_disk = os.stat(rd.lock_file)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)


def acquire_singleton_lock(rd: GatewayRunDir):
    """Acquire the exclusive, non-blocking gateway lock for this run dir.

    Returns an open file object that MUST be kept referenced for the process
    lifetime (closing it, or the process exiting/crashing, releases the lock).
    Returns None if another live gateway already holds the lock. On platforms
    without ``fcntl`` (e.g. Windows) locking is unsupported and a no-op handle
    is returned so callers proceed unguarded.
    """
    rd.ensure()
    try:
        import fcntl
    except ImportError:
        return rd.lock_file.open("w")  # locking unsupported; behave as before

    # Lock, then confirm the inode we locked is still the file at the path. If a
    # racing starter replaced the file between open() and flock(), our lock is on
    # an orphaned inode — drop it and retry against the current file. A bounded
    # retry converges: either we lock the live file, or another holder owns it
    # and flock fails.
    for _ in range(5):
        handle = rd.lock_file.open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        if lock_still_owned(rd, handle):
            return handle
        handle.close()
    return None


def lock_is_free(rd: GatewayRunDir) -> bool:
    """Best-effort probe: True if the singleton flock is currently acquirable.

    Acquires the lock non-blocking and releases it immediately, so a caller can
    poll for a previous gateway to *actually* let go of the lock. This is the
    correct signal for ``restart``: a dying gateway unlinks its pid file (and
    ``stop`` removes the state file) well before the process has fully exited and
    the OS has dropped the flock, so ``is_running`` — which keys off the pid file
    — reports "stopped" while the lock is still held. Polling this avoids the
    fresh gateway racing the still-exiting one and losing the lock.

    On platforms without ``fcntl`` (e.g. Windows) locking is unsupported, so we
    cannot observe contention and report free (matching ``acquire_singleton_lock``,
    which proceeds unguarded there).
    """
    rd.ensure()
    try:
        import fcntl
    except ImportError:
        return True
    try:
        handle = rd.lock_file.open("w")
    except OSError:
        return True  # cannot open to probe — do not block the caller
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False  # another live process still holds it
    finally:
        handle.close()  # releases the probe lock (if we took it)
