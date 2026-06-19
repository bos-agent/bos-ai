from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web

from bos.config.workspace import ResolvedGatewayConfig

JsonDict = dict[str, Any]
StatusProvider = Callable[[], JsonDict]
WSHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
APP_API_KEY = web.AppKey("api_key", str)
APP_GATEWAY_CONFIG = web.AppKey("gateway_config", ResolvedGatewayConfig)
APP_STATUS_PROVIDER: web.AppKey[StatusProvider] = web.AppKey("status_provider")
APP_WS_HANDLER: web.AppKey[WSHandler] = web.AppKey("ws_handler")


def resolve_gateway_api_key(config: ResolvedGatewayConfig, environ: dict[str, str] | None = None) -> str | None:
    """Return the configured gateway API key, or ``None`` when auth is disabled.

    The key is optional: when the environment variable is unset or empty the
    gateway runs without authentication (a hosting layer may enforce its own).
    """
    import os

    env = os.environ if environ is None else environ
    api_key = env.get(config.api_key_env, "").strip()
    return api_key or None


@web.middleware
async def api_key_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    api_key: str = request.app[APP_API_KEY]
    if api_key and request.headers.get("Authorization") != f"Bearer {api_key}":
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return await handler(request)


def create_gateway_app(
    *,
    config: ResolvedGatewayConfig,
    api_key: str | None,
    status_provider: StatusProvider,
    ws_handler: WSHandler | None = None,
) -> web.Application:
    app = web.Application(client_max_size=config.max_upload_bytes, middlewares=[api_key_middleware])
    app[APP_API_KEY] = api_key or ""
    app[APP_GATEWAY_CONFIG] = config
    app[APP_STATUS_PROVIDER] = status_provider
    if ws_handler is not None:
        app[APP_WS_HANDLER] = ws_handler
    app.router.add_get("/api/status", _status_handler)
    app.router.add_get("/api/actors", _actors_handler)
    app.router.add_post("/api/upload-image", _upload_image_handler)
    app.router.add_get("/ws", _ws_handler)
    return app


async def _status_handler(request: web.Request) -> web.Response:
    provider = request.app[APP_STATUS_PROVIDER]
    return web.json_response({"ok": True, **provider()})


async def _actors_handler(request: web.Request) -> web.Response:
    provider = request.app[APP_STATUS_PROVIDER]
    status = provider()
    return web.json_response({"ok": True, "actors": status.get("actors", {})})


async def _upload_image_handler(request: web.Request) -> web.Response:
    config: ResolvedGatewayConfig = request.app[APP_GATEWAY_CONFIG]
    reader = await request.multipart()
    file_field = await reader.next()
    if not isinstance(file_field, BodyPartReader) or file_field.name != "file":
        return web.json_response({"ok": False, "error": "Expected multipart field 'file'."}, status=400)
    try:
        data = await file_field.read()
        part = store_uploaded_image(
            upload_dir=Path(config.upload_dir),
            filename=file_field.filename or "image",
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


def store_uploaded_image(*, upload_dir: Path, filename: str, content_type: str | None, data: bytes) -> dict[str, Any]:
    if not data:
        raise ValueError("Uploaded image is empty.")
    safe_name = Path(filename or "image").name
    mime_type = (content_type or "").strip() or mimetypes.guess_type(safe_name)[0] or ""
    if not mime_type.startswith("image/"):
        raise ValueError("Uploaded file must be an image.")
    suffix = mimetypes.guess_extension(mime_type, strict=False) or ".img"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = (upload_dir / f"{uuid.uuid4().hex}{suffix}").resolve()
    stored_path.write_bytes(data)
    return {"type": "image", "source": {"kind": "path", "value": str(stored_path)}}
