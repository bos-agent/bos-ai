"""HttpChannel — aiohttp HTTP/WebSocket server bridging external clients to a bound mailbox.

Runs inside the actor process and shares a bound channel mailbox. Supports:

WebSocket
---------
``WS /ws`` — bidirectional envelope bridge. Handles three envelope types:

- ``content_type="message"`` — normal chat messages routed to/from the agent.
- ``content_type="command"`` — slash commands (e.g. ``/history``) executed
  server-side using the harness stores. Results are sent back as
  ``content_type="command_result"`` envelopes.
- ``content_type="turn_event"`` — structured runtime events forwarded to frontends.

REST
----
``POST /api/send``    One-shot fire-and-forget.
``POST /api/upload-image`` Upload an image and return a BOS path-backed image part.
``GET  /api/status``  JSON health check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from bos.core import MailBox, ep_channel
from bos.core.chat_state import ChatState
from bos.protocol import Envelope, MessageType

logger = logging.getLogger(__name__)

WS_TAKEOVER_CLOSE_CODE = 4001
WS_TAKEOVER_CLOSE_REASON = "Another interactive client took over this HttpChannel session."

APP_MAILBOX = web.AppKey("mailbox", MailBox)
APP_TARGET_ADDRESS = web.AppKey("target_address", str)
APP_ACTOR_REGISTRY = web.AppKey("actor_registry", object)
APP_UPLOAD_DIR = web.AppKey("upload_dir", Path)
APP_STATUS_INFO = web.AppKey("status_info", dict)
APP_RUNTIME_STATE = web.AppKey("runtime_state", dict)
APP_CHAT_STATE = web.AppKey("chat_state", ChatState)

# ── helpers ────────────────────────────────────────────────────


@dataclass
class _ClientConnection:
    client_id: str
    chat_id: str
    ws: web.WebSocketResponse


def _session_ack(*, channel_address: str, client_id: str, chat_id: str, resumed: bool) -> dict[str, Any]:
    return _envelope_to_dict(
        Envelope(
            sender=channel_address,
            recipient=client_id,
            content="Session resumed." if resumed else "Session started.",
            content_type=MessageType.SYSTEM,
            chat_id=chat_id,
            metadata={
                "event": "session",
                "client_id": client_id,
                "chat_id": chat_id,
                "resumed": resumed,
            },
        )
    )


def _merge_client_routing_metadata(
    metadata: dict[str, Any],
    *,
    client_id: str,
    chat_id: str,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    routing = dict(merged.get("routing") or {})
    routing["client_id"] = client_id
    routing["chat_id"] = chat_id
    merged["routing"] = routing
    return merged


def _selected_chat_from_command_result(env: Envelope) -> str | None:
    if env.content_type != MessageType.COMMAND_RESULT:
        return None
    try:
        payload = json.loads(env.content) if isinstance(env.content, str) else env.content
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    name = payload.get("name")
    if name not in {"new", "resume"}:
        return None
    chat_id = payload.get("chat_id")
    return chat_id if isinstance(chat_id, str) and chat_id.strip() else None


def _envelope_from_dict(data: dict[str, Any], *, sender: str, target: str) -> Envelope:
    ts_raw = data.get("timestamp")
    ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else datetime.now()
    return Envelope(
        sender=sender,
        recipient=target,
        content=data.get("content", ""),
        content_type=data.get("content_type", MessageType.MESSAGE),
        chat_id=data.get("chat_id"),
        timestamp=ts,
        metadata=data.get("metadata", {}),
    )


def _envelope_to_dict(env: Envelope) -> dict[str, Any]:
    d = dataclasses.asdict(env)
    d["timestamp"] = env.timestamp.isoformat()
    return d


def _store_uploaded_image(*, upload_dir: Path, filename: str, content_type: str | None, data: bytes) -> dict[str, Any]:
    if not data:
        raise ValueError("Uploaded image is empty.")

    safe_name = Path(filename or "image").name
    mime_type = (content_type or "").strip() or mimetypes.guess_type(safe_name)[0] or ""
    if not mime_type.startswith("image/"):
        raise ValueError("Uploaded file must be an image.")
    suffix = _guess_image_suffix(mime_type)

    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = (upload_dir / f"{uuid.uuid4().hex}{suffix}").resolve()
    stored_path.write_bytes(data)

    return {
        "type": "image",
        "source": {"kind": "path", "value": str(stored_path)},
    }


def _guess_image_suffix(content_type: str | None) -> str:
    guessed = mimetypes.guess_extension((content_type or "").strip(), strict=False)
    return guessed or ".img"


# ── slash command handler ──────────────────────────────────────


# ── WebSocket handler ──────────────────────────────────────────


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Bidirectional WebSocket bridge between an external client and the mailbox."""
    mailbox: MailBox = request.app[APP_MAILBOX]
    target_address: str = request.app[APP_TARGET_ADDRESS]
    registry = request.app.get(APP_ACTOR_REGISTRY)
    runtime_state = request.app[APP_RUNTIME_STATE]
    chat_state: ChatState = request.app[APP_CHAT_STATE]
    client_id = request.query.get("client_id", "").strip()
    if not client_id:
        raise web.HTTPBadRequest(text="HttpChannel websocket clients must provide client_id.")
    supplied_chat_id = request.query.get("chat_id", "").strip() or None
    had_cursor = chat_state.get_cursor(client_id) is not None
    chat_id = chat_state.resolve_for_client(client_id, supplied_chat_id)
    takeover = request.query.get("takeover", "").strip().lower() in {"1", "true", "yes", "on"}
    clients: dict[str, _ClientConnection] = runtime_state["clients"]
    current = clients.get(client_id)
    if current is not None and not current.ws.closed:
        if not takeover:
            raise web.HTTPConflict(text=f"HttpChannel already has an active websocket for client_id={client_id!r}.")
        await current.ws.close(
            code=WS_TAKEOVER_CLOSE_CODE,
            message=WS_TAKEOVER_CLOSE_REASON.encode(),
        )
    await _close_other_chat_clients(clients, chat_id, keep_client_id=client_id)

    ws = web.WebSocketResponse(heartbeat=30)
    conn = _ClientConnection(client_id=client_id, chat_id=chat_id, ws=ws)
    try:
        await ws.prepare(request)
        clients[client_id] = conn
        _ensure_dispatcher(request.app)
        await ws.send_json(
            _session_ack(
                channel_address=mailbox.address,
                client_id=client_id,
                chat_id=chat_id,
                resumed=bool(supplied_chat_id or had_cursor),
            )
        )
        logger.debug("WebSocket client connected from %s (client_id=%r)", request.remote, client_id)

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if not data.get("chat_id"):
                        data["chat_id"] = conn.chat_id
                    env = _envelope_from_dict(data, sender=mailbox.address, target=target_address)
                    if env.chat_id:
                        resolved_chat_id = chat_state.resolve_for_client(client_id, env.chat_id)
                        if resolved_chat_id != conn.chat_id:
                            await _close_other_chat_clients(
                                clients,
                                resolved_chat_id,
                                keep_client_id=client_id,
                            )
                        conn.chat_id = resolved_chat_id
                    metadata = _merge_client_routing_metadata(
                        env.metadata,
                        client_id=client_id,
                        chat_id=conn.chat_id,
                    )
                    if env.recipient.startswith("agent@"):
                        metadata.setdefault("target_actor", env.recipient.split("@", 1)[1])

                    recipient = env.recipient
                    content = env.content
                    if registry is not None:
                        route_result = registry.route(
                            str(content) if isinstance(content, str) else content,
                            metadata=metadata,
                        )
                        recipient = route_result.target_address
                        content = route_result.content
                        metadata = route_result.metadata

                    await mailbox.send(
                        recipient,
                        content,
                        content_type=env.content_type,
                        chat_id=conn.chat_id,
                        metadata=metadata,
                    )
                except Exception as exc:
                    logger.warning("Bad WS message: %s — %s", msg.data[:120], exc)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        if clients.get(client_id) is conn:
            clients.pop(client_id, None)
        logger.debug("WebSocket client disconnected (client_id=%r)", client_id)

    return ws


