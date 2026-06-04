from bos.protocol.content import MessageContent, MessageContentPart
from bos.protocol.envelope import Envelope
from bos.protocol.message_types import MessageType
from bos.protocol.turn_events import TurnEvent

WS_TAKEOVER_CLOSE_CODE = 4001
WS_TAKEOVER_CLOSE_REASON = "Another interactive channel took over this WebSocket session."

__all__ = [
    "Envelope",
    "MessageContent",
    "MessageContentPart",
    "MessageType",
    "TurnEvent",
    "WS_TAKEOVER_CLOSE_CODE",
    "WS_TAKEOVER_CLOSE_REASON",
]
