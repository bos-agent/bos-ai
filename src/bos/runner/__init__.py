"""Runner module — process lifecycle and orchestration for boscli gateway start/stop/status."""

from bos.gateway.state import GatewayRunDir
from bos.runner.mount import GatewayMount
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
from bos.runner.runner import serve, start

__all__ = [
    "GatewayMount",
    "GatewayRunDir",
    "acquire_singleton_lock",
    "is_running",
    "kill_process",
    "lock_still_owned",
    "read_state",
    "reap_stale",
    "serve",
    "start",
    "start_background",
    "write_state",
]
