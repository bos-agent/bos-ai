"""HttpChannelClient — WebSocket client for connecting to a running HttpChannel.

This module has no extension point registrations — safe to import standalone
without triggering any server-side or ``ep_channel`` side effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType

from bos.core import Envelope

logger = logging.getLogger(__name__)


def _envelope_to_dict(env: Envelope) -> dict[str, Any]:
    import dataclasses

    d = dataclasses.asdict(env)
    d["timestamp"] = env.timestamp.isoformat()
    return d


class HttpChannelClient:
    """aiohttp WebSocket client for connecting to a running HttpChannel.

    Used by ``bos tui`` to send/receive envelopes over WebSocket without
    direct mailbox access or any server-side imports.

    Example::

        client = HttpChannelClient(host="127.0.0.1", port=8080, address="tui")
        await client.connect()
        await client.send(Envelope(sender="tui", recipient="main", content="hello"))
        reply = await client.receive()
        await client.aclose()
    """

    def __init__(self, host: str, port: int, address: str = "tui") -> None:
        self._url = f"ws://{host}:{port}/ws"
        self._address = address
        self._session: Any = None
        self._ws: Any = None
        self._recv_queue: asyncio.Queue[Envelope] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Open the WebSocket connection and start the background reader."""
        import aiohttp

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url)
        self._reader_task = asyncio.create_task(self._reader())
        logger.debug("HttpChannelClient connected to %s (address=%r)", self._url, self._address)

    async def _reader(self) -> None:
        async for msg in self._ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    ts_raw = data.get("timestamp")
                    ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.now()
                    env = Envelope(
                        sender=data.get("sender", ""),
                        recipient=data.get("recipient", self._address),
                        content=data.get("content", ""),
                        content_type=data.get("content_type", "message"),
                        conversation_id=data.get("conversation_id"),
                        timestamp=ts,
                    )
                    await self._recv_queue.put(env)
                except Exception as exc:
                    logger.debug("Client reader error: %s", exc)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break

    async def send(self, env: Envelope) -> None:
        """Send an envelope to the channel server."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Not connected — call connect() first")
        await self._ws.send_json(_envelope_to_dict(env))

    async def receive(self) -> Envelope:
        """Block until the next envelope arrives."""
        return await self._recv_queue.get()

    async def receive_nowait(self) -> Envelope | None:
        """Non-blocking receive."""
        try:
            return self._recv_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def aclose(self) -> None:
        """Close the WebSocket connection and clean up."""
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.debug("HttpChannelClient disconnected")
