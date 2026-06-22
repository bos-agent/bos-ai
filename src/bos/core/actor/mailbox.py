from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .envelope import Envelope
from .message_types import MessageType


@runtime_checkable
class MailBox[ContentT = Any](Protocol):
    """A point-to-point messaging endpoint bound to one address.

    Generic over content type so the foundation never names the agent's
    ``MessageContent``: ``MailRoute.bind()`` yields a bare ``MailBox`` (=
    ``MailBox[Any]``) and the composition layer annotates ``MailBox[MessageContent]``
    where it wants send/receive to be content-precise.
    """

    @property
    def address(self) -> str: ...

    async def receive(self) -> Envelope[ContentT]: ...

    async def send(
        self,
        recipient: str,
        content: ContentT,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def receive_nowait(self) -> Envelope[ContentT] | None: ...
