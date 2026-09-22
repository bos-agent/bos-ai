from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web

from .config import ResolvedGatewayConfig

JsonDict = dict[str, Any]
StatusProvider = Callable[[], JsonDict]
# A provider, not a value: a mounted gateway is replaced wholesale on restart
# while the app it serves through is built once (BEP 17 §3.3.3), so the app must
# read the settings of whichever Gateway is current rather than close over one.
ConfigProvider = Callable[[], ResolvedGatewayConfig]
WSHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
APP_GATEWAY_CONFIG: web.AppKey[ConfigProvider] = web.AppKey("gateway_config")
APP_STATUS_PROVIDER: web.AppKey[StatusProvider] = web.AppKey("status_provider")
APP_WS_HANDLER: web.AppKey[WSHandler] = web.AppKey("ws_handler")



def create_gateway_app(
    *,
    config_provider: ConfigProvider,
    status_provider: StatusProvider,
    ws_handler: WSHandler | None = None,
) -> web.Application:
    # ``client_max_size`` is fixed at construction by aiohttp, so it is read
    # once here; every per-request setting goes through the provider.
    app = web.Application(client_max_size=config_provider().max_upload_bytes)
    app[APP_GATEWAY_CONFIG] = config_provider
    app[APP_STATUS_PROVIDER] = status_provider
    if ws_handler is not None:
        app[APP_WS_HANDLER] = ws_handler
    app.router.add_get("/api/status", _status_handler)
    app.router.add_get("/api/actors", _actors_handler)
    app.router.add_post("/api/upload-image", _upload_attachment_handler)
    app.router.add_post("/api/upload", _upload_attachment_handler)
    app.router.add_get("/ws", _ws_handler)
    return app


async def _status_handler(request: web.Request) -> web.Response:
    provider = request.app[APP_STATUS_PROVIDER]
    return web.json_response({"ok": True, **provider()})


async def _actors_handler(request: web.Request) -> web.Response:
    provider = request.app[APP_STATUS_PROVIDER]
    status = provider()
    return web.json_response({"ok": True, "actors": status.get("actors", {})})


async def _upload_attachment_handler(request: web.Request) -> web.Response:
    config = request.app[APP_GATEWAY_CONFIG]()
    reader = await request.multipart()
    file_field = await reader.next()
    if not isinstance(file_field, BodyPartReader) or file_field.name != "file":
        return web.json_response({"ok": False, "error": "Expected multipart field 'file'."}, status=400)
    try:
        data = await file_field.read()
        part = store_uploaded_attachment(
            upload_dir=Path(config.upload_dir),
            filename=file_field.filename or "attachment",
            content_type=file_field.headers.get("Content-Type"),
            data=data,
        )
        return web.json_response({"ok": True, "part": part}, status=201)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)


async def _ws_handler(request: web.Request) -> web.StreamResponse:
    handler = request.app.get(APP_WS_HANDLER)
    if handler is not None:
        return await handler(request)
    return web.json_response({"ok": False, "error": "ws_not_implemented"}, status=501)


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
