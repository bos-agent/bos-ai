import asyncio
import json
from urllib.parse import urlparse

import aiohttp
import pytest
from aiohttp import web

from bos.config import Workspace
from bos.core import Message
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway import Gateway
from bos.gateway.client import GatewayClient
from bos.protocol import MessageType


class EchoCommitAgent:
    name = "main"

    def __init__(self, store: InMemChatStore) -> None:
        self.store = store

    async def ask(self, chat_id, content, *, turn_id, commit_observer=None, **kwargs):
        commit = await self.store.commit_turn(
            chat_id,
            [
                Message(llm_message={"role": "user", "content": content}),
                Message(llm_message={"role": "assistant", "content": f"echo: {content}"}),
            ],
            turn_id=turn_id,
        )
        if commit_observer is not None:
            commit_observer(commit)
        return f"echo: {content}"


class FakeHarness:
    def __init__(self) -> None:
        InMemMailRoute._queues = {}
        self.chat_store = InMemChatStore()
        self.mail_route = InMemMailRoute()

    async def create_agent(self, kind=None, agent_cfg=None):
        return EchoCommitAgent(self.chat_store)


def _workspace(tmp_path) -> Workspace:
    return Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {"port": 0, "api_key_env": "BOS_TEST_GATEWAY_KEY"},
                "default_actor": "main",
                "actors": {"main": {"agent": "main"}},
            }
        },
    )


async def _start_gateway_app(gateway: Gateway):
    app = gateway.build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    await gateway.actor_manager.start_all()
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_gateway_ws_dynamic_channel_sends_message_to_actor(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    harness = FakeHarness()
    gateway = Gateway(workspace=_workspace(tmp_path), harness=harness)
    runner, base_url = await _start_gateway_app(gateway)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer secret"}) as session:
            ws = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            ack = await ws.receive_json()
            assert ack["metadata"]["event"] == "session"
            assert ack["metadata"]["current_revision"] == 0

            await ws.send_json({"content": "hello", "content_type": MessageType.MESSAGE, "base_revision": 0})
            response = await ws.receive_json(timeout=2)

            assert response["content"] == "echo: hello"
            assert response["metadata"]["channel"]["channel_id"] == "tui-a"
            assert await gateway.chat_coordinator.current_revision("chat-1") == 1
            await ws.close()
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_ws_duplicate_channel_id_rejected_without_takeover(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    gateway = Gateway(workspace=_workspace(tmp_path), harness=FakeHarness())
    runner, base_url = await _start_gateway_app(gateway)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer secret"}) as session:
            ws = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            await ws.receive_json()
            with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
                await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            assert excinfo.value.status == 409
            await ws.close()
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_ws_stale_message_returns_missing_history(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    harness = FakeHarness()
    await harness.chat_store.commit_turn(
        "chat-1",
        [Message(llm_message={"role": "assistant", "content": "already here"})],
        turn_id="turn-1",
    )
    gateway = Gateway(workspace=_workspace(tmp_path), harness=harness)
    runner, base_url = await _start_gateway_app(gateway)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer secret"}) as session:
            ws = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            await ws.receive_json()
            await ws.send_json({"content": "stale", "content_type": MessageType.MESSAGE, "base_revision": 0})
            response = await ws.receive_json(timeout=2)

            assert response["content_type"] == MessageType.SYSTEM
            content = json.loads(response["content"])
            assert content["event"] == "stale_chat"
            assert content["current_revision"] == 1
            assert content["missing_messages"]
            await ws.close()
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_ws_missing_base_revision_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    gateway = Gateway(workspace=_workspace(tmp_path), harness=FakeHarness())
    runner, base_url = await _start_gateway_app(gateway)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer secret"}) as session:
            ws = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            await ws.receive_json()
            await ws.send_json({"content": "hello", "content_type": MessageType.MESSAGE})
            response = await ws.receive_json(timeout=2)

            assert response["content_type"] == MessageType.SYSTEM
            content = json.loads(response["content"])
            assert content["event"] == "missing_base_revision"
            assert await gateway.chat_coordinator.current_revision("chat-1") == 0
            await ws.close()
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_ws_client_tracks_ack_revision_before_send(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    harness = FakeHarness()
    await harness.chat_store.commit_turn(
        "chat-1",
        [Message(llm_message={"role": "assistant", "content": "already here"})],
        turn_id="turn-1",
    )
    gateway = Gateway(workspace=_workspace(tmp_path), harness=harness)
    runner, base_url = await _start_gateway_app(gateway)
    parsed = urlparse(base_url)
    client = GatewayClient(
        parsed.hostname or "127.0.0.1",
        parsed.port or 80,
        channel_id="tui-a",
        chat_id="chat-1",
        api_key="secret",
    )
    try:
        await client.connect()
        assert client.current_revision == 1

        await client.send("fresh")
        response = await client.receive()

        assert response.content == "echo: fresh"
        assert client.current_revision == 2
    finally:
        await client.aclose()
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_ws_channel_id_can_reconnect_after_normal_close(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    gateway = Gateway(workspace=_workspace(tmp_path), harness=FakeHarness())
    runner, base_url = await _start_gateway_app(gateway)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer secret"}) as session:
            ws = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            await ws.receive_json()
            await ws.close()

            for _ in range(20):
                if "tui-a" not in gateway.channel_manager.channels:
                    break
                await asyncio.sleep(0.01)

            ws2 = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            ack = await ws2.receive_json()
            assert ack["metadata"]["event"] == "session"
            await ws2.close()
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_ws_new_command_updates_channel_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")
    gateway = Gateway(workspace=_workspace(tmp_path), harness=FakeHarness())
    runner, base_url = await _start_gateway_app(gateway)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer secret"}) as session:
            ws = await session.ws_connect(f"{base_url}/ws?channel_id=tui-a&chat_id=chat-1")
            await ws.receive_json()
            await ws.send_json({"content": "/new", "content_type": MessageType.COMMAND, "base_revision": 0})
            response = await ws.receive_json(timeout=2)

            assert response["content_type"] == MessageType.COMMAND_RESULT
            payload = json.loads(response["content"])
            assert payload["name"] == "new"
            assert payload["ok"] is True
            assert payload["chat_id"] != "chat-1"
            assert gateway.chat_coordinator.get_cursor(
                gateway.channel_manager.channels["tui-a"].channel.ref
            ) == payload["chat_id"]
            await ws.close()
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_client_send_stamps_workdir():
    client = GatewayClient("127.0.0.1", 1, channel_id="ask-1", workdir="/home/user/proj")
    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload):
            sent.append(payload)

    client._ws = _FakeWS()
    client._connected.set()

    await client.send("hello")
    await client.send("explicit", metadata={"workdir": "/elsewhere"})

    assert sent[0]["metadata"]["workdir"] == "/home/user/proj"
    assert sent[1]["metadata"]["workdir"] == "/elsewhere"


@pytest.mark.asyncio
async def test_gateway_client_send_omits_workdir_when_unset():
    client = GatewayClient("127.0.0.1", 1, channel_id="ask-1")
    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload):
            sent.append(payload)

    client._ws = _FakeWS()
    client._connected.set()

    await client.send("hello")

    assert "workdir" not in sent[0]["metadata"]
