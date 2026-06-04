"""Runner module — process lifecycle and orchestration for boscli gateway start/stop/status."""

from bos.gateway.state import GatewayRunDir
from bos.runner.proc import is_running, kill_process, read_state, start_background, write_state
from bos.runner.runner import start

__all__ = [
    "GatewayRunDir",
    "is_running",
    "kill_process",
    "read_state",
    "start",
    "start_background",
    "write_state",
]
