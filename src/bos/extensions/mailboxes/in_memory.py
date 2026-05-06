"""In-memory mail route.

Useful for testing and lightweight ephemeral workloads that do not
require persistence.
"""

from __future__ import annotations

import asyncio
from typing import Any

from bos.core.contract import ep_mail_route
from bos.protocol import Envelope, MessageContent, MessageType


class _InMemMailBox:
    def __init__(self, route: InMemMailRoute, address: str) -> None:
        self._route = route
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    async def receive(self) -> Envelope:
        return await self._route.receive(self._address)

    async def send(
        self,
        recipient: str,
        content: MessageContent,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._route.deliver(
            Envelope(
                sender=self._address,
                recipient=recipient,
                content=content,
                content_type=content_type,
                chat_id=chat_id,
                metadata=metadata or {},
            )
        )

    async def receive_nowait(self) -> Envelope | None:
        return await self._route.receive_nowait(self._address)


@ep_mail_route(name="InMemMailRoute")
class InMemMailRoute:
    """In-memory mail route backed by asyncio queues."""

    _queues: dict[str, asyncio.Queue[Envelope]] = {}

    @classmethod
    def _get_queue(cls, address: str) -> asyncio.Queue[Envelope]:
        if address not in cls._queues:
            cls._queues[address] = asyncio.Queue()
        return cls._queues[address]

    def bind(self, address: str) -> _InMemMailBox:
        return _InMemMailBox(self, address)

    async def deliver(self, env: Envelope) -> None:
        await self._get_queue(env.recipient).put(env)

    async def receive(self, address: str) -> Envelope:
        return await self._get_queue(address).get()

    async def receive_nowait(self, address: str) -> Envelope | None:
        try:
            return self._get_queue(address).get_nowait()
        except asyncio.QueueEmpty:
            return None