def _ensure_dispatcher(app: web.Application) -> None:
    runtime_state = app[APP_RUNTIME_STATE]
    task = runtime_state.get("dispatcher_task")
    if task is None or task.done():
        runtime_state["dispatcher_task"] = asyncio.create_task(_dispatch_to_clients(app))


async def _close_other_chat_clients(
    clients: dict[str, _ClientConnection],
    chat_id: str,
    *,
    keep_client_id: str,
) -> None:
    for other_client_id, other_conn in list(clients.items()):
        if other_client_id == keep_client_id:
            continue
        if other_conn.chat_id != chat_id or other_conn.ws.closed:
            continue
        await other_conn.ws.close(
            code=WS_TAKEOVER_CLOSE_CODE,
            message=WS_TAKEOVER_CLOSE_REASON.encode(),
        )


async def _dispatch_to_clients(app: web.Application) -> None:
    mailbox: MailBox = app[APP_MAILBOX]
    runtime_state = app[APP_RUNTIME_STATE]
    chat_state: ChatState = app[APP_CHAT_STATE]
    clients: dict[str, _ClientConnection] = runtime_state["clients"]
    try:
        while True:
            env = await mailbox.receive()
            stale: list[str] = []
            targets = _dispatch_targets(env, clients)
            selected_chat_id = _selected_chat_from_command_result(env)
            for client_id, conn in targets.items():
                if conn.ws.closed:
                    stale.append(client_id)
                    continue
                try:
                    out_env = env
                    if selected_chat_id:
                        await _close_other_chat_clients(
                            clients,
                            selected_chat_id,
                            keep_client_id=client_id,
                        )
                        conn.chat_id = selected_chat_id
                        chat_state.set_cursor(client_id, selected_chat_id)
                        out_env = dataclasses.replace(env, chat_id=selected_chat_id)
                    await conn.ws.send_json(_envelope_to_dict(out_env))
                except Exception as exc:
                    logger.debug("WS dispatch error for client_id=%r: %s", client_id, exc)
                    stale.append(client_id)
            for client_id in stale:
                if clients.get(client_id) and clients[client_id].ws.closed:
                    clients.pop(client_id, None)
    except asyncio.CancelledError:
        raise


