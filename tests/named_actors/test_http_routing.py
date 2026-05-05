import asyncio
import re

import aiohttp
import pytest
from aiohttp import web

from bos.extensions.channels.http import HttpChannel
from bos.named_actors.registry import ActorRegistry


class FakeRegistry:
    _MENTION_RE = re.compile(r"@([\w][\w-]*)\s+")

    def __init__(self):
        self.routes = []

    def route(self, content, metadata=None):
        self.routes.append((content, metadata))
        from bos.named_actors.registry import RouteResult

        out_metadata = dict(metadata or {})
        target = None
        cleaned = content

        m = self._MENTION_RE.match(content) if isinstance(content, str) else None
        if m:
            target = m.group(1)
            cleaned = content[m.end():]
            out_metadata["target_actor"] = target

        if target is None:
            target = out_metadata.get("target_actor", "main")

        out_metadata["target_display"] = f"{target.title()} (assistant)"
        return RouteResult(
            target_address=f"agent@{target}",
            content=cleaned,
            target_actor=target if target != "main" else None,
            metadata=out_metadata,
        )


class FakeMailBox:
    def __init__(self, address="channel@test"):
        self.address = address
        self.sent: list = []

    async def send(self, recipient, content, **kwargs):
        self.sent.append((recipient, content, kwargs))

    async def receive(self):
        await asyncio.sleep(10)
        raise asyncio.CancelledError

    async def receive_nowait(self):
        return None


def _registry_with_main_and_investment():
    registry = ActorRegistry()
    registry.register("main", FakeMailBox("agent@main"), is_default=True, display_name="Main", agent_kind="assistant")
    registry.register(
        "investment",
        FakeMailBox("agent@investment"),
        display_name="Investment",
        agent_kind="assistant",
    )
    registry.register("researcher", FakeMailBox("agent@researcher"), display_name="Researcher", agent_kind="assistant")
    return registry


@pytest.mark.asyncio
async def test_post_send_uses_registry_for_routing_and_metadata():
    registry = FakeRegistry()
    mailbox = FakeMailBox()
    channel = HttpChannel(target_address="agent@main", actor_registry=registry, port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{base_url}/api/send",
            json={
                "content": "@researcher find papers",
                "chat_id": "abc123",
            },
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["ok"] is True

    await runner.cleanup()

    assert len(registry.routes) == 1
    called_content, _ = registry.routes[0]
    assert called_content == "@researcher find papers"

    assert len(mailbox.sent) == 1
    recipient, content, kwargs = mailbox.sent[0]
    assert recipient == "agent@researcher"
    assert content == "find papers"
    assert kwargs["metadata"]["target_actor"] == "researcher"
    assert kwargs["metadata"]["target_display"] == "Researcher (assistant)"


@pytest.mark.asyncio
async def test_post_send_without_registry_uses_direct_target_address():
    mailbox = FakeMailBox()
    channel = HttpChannel(target_address="agent@investment", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{base_url}/api/send",
            json={
                "content": "portfolio check",
                "chat_id": "abc123",
            },
        )
        assert resp.status == 202

    await runner.cleanup()

    recipient, content, _ = mailbox.sent[0]
    assert recipient == "agent@investment"
    assert content == "portfolio check"


@pytest.mark.asyncio
async def test_post_send_with_registry_preserves_direct_target_address():
    mailbox = FakeMailBox()
    channel = HttpChannel(
        target_address="agent@investment",
        actor_registry=_registry_with_main_and_investment(),
        port=0,
    )
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{base_url}/api/send",
            json={
                "content": "portfolio check",
                "chat_id": "abc123",
            },
        )
        assert resp.status == 202

    await runner.cleanup()

    recipient, content, kwargs = mailbox.sent[0]
    assert recipient == "agent@investment"
    assert content == "portfolio check"
    assert kwargs["metadata"]["target_actor"] == "investment"


@pytest.mark.asyncio
async def test_post_send_with_registry_accepts_structured_content():
    mailbox = FakeMailBox()
    channel = HttpChannel(target_address="agent@main", actor_registry=_registry_with_main_and_investment(), port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    content = [{"type": "text", "text": "hello"}]

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{base_url}/api/send",
            json={
                "content": content,
                "chat_id": "abc123",
            },
        )
        assert resp.status == 202

    await runner.cleanup()

    recipient, sent_content, kwargs = mailbox.sent[0]
    assert recipient == "agent@main"
    assert sent_content == content
    assert kwargs["metadata"]["target_actor"] == "main"


@pytest.mark.asyncio
async def test_websocket_send_uses_registry_for_routing_and_metadata():
    mailbox = FakeMailBox()
    channel = HttpChannel(target_address="agent@main", actor_registry=_registry_with_main_and_investment(), port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(f"{base_url}/ws?client_id=test-client")
        await ws.receive_json()
        await ws.send_json({"content": "@researcher find papers"})
        for _ in range(20):
            if mailbox.sent:
                break
            await asyncio.sleep(0.01)
        await ws.close()

    await runner.cleanup()

    recipient, content, kwargs = mailbox.sent[0]
    assert recipient == "agent@researcher"
    assert content == "find papers"
    assert kwargs["metadata"]["target_actor"] == "researcher"
