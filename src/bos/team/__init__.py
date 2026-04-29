"""Multi-actor team runtime.

The core runtime owns single-actor async execution. This package owns the
optional peer task protocol for configured multi-actor topologies.
"""

from __future__ import annotations

from .harness import TeamHarness
from .runtime import PeerTaskRuntime
from .tasks import ActorRef, ChatTaskBinding, TaskEvent, TaskLedger, TaskLedgerError, TaskRecord

__all__ = [
    "ActorRef",
    "ChatTaskBinding",
    "PeerTaskRuntime",
    "TaskEvent",
    "TaskLedger",
    "TaskLedgerError",
    "TaskRecord",
    "TeamHarness",
]
