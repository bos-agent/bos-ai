"""Runner module — process lifecycle and orchestration for boscli gateway start/stop/status."""

from bos.gateway.state import GatewayRunDir
from bos.runner.proc import (
    acquire_singleton_lock,
    is_running,
    kill_process,
    lock_still_owned,
    read_state,
    reap_stale,
    start_background,
    write_state,
)
from bos.runner.runner import start

__all__ = [
    "GatewayRunDir",
    "acquire_singleton_lock",
    "is_running",
    "kill_process",
    "lock_still_owned",
    "read_state",
    "reap_stale",
    "start",
    "start_background",
    "write_state",
]
