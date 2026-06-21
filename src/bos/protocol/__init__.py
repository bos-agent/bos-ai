from typing import TYPE_CHECKING, Any

from bos.protocol.envelope import Envelope
from bos.protocol.message_types import MessageType

# ``MessageContent``/``MessageContentPart``/``TurnEvent`` are owned by the agent
# core (``bos.core.agent``). They are re-exported here for backward
# compatibility, but pulled in lazily via ``__getattr__`` so that importing
# ``bos.protocol`` never triggers ``bos.core`` at module-init time — keeping
# ``bos.protocol`` a leaf and avoiding an import-order deadlock.

if TYPE_CHECKING:
    from bos.core.agent import MessageContent, MessageContentPart, TurnEvent

WS_TAKEOVER_CLOSE_CODE = 4001
WS_TAKEOVER_CLOSE_REASON = "Another interactive channel took over this WebSocket session."

_LAZY_FROM_AGENT = ("MessageContent", "MessageContentPart", "TurnEvent")

__all__ = [
    "Envelope",
    "MessageContent",
    "MessageContentPart",
    "MessageType",
    "TurnEvent",
    "WS_TAKEOVER_CLOSE_CODE",
    "WS_TAKEOVER_CLOSE_REASON",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY_FROM_AGENT:
        import bos.core.agent as _agent

        return getattr(_agent, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
