import aiohttp
import pytest
from aiohttp import FormData, web

from bos.core import BaseChannel, ep_channel
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import Gateway, ResolvedGatewayConfig
from bos.gateway.http import create_gateway_app
from bos.gateway.state import GatewayRunDir, read_gateway_state, write_gateway_state


async def _start_app(app: web.Application):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def _app(tmp_path) -> web.Application:
    config = ResolvedGatewayConfig(upload_dir=str(tmp_path / "uploads"))
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
    # Persistent channels are instantiated by run(); replicate that step only.
    await gateway.channel_manager.create_persistent(gateway._persistent_channel_configs)
    snapshot = gateway.status_snapshot()

    assert snapshot["channels"]["demo"]["type"] == "GatewayStatusTestChannel"
    assert snapshot["channels"]["demo"]["address"] == "channel@demo"


@pytest.mark.asyncio
async def test_gateway_upload_image_returns_path_part(tmp_path):
    runner, base_url = await _start_app(_app(tmp_path))
    try:
        form = FormData()
        form.add_field("file", b"\x89PNG\r\n\x1a\nfake", filename="cat.png", content_type="image/png")
        async with aiohttp.ClientSession() as session:
            response = await session.post(
                f"{base_url}/api/upload-image",
                data=form,
            )
            payload = await response.json()

        assert response.status == 201
        assert payload["ok"] is True
        assert payload["part"]["type"] == "image"
        assert payload["part"]["source"]["kind"] == "path"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_upload_non_image_returns_file_part(tmp_path):
    runner, base_url = await _start_app(_app(tmp_path))
    try:
        form = FormData()
        form.add_field("file", b"%PDF-1.4 fake", filename="report.pdf", content_type="application/pdf")
        async with aiohttp.ClientSession() as session:
            response = await session.post(
                f"{base_url}/api/upload",
                data=form,
            )
            payload = await response.json()

        assert response.status == 201
        assert payload["ok"] is True
        part = payload["part"]
        assert part["type"] == "file"
        assert part["mime_type"] == "application/pdf"
        assert part["source"]["kind"] == "path"
        assert part["source"]["value"].endswith(".pdf")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ws_endpoint_reports_not_implemented_without_a_handler(tmp_path):
    runner, base_url = await _start_app(_app(tmp_path))
    try:
        async with aiohttp.ClientSession() as session:
            response = await session.get(f"{base_url}/ws")

            assert response.status == 501
            assert (await response.json())["error"] == "ws_not_implemented"
    finally:
        await runner.cleanup()


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