def _dispatch_targets(
    env: Envelope,
    clients: dict[str, _ClientConnection],
) -> dict[str, _ClientConnection]:
    routing = env.metadata.get("routing")
    if isinstance(routing, dict):
        client_id = routing.get("client_id")
        if isinstance(client_id, str) and client_id in clients:
            return {client_id: clients[client_id]}

    if env.chat_id:
        matches = {
            client_id: conn
            for client_id, conn in clients.items()
            if conn.chat_id == env.chat_id and not conn.ws.closed
        }
        if len(matches) <= 1:
            return matches
        return {}

    return {}


async def _cleanup_runtime_state(app: web.Application) -> None:
    runtime_state = app[APP_RUNTIME_STATE]
    task = runtime_state.get("dispatcher_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    clients: dict[str, _ClientConnection] = runtime_state["clients"]
    for conn in list(clients.values()):
        if not conn.ws.closed:
            await conn.ws.close()
    clients.clear()


# ── REST handlers ──────────────────────────────────────────────


async def _send_handler(request: web.Request) -> web.Response:
    """POST /api/send — one-shot fire-and-forget."""
    mailbox: MailBox = request.app[APP_MAILBOX]
    target_address: str = request.app[APP_TARGET_ADDRESS]
    chat_state: ChatState = request.app[APP_CHAT_STATE]
    registry = request.app.get(APP_ACTOR_REGISTRY)
    try:
        data = await request.json()
        routing = data.get("metadata", {}).get("routing") if isinstance(data.get("metadata"), dict) else {}
        client_id = data.get("client_id")
        if not isinstance(client_id, str) and isinstance(routing, dict):
            client_id = routing.get("client_id")
        env = _envelope_from_dict(data, sender=mailbox.address, target=target_address)
        if client_id:
            chat_id = chat_state.resolve_for_client(str(client_id), env.chat_id)
            metadata = _merge_client_routing_metadata(
                env.metadata,
                client_id=str(client_id),
                chat_id=chat_id,
            )
        elif env.chat_id:
            chat_id = env.chat_id
            metadata = env.metadata
        else:
            raise ValueError("POST /api/send requires chat_id or client_id.")
        if env.recipient.startswith("agent@"):
            metadata.setdefault("target_actor", env.recipient.split("@", 1)[1])

        recipient = env.recipient
        content = env.content
        if registry is not None:
            route_result = registry.route(
                str(content) if isinstance(content, str) else content,
                metadata=metadata,
            )
            recipient = route_result.target_address
            content = route_result.content
            metadata = route_result.metadata

        await mailbox.send(
            recipient,
            content,
            content_type=env.content_type,
            chat_id=chat_id,
            metadata=metadata,
        )
        return web.json_response({"ok": True}, status=202)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)


