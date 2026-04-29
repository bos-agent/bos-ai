import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from bos.extensions.channels.http import APP_STATUS_INFO, HttpChannel, _envelope_from_dict, _store_uploaded_image
from bos.extensions.channels.http_client import HttpChannelClient
from bos.protocol import Envelope, MessageType


class FakeMailbox:
    def __init__(self, address: str) -> None:
        self.address = address
        self._queue = asyncio.Queue()
        self.sent: list[Envelope] = []

    async def receive(self) -> Envelope:
        return await self._queue.get()

    async def send(
        self,
        recipient: str,
        content,
        *,
        content_type: str = "message",
        conversation_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.sent.append(
            Envelope(
                sender=self.address,
                recipient=recipient,
                content=content,
                content_type=content_type,
                conversation_id=conversation_id,
                metadata=metadata or {},
            )
        )

    async def receive_nowait(self) -> Envelope | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def push(self, env: Envelope) -> None:
        self._queue.put_nowait(env)


def test_envelope_from_dict_ignores_client_recipient():
    env = _envelope_from_dict(
        {
            "recipient": "agent@main",
            "content": "hello",
            "content_type": "message",
            "conversation_id": "conv-1",
        },
        sender="channel@http",
        target="channel@user",
    )

    assert env.sender == "channel@http"
    assert env.recipient == "channel@user"
    assert env.content == "hello"
    assert env.content_type == "message"
    assert env.conversation_id == "conv-1"


def test_envelope_from_dict_preserves_structured_message_content():
    content = [
        {"type": "text", "text": "Inspect this image."},
        {"type": "image", "source": {"kind": "url", "value": "https://example.com/image.jpg"}},
    ]

    env = _envelope_from_dict(
        {
            "content": content,
            "content_type": "message",
            "conversation_id": "conv-structured",
        },
        sender="channel@http",
        target="channel@user",
    )

    assert env.content == content
    assert env.content_type == "message"


def test_envelope_from_dict_preserves_path_backed_image_content():
    content = [
        {"type": "text", "text": "Inspect this uploaded image."},
        {"type": "image", "source": {"kind": "path", "value": "/tmp/bos-uploads/cat.png"}},
    ]

    env = _envelope_from_dict(
        {
            "content": content,
            "content_type": "message",
            "conversation_id": "conv-path-image",
        },
        sender="channel@http",
        target="channel@user",
    )

    assert env.content == content
    assert env.content_type == "message"


def test_envelope_from_dict_preserves_actor_key_metadata():
    env = _envelope_from_dict(
        {
            "content": "/new",
            "content_type": "command",
            "conversation_id": "conv-1",
            "metadata": {"actor_key": "telegram:42"},
        },
        sender="channel@http",
        target="channel@user",
    )

    assert env.metadata == {"actor_key": "telegram:42"}


def test_envelope_from_dict_rejects_structured_non_message_content():
    try:
        _envelope_from_dict(
            {
                "content": [{"type": "text", "text": "/history"}],
                "content_type": "command",
                "conversation_id": "conv-command",
            },
            sender="channel@http",
            target="channel@user",
        )
    except TypeError as exc:
        assert "Non-message envelopes require string content" in str(exc)
    else:
        raise AssertionError("Expected structured command content to be rejected")


def test_store_uploaded_image_returns_path_backed_bos_part(tmp_path):
    part = _store_uploaded_image(
        upload_dir=tmp_path,
        filename="cat.png",
        content_type="image/png",
        data=b"\x89PNG\r\n\x1a\nfake",
    )

    assert part["type"] == "image"
    assert part["source"]["kind"] == "path"
    stored_path = Path(part["source"]["value"])
    assert stored_path.parent == tmp_path
    assert stored_path.is_file()
    assert stored_path.read_bytes() == b"\x89PNG\r\n\x1a\nfake"


def test_store_uploaded_image_uses_image_mime_to_choose_extension(tmp_path):
    part = _store_uploaded_image(
        upload_dir=tmp_path,
        filename="upload.bin",
        content_type="image/png",
        data=b"\x89PNG\r\n\x1a\nfake",
    )

    stored_path = Path(part["source"]["value"])
    assert stored_path.suffix == ".png"


def test_http_channel_build_app_sets_explicit_upload_limit():
    class DummyMailbox:
        address = "channel@http"

    channel = HttpChannel(target_address="channel@user", max_upload_bytes=5 * 1024 * 1024)
    app = channel._build_app(DummyMailbox())

    assert app._client_max_size == 5 * 1024 * 1024
    assert app[APP_STATUS_INFO]["max_upload_bytes"] == 5 * 1024 * 1024
    assert app[APP_STATUS_INFO]["interactive_ws_clients_supported"] == "multiple_by_client_id"
    assert app[APP_STATUS_INFO]["interactive_ws_takeover_supported"] == "per_client_id"


@pytest.mark.asyncio
async def test_http_channel_allows_multiple_interactive_websocket_clients_with_distinct_client_ids():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base_url = f"http://127.0.0.1:{port}"

    session_a = aiohttp.ClientSession()
    session_b = aiohttp.ClientSession()
    ws_a = None
    ws_b = None
    try:
        ws_a = await session_a.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=conv-a")
        ws_b = await session_b.ws_connect(f"{base_url}/ws?client_id=tui-b&conversation_id=conv-b")
        assert (await ws_a.receive_json(timeout=1))["conversation_id"] == "conv-a"
        assert (await ws_b.receive_json(timeout=1))["conversation_id"] == "conv-b"
        assert ws_a.closed is False
        assert ws_b.closed is False
    finally:
        if ws_a is not None:
            await ws_a.close()
        if ws_b is not None:
            await ws_b.close()
        await session_a.close()
        await session_b.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_channel_rejects_duplicate_client_id_without_takeover():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base_url = f"http://127.0.0.1:{port}"

    session_a = aiohttp.ClientSession()
    session_b = aiohttp.ClientSession()
    ws_a = None
    try:
        ws_a = await session_a.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=conv-a")
        await ws_a.receive_json(timeout=1)
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await session_b.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=conv-a")
        assert exc.value.status == 409
    finally:
        if ws_a is not None:
            await ws_a.close()
        await session_a.close()
        await session_b.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_channel_takeover_disconnects_existing_client_id_only():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base_url = f"http://127.0.0.1:{port}"

    session_a = aiohttp.ClientSession()
    session_b = aiohttp.ClientSession()
    ws_a = None
    ws_b = None
    try:
        ws_a = await session_a.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=conv-a")
        await ws_a.receive_json(timeout=1)
        ws_b = await session_b.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=conv-a&takeover=1")
        await ws_b.receive_json(timeout=1)

        msg = await asyncio.wait_for(ws_a.receive(), timeout=1)
        assert msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED}
        assert ws_b.closed is False
    finally:
        if ws_a is not None and not ws_a.closed:
            await ws_a.close()
        if ws_b is not None and not ws_b.closed:
            await ws_b.close()
        await session_a.close()
        await session_b.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_channel_injects_direct_actor_key_and_routes_reply_to_matching_client():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base_url = f"http://127.0.0.1:{port}"

    session_a = aiohttp.ClientSession()
    session_b = aiohttp.ClientSession()
    ws_a = None
    ws_b = None
    try:
        ws_a = await session_a.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=conv-a")
        ws_b = await session_b.ws_connect(f"{base_url}/ws?client_id=tui-b&conversation_id=conv-b")
        await ws_a.receive_json(timeout=1)
        await ws_b.receive_json(timeout=1)

        await ws_a.send_json({"content": "hello", "content_type": "message", "conversation_id": "conv-a"})

        for _ in range(20):
            if mailbox.sent:
                break
            await asyncio.sleep(0.01)
        sent = mailbox.sent[-1]
        assert sent.recipient == "agent@main"
        assert sent.conversation_id == "conv-a"
        assert sent.metadata["actor_key"] == "http:tui-a:conv-a"
        assert sent.metadata["routing"] == {"client_id": "tui-a", "conversation_id": "conv-a"}

        mailbox.push(
            Envelope(
                sender="agent@main",
                recipient="channel@http",
                content="reply",
                conversation_id="conv-a",
            )
        )

        msg = await ws_a.receive_json(timeout=1)
        assert msg["content"] == "reply"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws_b.receive_json(), timeout=0.2)
    finally:
        if ws_a is not None:
            await ws_a.close()
        if ws_b is not None:
            await ws_b.close()
        await session_a.close()
        await session_b.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_channel_client_receives_takeover_system_event_and_stops():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    client_a = HttpChannelClient(host="127.0.0.1", port=port, address="tui-a")
    client_b = HttpChannelClient(host="127.0.0.1", port=port, address="tui-b")
    try:
        await client_a.connect()
        client_b = HttpChannelClient(host="127.0.0.1", port=port, address="tui-a", client_id=client_a.client_id)
        await client_b.connect(takeover=True)

        env = await asyncio.wait_for(client_a.receive(), timeout=1)
        assert env.content_type == MessageType.SYSTEM
        assert "took over" in env.content
        assert client_a.connected is False
    finally:
        await client_a.aclose()
        await client_b.aclose()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_channel_websocket_receives_new_command_result_payload():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base_url = f"http://127.0.0.1:{port}"

    session = aiohttp.ClientSession()
    ws = None
    try:
        ws = await session.ws_connect(f"{base_url}/ws?client_id=tui-a&conversation_id=session-2")
        await ws.receive_json(timeout=1)
        mailbox.push(
            Envelope(
                sender="agent@main",
                recipient="channel@http",
                content=json.dumps(
                    {
                        "name": "new",
                        "ok": True,
                        "result": "session reset",
                        "session_id": "session-2",
                        "scope": "actor_key_session",
                    }
                ),
                content_type=MessageType.COMMAND_RESULT,
                conversation_id="session-2",
            )
        )
        msg = await ws.receive_json(timeout=1)
        assert msg["content_type"] == MessageType.COMMAND_RESULT
        assert json.loads(msg["content"]) == {
            "name": "new",
            "ok": True,
            "result": "session reset",
            "session_id": "session-2",
            "scope": "actor_key_session",
        }
        assert msg["conversation_id"] == "session-2"
    finally:
        if ws is not None:
            await ws.close()
        await session.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_send_endpoint_accepts_async_new_without_command_result_guarantee():
    mailbox = FakeMailbox("channel@http")
    channel = HttpChannel(target_address="agent@main", port=0)
    app = channel._build_app(mailbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    base_url = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as session:
        response = await session.post(
            f"{base_url}/api/send",
            json={"content": "/new", "content_type": "command", "conversation_id": "legacy-session"},
        )
        payload = await response.json()

    await runner.cleanup()

    assert response.status == 202
    assert payload == {"ok": True}
    assert [(env.recipient, env.content, env.content_type) for env in mailbox.sent] == [
        ("agent@main", "/new", MessageType.COMMAND)
    ]
