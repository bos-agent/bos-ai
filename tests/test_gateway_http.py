import httpx
import pytest

from bos.core import BaseChannel, ep_channel
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import Gateway, ResolvedGatewayConfig
from bos.gateway.http import create_gateway_app
from bos.gateway.state import GatewayRunDir, read_gateway_state, write_gateway_state


def _app(tmp_path, *, max_upload_bytes: int = 20 * 1024 * 1024):
    config = ResolvedGatewayConfig(upload_dir=str(tmp_path / "uploads"), max_upload_bytes=max_upload_bytes)
    return create_gateway_app(
        config_provider=lambda: config,
        status_provider=lambda: {
            "runtime": "process",
            "gateway": {},
            "actors": {"main": {"display_name": "Main"}},
            "channels": {},
            "active_turns": {},
        },
    )


def _client(app) -> httpx.AsyncClient:
    """Drive the app in-process. ASGITransport runs the whole stack, middleware
    included, so the body-limit path is exercised without binding a socket."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


@ep_channel(name="GatewayStatusTestChannel")
class GatewayStatusTestChannel(BaseChannel[dict]):
    async def run(self, mailbox):
        raise AssertionError("not started by status_snapshot")


class _FakeMailRoute:
    def bind(self, address: str):
        return object()

    async def deliver(self, env):
        raise AssertionError("not used")


class _FakeHarness:
    chat_store = InMemChatStore()
    mail_route = _FakeMailRoute()


@pytest.mark.asyncio
async def test_gateway_status_uses_channel_manager_payload():
    from bos.config import Workspace

    ws = Workspace(
        ".",
        ".bos",
        {
            "runtime": {
                "main_actor": "main",
                "actors": {"main": {"agent": "main"}},
                "channels": [
                    {
                        "type": "GatewayStatusTestChannel",
                        "channel_id": "demo",
                        "display_name": "Demo",
                        "settings": {},
                    }
                ],
            }
        },
    )

    gateway = Gateway(runtime=ws.resolve_gateway_runtime(), harness=_FakeHarness())
    # Persistent channels are instantiated by start(); replicate that step only.
    await gateway.channel_manager.create_persistent(gateway._persistent_channel_configs)
    snapshot = gateway.status_snapshot()

    assert snapshot["channels"]["demo"]["type"] == "GatewayStatusTestChannel"
    assert snapshot["channels"]["demo"]["address"] == "channel@demo"


@pytest.mark.asyncio
async def test_gateway_status_is_served_without_authentication(tmp_path):
    async with _client(_app(tmp_path)) as client:
        response = await client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["actors"]["main"]["display_name"] == "Main"


@pytest.mark.asyncio
async def test_gateway_actors_route_projects_the_status_payload(tmp_path):
    async with _client(_app(tmp_path)) as client:
        response = await client.get("/api/actors")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "actors": {"main": {"display_name": "Main"}}}


@pytest.mark.asyncio
async def test_gateway_upload_image_returns_path_part(tmp_path):
    async with _client(_app(tmp_path)) as client:
        response = await client.post(
            "/api/upload-image",
            files={"file": ("cat.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ok"] is True
    assert payload["part"]["type"] == "image"
    assert payload["part"]["source"]["kind"] == "path"


@pytest.mark.asyncio
async def test_gateway_upload_non_image_returns_file_part(tmp_path):
    async with _client(_app(tmp_path)) as client:
        response = await client.post(
            "/api/upload",
            files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )

    assert response.status_code == 201
    part = response.json()["part"]
    assert part["type"] == "file"
    assert part["mime_type"] == "application/pdf"
    assert part["source"]["kind"] == "path"
    assert part["source"]["value"].endswith(".pdf")


@pytest.mark.asyncio
async def test_gateway_upload_rejects_a_field_that_is_not_a_file(tmp_path):
    async with _client(_app(tmp_path)) as client:
        response = await client.post("/api/upload", data={"file": "not-a-file"})

    assert response.status_code == 400
    assert response.json()["ok"] is False


@pytest.mark.asyncio
async def test_gateway_upload_under_the_limit_succeeds(tmp_path):
    """15 MB against the 20 MB default (BEP 17 §7.7). The bound is the whole
    request body, as it was under aiohttp's client_max_size — not the part size,
    which starlette does not apply to a part carrying a filename."""
    async with _client(_app(tmp_path)) as client:
        response = await client.post(
            "/api/upload",
            files={"file": ("big.bin", b"\0" * (15 * 1024 * 1024), "application/octet-stream")},
        )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_gateway_upload_over_the_limit_is_rejected_with_413(tmp_path):
    async with _client(_app(tmp_path, max_upload_bytes=1024)) as client:
        response = await client.post(
            "/api/upload",
            files={"file": ("big.bin", b"\0" * 8192, "application/octet-stream")},
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_gateway_upload_limit_follows_the_config_provider(tmp_path):
    """The app is built once and outlives every Gateway behind it, so the limit
    cannot be captured at construction — a restart may change it, and at build
    time there may be no gateway to read at all (BEP 17 §3.3.3)."""
    holder = {"config": ResolvedGatewayConfig(upload_dir=str(tmp_path / "uploads"), max_upload_bytes=1024)}
    app = create_gateway_app(
        config_provider=lambda: holder["config"],
        status_provider=lambda: {"actors": {}},
    )
    async with _client(app) as client:
        too_big = await client.post("/api/upload", files={"file": ("a.bin", b"\0" * 8192)})
        assert too_big.status_code == 413

        holder["config"] = ResolvedGatewayConfig(
            upload_dir=str(tmp_path / "uploads"), max_upload_bytes=1024 * 1024
        )
        now_fine = await client.post("/api/upload", files={"file": ("a.bin", b"\0" * 8192)})
        assert now_fine.status_code == 201


def test_gateway_state_round_trips_without_secrets(tmp_path):
    run_dir = GatewayRunDir(tmp_path / ".bos")
    snapshot = {
        "runtime": "process",
        "gateway": {"host": "127.0.0.1", "port": 5920},
        "actors": {},
        "channels": {},
        "active_turns": {},
    }

    write_gateway_state(run_dir, snapshot)

    assert read_gateway_state(run_dir)["gateway"]["port"] == 5920
    assert "secret" not in run_dir.state_file.read_text(encoding="utf-8").lower()