async def _upload_image_handler(request: web.Request) -> web.Response:
    upload_dir: Path = request.app[APP_UPLOAD_DIR]
    try:
        data = await request.post()
        file_field = data.get("file")
        if file_field is None or not hasattr(file_field, "file"):
            raise ValueError("Expected multipart form field `file`.")

        part = _store_uploaded_image(
            upload_dir=upload_dir,
            filename=getattr(file_field, "filename", "") or "image",
            content_type=getattr(file_field, "content_type", None),
            data=file_field.file.read(),
        )
        return web.json_response({"ok": True, "part": part}, status=201)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)


async def _status_handler(request: web.Request) -> web.Response:
    """GET /api/status — lightweight health check."""
    info: dict[str, Any] = request.app.get(APP_STATUS_INFO, {})
    return web.json_response({"ok": True, **info})


# ── HttpChannel (server) ───────────────────────────────────────


@ep_channel(name="HttpChannel")
class HttpChannel:
    """aiohttp HTTP/WebSocket channel server registered on ``ep_channel``.

    Binds to ``host:port`` and bridges external WebSocket clients to/from
    the shared harness mail route via one bound channel mailbox.
    """

    def __init__(
        self,
        target_address: str,
        host: str = "127.0.0.1",
        port: int = 5920,
        upload_dir: str | Path | None = None,
        max_upload_bytes: int = 20 * 1024 * 1024,
        bos_dir: str | Path | None = None,
        chat_state_path: str | Path | None = None,
        actor_registry: Any = None,
    ) -> None:
        self._host = host
        self._port = int(port)
        self.actual_host: str = self._host
        self.actual_port: int = self._port
        self.target_address: str = target_address
        self._upload_dir = Path(upload_dir or ".bos/uploads/http").expanduser().resolve()
        self._max_upload_bytes = int(max_upload_bytes)
        self._chat_state = ChatState(bos_dir=bos_dir, path=chat_state_path)
        self._actor_registry = actor_registry

    def _build_app(self, mailbox: MailBox) -> web.Application:
        address = mailbox.address
        target = self.target_address or address
        app = web.Application(client_max_size=self._max_upload_bytes)
        app[APP_MAILBOX] = mailbox
        app[APP_TARGET_ADDRESS] = target
        app[APP_ACTOR_REGISTRY] = self._actor_registry
        app[APP_UPLOAD_DIR] = self._upload_dir
        app[APP_CHAT_STATE] = self._chat_state
        app[APP_RUNTIME_STATE] = {"clients": {}, "dispatcher_task": None}
        app[APP_STATUS_INFO] = {
            "channel": "HttpChannel",
            "address": address,
            "target_address": target,
            "upload_dir": str(self._upload_dir),
            "max_upload_bytes": self._max_upload_bytes,
            "interactive_ws_clients_supported": "one_active_client_per_chat",
            "interactive_ws_takeover_supported": "per_client_id",
            "started_at": datetime.now().isoformat(),
        }

        app.router.add_get("/ws", _ws_handler)
        app.router.add_post("/api/send", _send_handler)
        app.router.add_post("/api/upload-image", _upload_image_handler)
        app.router.add_get("/api/status", _status_handler)
        app.on_cleanup.append(_cleanup_runtime_state)
        return app

    async def run(self, mailbox: MailBox) -> None:
        address = mailbox.address
        app = self._build_app(mailbox)

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
