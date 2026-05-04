# tests/squad/test_http_routing.py
import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from bos.extensions.channels.http import HttpChannel


import re

class FakeRegistry:
    _MENTION_RE = re.compile(r"@([\w][\w-]*)\s+")

    def __init__(self):
        self.routes = []

    def route(self, content, metadata=None):
        self.routes.append((content, metadata))
        from bos.squad.registry import RouteResult

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


@pytest.mark.asyncio
async def test_post_send_uses_registry_for_routing():
    registry = FakeRegistry()
    mailbox = FakeMailBox()
    channel = HttpChannel(
        target_address="agent@main",
        actor_registry=registry,
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
                "content": "@researcher find papers",
                "chat_id": "abc123",
            },
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["ok"] is True

    await runner.cleanup()

    # Verify registry was consulted
    assert len(registry.routes) == 1
    called_content, _ = registry.routes[0]
    assert called_content == "@researcher find papers"

    # Verify message was sent to resolved address
    assert len(mailbox.sent) == 1
    recipient, _, _ = mailbox.sent[0]
    assert recipient == "agent@researcher"
