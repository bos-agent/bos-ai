"""HttpChannelClient — WebSocket client for connecting to a running BOS gateway.

This module has no extension point registrations — safe to import standalone
without triggering any server-side or ``ep_channel`` side effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import WSMsgType

from bos.protocol import WS_TAKEOVER_CLOSE_CODE, WS_TAKEOVER_CLOSE_REASON, Envelope, MessageContent, MessageType

# Type alias for the optional endpoint resolver callback.
# Returns (host, port) or None if the endpoint cannot be determined.
EndpointResolver = Callable[[], tuple[str, int] | None]

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
    """aiohttp WebSocket client for connecting to a running BOS gateway.

    Used by ``boscli tui`` to send/receive envelopes over WebSocket without
    direct mailbox access or any server-side imports.

    Automatically reconnects when the WebSocket connection drops (e.g. after
    an agent restart), with exponential backoff.

    Example::

        client = HttpChannelClient(host="127.0.0.1", port=5920, address="tui")
        await client.connect()
        await client.send("hello")
        reply = await client.receive()
        await client.aclose()
    """

    def __init__(
        self,
        host: str,
        port: int,
        address: str = "tui",
        *,
        channel_id: str | None = None,
        client_id: str | None = None,
        chat_id: str | None = None,
        endpoint_resolver: EndpointResolver | None = None,
        api_key: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._endpoint_resolver = endpoint_resolver
        self._rebuild_urls()
        self._address = address
        self._channel_id = (channel_id or client_id or address or uuid.uuid4().hex).strip()
        self._api_key = api_key
        self._chat_id = (
            chat_id.strip() if isinstance(chat_id, str) and chat_id else None
        )
        self._session: Any = None
        self._ws: Any = None
        self._recv_queue: asyncio.Queue[Envelope] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False  # explicit close requested
        self._connected = asyncio.Event()

    def _rebuild_urls(self) -> None:
        self._url = f"ws://{self._host}:{self._port}/ws"
        self._http_base_url = f"http://{self._host}:{self._port}"

    def _resolve_endpoint(self) -> None:
        """Re-discover host:port via the resolver callback, if provided."""
        if self._endpoint_resolver is None:
            return
        result = self._endpoint_resolver()
        if result is None:
            return
        host, port = result
        if host != self._host or port != self._port:
            logger.info("Endpoint changed: %s:%d -> %s:%d", self._host, self._port, host, port)
            self._host = host
            self._port = port
            self._rebuild_urls()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def client_id(self) -> str:
        return self._channel_id

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def chat_id(self) -> str | None:
        return self._chat_id

    def update_chat_id(self, chat_id: str) -> None:
        if not chat_id:
            raise ValueError("chat_id must be non-empty.")
        self._chat_id = chat_id

    async def connect(self, *, takeover: bool = False) -> None:
        """Open the WebSocket connection and start the background reader."""
        await self._do_connect(takeover=takeover)
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.debug("HttpChannelClient connected to %s (address=%r)", self._url, self._address)

    async def _do_connect(self, *, takeover: bool = False) -> None:
        """Low-level connect (or reconnect). Creates session + WS."""
        import aiohttp

        # Clean up any previous session
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

        self._session = aiohttp.ClientSession(headers=self._auth_headers())
        query: dict[str, str] = {"channel_id": self._channel_id, "client_id": self._channel_id}
        if self._chat_id:
            query["chat_id"] = self._chat_id
        if takeover:
            query["takeover"] = "1"
        url = f"{self._url}?{urlencode(query)}"
        self._ws = await self._session.ws_connect(url)
        await self._receive_session_ack()
        self._connected.set()

    async def _receive_session_ack(self) -> None:
        msg = await self._ws.receive(timeout=5)
        if msg.type != WSMsgType.TEXT:
            raise RuntimeError("Gateway did not send session acknowledgement.")
        data = json.loads(msg.data)
        metadata = data.get("metadata") or {}
        if data.get("content_type") != MessageType.SYSTEM or metadata.get("event") != "session":
            raise RuntimeError("Gateway sent an invalid session acknowledgement.")
        client_id = metadata.get("channel_id") or metadata.get("client_id")
        chat_id = metadata.get("chat_id") or data.get("chat_id")
        if isinstance(client_id, str) and client_id:
            self._channel_id = client_id
        if isinstance(chat_id, str) and chat_id:
            self._chat_id = chat_id

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff. Blocks until connected or closed."""
        self._connected.clear()
        delay = _RECONNECT_BASE_DELAY
        while not self._closed:
            try:
                self._resolve_endpoint()
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

    async def _emit_takeover_system_event(self) -> None:
        self._closed = True
        self._connected.clear()
        await self._recv_queue.put(
            Envelope(
                sender="channel@http",
                recipient=self._address,
                content=WS_TAKEOVER_CLOSE_REASON,
                content_type=MessageType.SYSTEM,
            )
        )

    async def _reader_loop(self) -> None:
        """Background reader: reads WS messages and reconnects on drop."""
        while not self._closed:
            should_reconnect = True
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
                                chat_id=data.get("chat_id"),
                                timestamp=ts,
                                metadata=data.get("metadata", {}),
                            )
                            await self._recv_queue.put(env)
                        except Exception as exc:
                            logger.debug("Client reader error: %s", exc)
                    elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                        if getattr(self._ws, "close_code", None) == WS_TAKEOVER_CLOSE_CODE:
                            should_reconnect = False
                            await self._emit_takeover_system_event()
                        break
                if not self._closed and getattr(self._ws, "close_code", None) == WS_TAKEOVER_CLOSE_CODE:
                    should_reconnect = False
                    await self._emit_takeover_system_event()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Reader loop error: %s", exc)

            # WS stream ended — reconnect unless explicitly closed
            if not self._closed and should_reconnect:
                logger.info("WebSocket disconnected — will reconnect")
                await self._reconnect()

    async def send(
        self,
        content: MessageContent,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
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
                    chat_id=chat_id,
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

    async def upload_image(self, path: str | Path) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            raise RuntimeError("Not connected — connect the gateway client before uploading images.")

        upload_path = Path(path).expanduser().resolve()
        if not upload_path.is_file():
            raise FileNotFoundError(upload_path)

        import aiohttp

        form = aiohttp.FormData()
        with upload_path.open("rb") as handle:
            form.add_field("file", handle, filename=upload_path.name)
            async with self._session.post(f"{self._http_base_url}/api/upload-image", data=form) as response:
                payload = await response.json()

        if response.status >= 400 or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or f"Upload failed with HTTP {response.status}")

        return payload["part"]

    async def list_actors(self) -> dict[str, dict[str, Any]]:
        """Fetch the list of available named actors from the server."""
        if self._session is None or self._session.closed:
            raise RuntimeError("Not connected — connect the gateway client before listing actors.")
        async with self._session.get(f"{self._http_base_url}/api/actors") as response:
            payload = await response.json()
        if response.status >= 400:
            raise RuntimeError(payload.get("error") or f"List actors failed with HTTP {response.status}")
        return payload.get("actors", {})

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

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
