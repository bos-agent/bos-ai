import json

import aiohttp
import pytest
from aiohttp import web

from bos.config import Workspace
from bos.core import Message
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway import Gateway
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
