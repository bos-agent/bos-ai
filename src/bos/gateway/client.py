"""GatewayClient — WebSocket client for connecting to a running BOS gateway.

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

from bos.core.actor import Envelope, MessageType
from bos.core.agent import MessageContent

from .channels.ws_channel import WS_MAX_MESSAGE_BYTES, WS_TAKEOVER_CLOSE_CODE, WS_TAKEOVER_CLOSE_REASON

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


class GatewayClient:
    """WebSocket client for connecting to a running BOS gateway.

    Used by ``boscli tui`` to send/receive envelopes over WebSocket without
    direct mailbox access or any server-side imports.

    Automatically reconnects when the WebSocket connection drops (e.g. after
    an agent restart), with exponential backoff.

    Example::

        client = GatewayClient(host="127.0.0.1", port=5920, address="tui")
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
        chat_id: str | None = None,
        endpoint_resolver: EndpointResolver | None = None,
        workdir: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._endpoint_resolver = endpoint_resolver
        self._rebuild_urls()
        self._address = address
        self._channel_id = (channel_id or address or uuid.uuid4().hex).strip()
        self._workdir = workdir or None
        self._chat_id = chat_id.strip() if isinstance(chat_id, str) and chat_id else None
        self._current_revision = 0
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
        from websockets.protocol import State

        return self._ws is not None and self._ws.state is State.OPEN

    @property
    def client_id(self) -> str:
        return self._channel_id

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def chat_id(self) -> str | None:
        return self._chat_id

    @property
    def current_revision(self) -> int:
        return self._current_revision

    def update_chat_id(self, chat_id: str) -> None:
        if not chat_id:
            raise ValueError("chat_id must be non-empty.")
        self._chat_id = chat_id

    @property
    def workdir(self) -> str | None:
        return self._workdir

    def update_workdir(self, workdir: str | None) -> None:
        """Set or clear the workdir stamped on outgoing message metadata.

        When cleared, the gateway stamps its own workspace as the fallback.
        """
        self._workdir = (workdir or "").strip() or None

    async def connect(self, *, takeover: bool = False) -> None:
        """Open the WebSocket connection and start the background reader."""
        await self._do_connect(takeover=takeover)
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.debug("GatewayClient connected to %s (address=%r)", self._url, self._address)

    async def _do_connect(self, *, takeover: bool = False) -> None:
        """Low-level connect (or reconnect). Creates the HTTP client + WS."""
        import httpx
        from websockets.asyncio.client import connect

        await self._close_transport()  # drop any previous transport

        # The trailing slash is load-bearing: httpx resolves a relative path
        # against base_url by URL rules, so a base without it has its last
        # segment replaced rather than extended — not how aiohttp behaved.
        self._session = httpx.AsyncClient(base_url=f"{self._http_base_url}/")
        query: dict[str, str] = {"channel_id": self._channel_id}
        if self._chat_id:
            query["chat_id"] = self._chat_id
        if takeover:
            query["takeover"] = "1"
        url = f"{self._url}?{urlencode(query)}"
        try:
            self._ws = await connect(url, max_size=WS_MAX_MESSAGE_BYTES)
            await self._receive_session_ack()
        except BaseException:
            # A failed connect owns the transport it just opened — close it here
            # so callers don't have to know aclose() is needed after connect()
            # raised. BaseException, not Exception: a cancelled connect leaks
            # just the same.
            await self._close_transport()
            raise
        self._connected.set()

    async def _receive_session_ack(self) -> None:
        raw = await asyncio.wait_for(self._ws.recv(), 5)
        if not isinstance(raw, str):
            raise RuntimeError("Gateway did not send session acknowledgement.")
        data = json.loads(raw)
        metadata = data.get("metadata") or {}
        if data.get("content_type") != MessageType.SYSTEM or metadata.get("event") != "session":
            raise RuntimeError("Gateway sent an invalid session acknowledgement.")
        channel_id = metadata.get("channel_id")
        chat_id = metadata.get("chat_id") or data.get("chat_id")
        if isinstance(channel_id, str) and channel_id:
            self._channel_id = channel_id
        if isinstance(chat_id, str) and chat_id:
            self._chat_id = chat_id
        self._ingest_revision(metadata)
        # Forward the ack to the consumer so it can hydrate its transcript
        # view; the ack metadata carries the chat's full message history.
        await self._recv_queue.put(
            Envelope(
                sender=data.get("sender", "channel@gateway"),
                recipient=self._address,
                content=data.get("content", "connected"),
                content_type=MessageType.SYSTEM,
                chat_id=self._chat_id,
                metadata=metadata,
            )
        )

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

    def _takeover_closed(self) -> bool:
        """True when the gateway closed this session for a newer channel.

        ``websockets`` reports the close code on the connection rather than as a
        message, so both the normal end of the iterator and a ConnectionClosed
        land here.
        """
        return getattr(self._ws, "close_code", None) == WS_TAKEOVER_CLOSE_CODE

    async def _reader_loop(self) -> None:
        """Background reader: reads WS messages and reconnects on drop."""
        from websockets.exceptions import ConnectionClosed

        while not self._closed:
            should_reconnect = True
            try:
                await self._connected.wait()
                async for raw in self._ws:
                    try:
                        data = json.loads(raw)
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
                        self._ingest_revision(env.metadata)
                        await self._recv_queue.put(env)
                    except Exception as exc:
                        logger.debug("Client reader error: %s", exc)
                if not self._closed and self._takeover_closed():
                    should_reconnect = False
                    await self._emit_takeover_system_event()
            except asyncio.CancelledError:
                break
            except ConnectionClosed:
                if not self._closed and self._takeover_closed():
                    should_reconnect = False
                    await self._emit_takeover_system_event()
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
        out_metadata = dict(metadata or {})
        out_metadata.setdefault("base_revision", self._current_revision)
        if self._workdir:
            # The client's working directory; the gateway keeps it if present,
            # otherwise it stamps its own workspace as the fallback.
            out_metadata.setdefault("workdir", self._workdir)
        await self._ws.send(
            json.dumps(
                _envelope_to_dict(
                    Envelope(
                        sender=self._address,
                        recipient="",
                        content=content,
                        content_type=content_type,
                        chat_id=chat_id or self._chat_id,
                        metadata=out_metadata,
                    )
                ),
                default=str,
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

    async def upload_attachment(self, path: str | Path) -> dict[str, Any]:
        if self._session is None or self._session.is_closed:
            raise RuntimeError("Not connected — connect the gateway client before uploading attachments.")

        upload_path = Path(path).expanduser().resolve()
        if not upload_path.is_file():
            raise FileNotFoundError(upload_path)

        with upload_path.open("rb") as handle:
            # No leading slash: httpx resolves against base_url by URL rules, and
            # "/api/upload" would discard whatever path the base carries.
            response = await self._session.post("api/upload", files={"file": (upload_path.name, handle)})
        payload = response.json()

        if response.status_code >= 400 or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or f"Upload failed with HTTP {response.status_code}")

        return payload["part"]

    async def list_actors(self) -> dict[str, dict[str, Any]]:
        """Fetch the list of available actors from the gateway."""
        if self._session is None or self._session.is_closed:
            raise RuntimeError("Not connected — connect the gateway client before listing actors.")
        response = await self._session.get("api/actors")
        payload = response.json()
        if response.status_code >= 400:
            raise RuntimeError(payload.get("error") or f"List actors failed with HTTP {response.status_code}")
        return payload.get("actors", {})

    def _ingest_revision(self, metadata: dict[str, Any]) -> None:
        revision = _coerce_revision(metadata.get("current_revision"))
        if revision is None:
            payload = metadata.get("payload")
            if isinstance(payload, dict):
                revision = _coerce_revision(payload.get("current_revision"))
        if revision is not None:
            self._current_revision = revision

    async def aclose(self) -> None:
        """Close the WebSocket connection and clean up."""
        self._closed = True
        self._connected.set()  # unblock anything waiting on reconnect
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        await self._close_transport()
        logger.debug("GatewayClient disconnected")

    async def _close_transport(self) -> None:
        """Close the WS and the HTTP client if open, and drop both references.

        ``websockets``' close() is idempotent, unlike starlette's server-side
        one, so no state guard is needed here."""
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None and not self._session.is_closed:
            await self._session.aclose()
        self._ws = None
        self._session = None


def _coerce_revision(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None
