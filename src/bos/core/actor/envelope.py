from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .message_types import MessageType


@dataclass
class Envelope[ContentT = Any]:
    """A message in transit between mailboxes.

    Domain-agnostic: ``content`` is a type parameter (defaulting to ``Any``), so
    the actor foundation never names the agent's ``MessageContent``. The
    composition layer annotates ``Envelope[MessageContent]`` where it wants
    precision. Structural validation of agent message content lives with the
    agent core (it validates content as it processes a turn); this envelope only
    enforces the generic transport invariant below.
    """

    sender: str
    recipient: str
    content: ContentT
    content_type: MessageType | str = MessageType.MESSAGE
    chat_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content_type, MessageType):
            try:
                self.content_type = MessageType(str(self.content_type))
            except ValueError:
                self.content_type = str(self.content_type)
        # Generic transport invariant: only MESSAGE envelopes may carry
        # non-string (structured/multimodal) content; everything else is text.
        if self.content_type != MessageType.MESSAGE and not isinstance(self.content, str):
            raise TypeError("Non-message envelopes require string content.")
