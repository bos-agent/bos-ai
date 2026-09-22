from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from .config import ResolvedGatewayConfig

JsonDict = dict[str, Any]
StatusProvider = Callable[[], JsonDict]
# A provider, not a value: a mounted gateway is replaced wholesale on restart
# while the app it serves through is built once (BEP 17 §3.3.3), so the app must
# read the settings of whichever Gateway is current rather than close over one.
ConfigProvider = Callable[[], ResolvedGatewayConfig]
WSHandler = Callable[[WebSocket], Awaitable[None]]


class _LiveBodyLimit:
    """Cap each request body at the *current* ``max_upload_bytes``.

    Starlette fixes its own limit at construction — on the app, on a route, or
    on the middleware — but this app is built once, before ``start()``, and
    outlives every ``Gateway`` behind it (BEP 17 §3.3.3). At build time there
    may be no gateway at all, so a limit read then would be the dataclass
    default rather than the configured one. Reading it per request is what makes
    the configured value, and a restart that changes it, take effect.

    A pure ASGI callable, not a ``BaseHTTPMiddleware`` subclass: that class
    returns early on any non-``http`` scope, so anything built on it silently
    skips ``/ws`` (BEP 17 §3.6.2). ``RequestBodyLimitMiddleware`` passes
    non-``http`` scopes through itself, which is the correct behaviour here.
    """

    def __init__(self, app: ASGIApp, config_provider: ConfigProvider) -> None:
        self.app = app
        self._config_provider = config_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._config_provider().max_upload_bytes
        await RequestBodyLimitMiddleware(self.app, max_body_size=limit)(scope, receive, send)


async def send_ws_denial(websocket: WebSocket, status: int, payload: JsonDict) -> None:
    """Reject a websocket *before* the upgrade, keeping the HTTP status and body.

    The ASGI websocket denial-response extension is optional. uvicorn advertises
    it — measured: a 409 reaches a ``websockets`` client intact, readable from
    ``InvalidStatus.response`` — but ``send_denial_response`` raises
    ``RuntimeError`` on a server that does not, so an arbitrary host falls back
    to a private-use close code carrying the status (BEP 17 §3.6.4). Those codes
    cannot collide with ``WS_TAKEOVER_CLOSE_CODE`` (4001).
    """
    try:
        await websocket.send_denial_response(JSONResponse(payload, status_code=status))
    except RuntimeError:
        await websocket.close(code=4000 + status)


def create_gateway_app(
    *,
    config_provider: ConfigProvider,
    status_provider: StatusProvider,
    ws_handler: WSHandler | None = None,
) -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/status", _status_handler),
            Route("/api/actors", _actors_handler),
            Route("/api/upload-image", _upload_attachment_handler, methods=["POST"]),
            Route("/api/upload", _upload_attachment_handler, methods=["POST"]),
            WebSocketRoute("/ws", _ws_handler),
        ],
        middleware=[Middleware(_LiveBodyLimit, config_provider=config_provider)],
    )
    app.state.gateway_config = config_provider
    app.state.status_provider = status_provider
    app.state.ws_handler = ws_handler
    return app


async def _status_handler(request: Request) -> JSONResponse:
    provider: StatusProvider = request.app.state.status_provider
    return JSONResponse({"ok": True, **provider()})


async def _actors_handler(request: Request) -> JSONResponse:
    provider: StatusProvider = request.app.state.status_provider
    return JSONResponse({"ok": True, "actors": provider().get("actors", {})})


async def _upload_attachment_handler(request: Request) -> JSONResponse:
    config: ResolvedGatewayConfig = request.app.state.gateway_config()
    try:
        # The handler reads exactly one field named "file", so one part and one
        # field is the whole contract. ``max_part_size`` bounds a *non-file*
        # part only — starlette streams a part carrying a filename to a spooled
        # temporary file instead of accumulating it, so a file upload never
        # trips that check. What enforces ``max_upload_bytes`` is
        # ``_LiveBodyLimit``, which bounds the whole body and answers 413 from
        # Content-Length before the body is read.
        async with request.form(max_files=1, max_fields=1, max_part_size=config.max_upload_bytes) as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                return JSONResponse({"ok": False, "error": "Expected multipart field 'file'."}, status_code=400)
            part = store_uploaded_attachment(
                upload_dir=Path(config.upload_dir),
                filename=upload.filename or "attachment",
                content_type=upload.content_type,
                data=await upload.read(),
            )
        return JSONResponse({"ok": True, "part": part}, status_code=201)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


async def _ws_handler(websocket: WebSocket) -> None:
    handler: WSHandler | None = websocket.app.state.ws_handler
    if handler is None:
        await send_ws_denial(websocket, 501, {"ok": False, "error": "ws_not_implemented"})
        return
    await handler(websocket)


def store_uploaded_attachment(
    *, upload_dir: Path, filename: str, content_type: str | None, data: bytes
) -> dict[str, Any]:
    """Persist any inbound attachment to the upload dir under a unique name.

    Images become a native ``image`` part (handled by the vision path); every
    other MIME type becomes a ``file`` part carrying the absolute path and mime
    type, which the agent resolves with its filesystem tools. No MIME type is
    rejected — the input route never blocks an attachment from reaching the LLM.
    """
    if not data:
        raise ValueError("Uploaded attachment is empty.")
    safe_name = Path(filename or "attachment").name
    mime_type = (content_type or "").strip() or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    suffix = mimetypes.guess_extension(mime_type, strict=False) or Path(safe_name).suffix or ".bin"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = (upload_dir / f"{uuid.uuid4().hex}{suffix}").resolve()
    stored_path.write_bytes(data)
    if mime_type.startswith("image/"):
        return {"type": "image", "source": {"kind": "path", "value": str(stored_path)}}
    return {"type": "file", "mime_type": mime_type, "source": {"kind": "path", "value": str(stored_path)}}
