"""LocalClient — in-process mailbox-backed client for ChatApp.

Implements the same interface as HttpChannelClient so ChatApp can be
reused unchanged when running in interactive mode (``bos ask -i``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bos.named_actors.registry import ActorRegistry
from bos.protocol import Envelope, MessageContent, MessageType

logger = logging.getLogger(__name__)


class LocalClient:
    """In-process client that bridges ChatApp to an AgentActor via mailboxes.

    Same ``send`` / ``receive`` / ``connect`` / ``list_actors`` / ``aclose``
    interface as ``HttpChannelClient``, but backed by in-memory mailboxes
    instead of WebSocket.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_mbox: Any,
        registry: ActorRegistry,
        chat_state: Any,
    ) -> None:
        self._client_id = client_id
        self._client_mbox = client_mbox
        self._registry = registry
        self._chat_state = chat_state
        self._chat_id: str | None = None
        self._recv_queue: asyncio.Queue[Envelope] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return True

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def chat_id(self) -> str | None:
        return self._chat_id

    def update_chat_id(self, chat_id: str) -> None:
        if not chat_id:
            raise ValueError("chat_id must be non-empty.")
        self._chat_id = chat_id

    async def connect(self, *, takeover: bool = False) -> None:
        chat_id = self._chat_state.resolve_for_client(self._client_id, None)
        self._chat_id = chat_id

        await self._recv_queue.put(
            Envelope(
                sender=self._client_mbox.address,
                recipient=self._client_id,
                content="Session started.",
                content_type=MessageType.SYSTEM,
                chat_id=chat_id,
                metadata={
                    "event": "session",
                    "client_id": self._client_id,
                    "chat_id": chat_id,
                },
            )
        )

        self._reader_task = asyncio.create_task(self._reader_loop())

    async def send(
        self,
        content: MessageContent,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        effective_chat_id = chat_id or self._chat_id
        merged_metadata: dict[str, Any] = dict(metadata or {})

        routing = dict(merged_metadata.get("routing") or {})
        routing["client_id"] = self._client_id
        routing["chat_id"] = effective_chat_id
        merged_metadata["routing"] = routing

        route_result = self._registry.route(
            str(content) if isinstance(content, str) else content,
            metadata=merged_metadata,
        )

        await self._client_mbox.send(
            route_result.target_address,
            route_result.content,
            content_type=content_type,
            chat_id=effective_chat_id,
            metadata=route_result.metadata,
        )

    async def receive(self) -> Envelope:
        return await self._recv_queue.get()

    async def list_actors(self) -> dict[str, dict[str, Any]]:
        actors = self._registry.list_actors()
        return {
            name: {
                "display_name": rec.display_name,
                "agent_kind": rec.agent_kind,
                "is_default": rec.is_default,
            }
            for name, rec in actors.items()
        }

    async def aclose(self) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)

    async def _reader_loop(self) -> None:
        while not self._closed:
            try:
                env = await self._client_mbox.receive()
                await self._recv_queue.put(env)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("LocalClient reader error", exc_info=True)
