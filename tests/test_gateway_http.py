import aiohttp
import pytest
from aiohttp import FormData, web

from bos.config.workspace import ResolvedGatewayConfig
from bos.core import BaseChannel, ep_channel
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import Gateway
from bos.gateway.http import create_gateway_app, require_gateway_api_key
from bos.gateway.state import GatewayRunDir, read_gateway_state, write_gateway_state


async def _start_app(app: web.Application):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def _app(tmp_path, *, api_key: str = "secret") -> web.Application:
    config = ResolvedGatewayConfig(upload_dir=str(tmp_path / "uploads"))
    return create_gateway_app(
        config=config,
        api_key=api_key,
        status_provider=lambda: {
            "runtime": "process",
            "gateway": {"auth": {"type": "api_key", "configured": True}},
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


def test_gateway_status_uses_channel_manager_payload(monkeypatch):
    monkeypatch.setenv("BOS_GATEWAY_API_KEY", "secret")
    from bos.config import Workspace

    ws = Workspace(
        ".",
        ".bos",
        {
            "runtime": {
                "default_actor": "main",
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

    snapshot = Gateway(workspace=ws, harness=_FakeHarness()).status_snapshot()

    assert snapshot["channels"]["demo"]["type"] == "GatewayStatusTestChannel"
    assert snapshot["channels"]["demo"]["address"] == "channel@demo"


def test_require_gateway_api_key_fails_when_env_missing():
    config = ResolvedGatewayConfig(api_key_env="MISSING_KEY")

    with pytest.raises(RuntimeError, match="MISSING_KEY"):
        require_gateway_api_key(config, environ={})


@pytest.mark.asyncio
async def test_gateway_status_requires_bearer_auth(tmp_path):
    runner, base_url = await _start_app(_app(tmp_path))
    try:
        async with aiohttp.ClientSession() as session:
            missing = await session.get(f"{base_url}/api/status")
            wrong = await session.get(f"{base_url}/api/status", headers={"Authorization": "Bearer wrong"})
            ok = await session.get(f"{base_url}/api/status", headers={"Authorization": "Bearer secret"})

            assert missing.status == 401
            assert await missing.json() == {"ok": False, "error": "unauthorized"}
            assert wrong.status == 401
            assert ok.status == 200
            payload = await ok.json()
            assert payload["ok"] is True
            assert payload["actors"]["main"]["display_name"] == "Main"
    finally:
        await runner.cleanup()


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
                headers={"Authorization": "Bearer secret"},
            )
            payload = await response.json()

        assert response.status == 201
        assert payload["ok"] is True
        assert payload["part"]["type"] == "image"
        assert payload["part"]["source"]["kind"] == "path"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ws_endpoint_is_authenticated_even_before_ws_channel_slice(tmp_path):
    runner, base_url = await _start_app(_app(tmp_path))
    try:
        async with aiohttp.ClientSession() as session:
            missing = await session.get(f"{base_url}/ws")
            authed = await session.get(f"{base_url}/ws", headers={"Authorization": "Bearer secret"})

            assert missing.status == 401
            assert authed.status == 501
            assert (await authed.json())["error"] == "ws_not_implemented"
    finally:
        await runner.cleanup()


def test_gateway_state_round_trips_without_secrets(tmp_path):
    run_dir = GatewayRunDir(tmp_path / ".bos")
    snapshot = {
        "runtime": "process",
        "gateway": {"auth": {"type": "api_key", "configured": True}},
        "actors": {},
        "channels": {},
        "active_turns": {},
    }

    write_gateway_state(run_dir, snapshot)

    assert read_gateway_state(run_dir)["gateway"]["auth"] == {"type": "api_key", "configured": True}
    assert "secret" not in run_dir.state_file.read_text(encoding="utf-8").lower()
