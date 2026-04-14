"""HttpChannel — aiohttp HTTP/WebSocket server bridging external clients to the mailbox.

Runs inside the actor process, shares the harness mailbox. Supports:

WebSocket
---------
``WS /ws`` — bidirectional envelope bridge. Handles three envelope types:

- ``content_type="message"`` — normal chat messages routed to/from the agent.
- ``content_type="command"`` — slash commands (e.g. ``/history``) executed
  server-side using the harness stores. Results are sent back as
  ``content_type="command_result"`` envelopes.
- ``content_type="agent_step"`` — real-time step info forwarded to the TUI.

REST
----
``POST /api/send``    One-shot fire-and-forget.
``GET  /api/status``  JSON health check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType, web

from bos.core import Envelope, Mailbox, ep_channel

logger = logging.getLogger(__name__)

# ── helpers ────────────────────────────────────────────────────


def _envelope_from_dict(data: dict[str, Any], default_sender: str) -> Envelope:
    ts_raw = data.get("timestamp")
    ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.now()
    return Envelope(
        sender=data.get("sender", default_sender),
        recipient=data.get("recipient", "main"),
        content=data.get("content", ""),
        content_type=data.get("content_type", "message"),
        conversation_id=data.get("conversation_id"),
        timestamp=ts,
    )


def _envelope_to_dict(env: Envelope) -> dict[str, Any]:
    d = dataclasses.asdict(env)
    d["timestamp"] = env.timestamp.isoformat()
    return d


# ── slash command handler ──────────────────────────────────────


# ── WebSocket handler ──────────────────────────────────────────


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Bidirectional WebSocket bridge between an external client and the mailbox."""
    mailbox: Mailbox = request.app["mailbox"]
    channel_address: str = request.app["channel_address"]

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    logger.debug("WebSocket client connected from %s", request.remote)

    # Forward replies from mailbox → WS client
    async def _forward_to_ws() -> None:
        while not ws.closed:
            try:
                env = await mailbox.receive(channel_address)
                await ws.send_json(_envelope_to_dict(env))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("WS forward error: %s", exc)
                break

    forward_task = asyncio.create_task(_forward_to_ws())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    env = _envelope_from_dict(data, default_sender=channel_address)
                    await mailbox.send(env)
                except Exception as exc:
                    logger.warning("Bad WS message: %s — %s", msg.data[:120], exc)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        forward_task.cancel()
        await asyncio.gather(forward_task, return_exceptions=True)
        logger.debug("WebSocket client disconnected")

    return ws


# ── REST handlers ──────────────────────────────────────────────


async def _send_handler(request: web.Request) -> web.Response:
    """POST /api/send — one-shot fire-and-forget."""
    mailbox: Mailbox = request.app["mailbox"]
    channel_address: str = request.app["channel_address"]
    try:
        data = await request.json()
        env = _envelope_from_dict(data, default_sender=channel_address)
        await mailbox.send(env)
        return web.json_response({"ok": True}, status=202)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)


async def _status_handler(request: web.Request) -> web.Response:
    """GET /api/status — lightweight health check."""
    info: dict[str, Any] = request.app.get("status_info", {})
    return web.json_response({"ok": True, **info})


# ── HttpChannel (server) ───────────────────────────────────────


@ep_channel(name="HttpChannel")
class HttpChannel:
    """aiohttp HTTP/WebSocket channel server registered on ``ep_channel``.

    Binds to ``host:port`` and bridges external WebSocket clients to/from
    the shared harness mailbox via ``address``.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8080, **_: Any) -> None:
        self._host = os.environ.get("BOS_CHANNEL_HOST", host)
        self._port = int(os.environ.get("BOS_CHANNEL_PORT", port))
        self.actual_host: str = self._host
        self.actual_port: int = self._port

    async def run(self, mailbox: Mailbox, address: str) -> None:  # noqa: D102
        app = web.Application()
        app["mailbox"] = mailbox
        app["channel_address"] = address
        app["status_info"] = {
            "channel": "HttpChannel",
            "address": address,
            "started_at": datetime.now().isoformat(),
        }

        app.router.add_get("/ws", _ws_handler)
        app.router.add_post("/api/send", _send_handler)
        app.router.add_get("/api/status", _status_handler)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()

        # Discover actual port (important when port=0 was given)
        actual_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.actual_port = actual_port
        self.actual_host = self._host
        logger.info(
            "HttpChannel listening on %s:%d (mailbox address=%r)",
            self._host,
            actual_port,
            address,
        )

        try:
            await asyncio.Event().wait()  # hold forever until task is cancelled
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()
            logger.info("HttpChannel stopped")

    async def aclose(self) -> None:  # noqa: D102
        pass  # cleanup is handled inside run()
