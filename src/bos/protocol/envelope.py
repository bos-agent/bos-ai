from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bos.protocol.message_types import MessageType


@dataclass
class Envelope:
    sender: str
    recipient: str
    content: str
    content_type: MessageType | str = MessageType.MESSAGE
    conversation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content_type, MessageType):
            try:
                self.content_type = MessageType(str(self.content_type))
            except ValueError:
                self.content_type = str(self.content_type)
