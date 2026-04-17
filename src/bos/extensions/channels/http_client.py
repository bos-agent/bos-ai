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

from bos.protocol import Envelope, MessageType

logger = logging.getLogger(__name__)

# Reconnect tunables
_RECONNECT_BASE_DELAY = 0.5  # seconds
_RECONNECT_MAX_DELAY = 10.0  # seconds
_RECONNECT_BACKOFF = 2.0  # multiplier


def _envelope_to_dict(env: Envelope) -> dict[str, Any]:
    import dataclasses

    d = dataclasses.asdict(env)
    d["timestamp"] = env.timestamp.isoformat()
    return d


class HttpChannelClient:
    """aiohttp WebSocket client for connecting to a running HttpChannel.

    Used by ``bos tui`` to send/receive envelopes over WebSocket without
    direct mailbox access or any server-side imports.

    Automatically reconnects when the WebSocket connection drops (e.g. after
    an agent restart), with exponential backoff.

    Example::

        client = HttpChannelClient(host="127.0.0.1", port=8080, address="tui")
        await client.connect()
        await client.send("hello")
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
        self._closed = False  # explicit close requested
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Open the WebSocket connection and start the background reader."""
        await self._do_connect()
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.debug("HttpChannelClient connected to %s (address=%r)", self._url, self._address)

    async def _do_connect(self) -> None:
        """Low-level connect (or reconnect). Creates session + WS."""
        import aiohttp

        # Clean up any previous session
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url)
        self._connected.set()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff. Blocks until connected or closed."""
        self._connected.clear()
        delay = _RECONNECT_BASE_DELAY
        while not self._closed:
            try:
                logger.info("Reconnecting to %s in %.1fs …", self._url, delay)
                await asyncio.sleep(delay)
                await self._do_connect()
                logger.info("Reconnected to %s", self._url)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Reconnect failed: %s", exc)
                delay = min(delay * _RECONNECT_BACKOFF, _RECONNECT_MAX_DELAY)

    async def _reader_loop(self) -> None:
        """Background reader: reads WS messages and reconnects on drop."""
        while not self._closed:
            try:
                await self._connected.wait()
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
                                content_type=data.get("content_type", MessageType.MESSAGE),
                                conversation_id=data.get("conversation_id"),
                                timestamp=ts,
                                metadata=data.get("metadata", {}),
                            )
                            await self._recv_queue.put(env)
                        except Exception as exc:
                            logger.debug("Client reader error: %s", exc)
                    elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                        break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Reader loop error: %s", exc)

            # WS stream ended — reconnect unless explicitly closed
            if not self._closed:
                logger.info("WebSocket disconnected — will reconnect")
                await self._reconnect()

    async def send(
        self,
        content: str,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Send a message to the channel server.

        If the connection is down, waits for reconnection (up to 15s)
        before raising.
        """
        if not self.connected:
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=15)
            except asyncio.TimeoutError:
                raise RuntimeError("Not connected — reconnect timed out")
        await self._ws.send_json(
            _envelope_to_dict(
                Envelope(
                    sender=self._address,
                    recipient="",
                    content=content,
                    content_type=content_type,
                    conversation_id=conversation_id,
                    metadata=metadata or {},
                )
            )
        )

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
        self._closed = True
        self._connected.set()  # unblock anything waiting on reconnect
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.debug("HttpChannelClient disconnected")
