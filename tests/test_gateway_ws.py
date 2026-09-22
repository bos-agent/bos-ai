import asyncio
import contextlib
import json

import pytest
from conftest import serve_asgi
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.protocol import State

from bos.config import Workspace
from bos.core import Message
from bos.core.actor import MessageType
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway import Gateway
from bos.gateway.client import GatewayClient


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


class SlowAbortAgent:
    """Blocks until cancelled, then commits abort-safe history like the real agent."""

    name = "scout"

    def __init__(self, store: InMemChatStore) -> None:
        self.store = store
        self.started = asyncio.Event()

    async def ask(self, chat_id, content, *, turn_id, commit_observer=None, **kwargs):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            commit = await self.store.commit_turn(
                chat_id,
                [
                    Message(llm_message={"role": "user", "content": content}),
                    Message(llm_message={"role": "assistant", "content": "(turn aborted before completion)"}),
                ],
                turn_id=turn_id,
            )
            if commit_observer is not None:
                commit_observer(commit)
            raise
        return "unreachable"


class TwoActorHarness(FakeHarness):
    """`main` echoes; `scout` hangs until aborted."""

    def __init__(self) -> None:
        super().__init__()
        self.scout = SlowAbortAgent(self.chat_store)

    async def create_agent(self, kind=None, agent_cfg=None):
        return self.scout if kind == "scout" else EchoCommitAgent(self.chat_store)


def _workspace(tmp_path) -> Workspace:
    return Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {"port": 0},
                "main_actor": "main",
                "actors": {"main": {"agent": "main"}},
            }
        },
    )


def _two_actor_workspace(tmp_path) -> Workspace:
    return Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {"port": 0},
                "main_actor": "main",
                "actors": {"main": {"agent": "main"}, "scout": {"agent": "scout"}},
            }
        },
    )


@contextlib.asynccontextmanager
async def _gateway_server(workspace, harness):
    """A Gateway behind the *mount's* app — the production path to /ws.

    Gateway.build_app() is reachable only from tests; every real websocket goes
    through GatewayMount._dispatch_ws, which is also the only thing enforcing the
    live-only gate (BEP 17 §3.4.3). The mount's internals are set directly rather
    than through start(), which would build a real harness and replace the fake
    these tests are written against.
    """
    from bos.runner.mount import GatewayMount

    mount = GatewayMount(lambda: workspace)
    gateway = Gateway(runtime=workspace.resolve_gateway_runtime(), harness=harness)
    mount._gateway = gateway
    mount._state = "live"
    await gateway.actor_manager.start_all()
    try:
        async with serve_asgi(mount.build_app()) as addr:
            yield gateway, addr
    finally:
        await gateway.actor_manager.stop_all()
        await gateway.channel_manager.stop_all()


@pytest.mark.asyncio
async def test_gateway_ws_dynamic_channel_sends_message_to_actor(tmp_path):
    harness = FakeHarness()
    async with _gateway_server(_workspace(tmp_path), harness) as (gateway, addr):
        async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1", max_size=None) as ws:
            ack = json.loads(await ws.recv())
            assert ack["metadata"]["event"] == "session"
            assert ack["metadata"]["current_revision"] == 0
            # No turn in flight: clients adopt this to gate interrupt hotkeys.
            assert ack["metadata"]["active_turn"] is None

            await ws.send(json.dumps({"content": "hello", "content_type": MessageType.MESSAGE, "base_revision": 0}))
            response = json.loads(await asyncio.wait_for(ws.recv(), 2))

            assert response["content"] == "echo: hello"
            assert response["metadata"]["channel"]["channel_id"] == "tui-a"
            assert await gateway.chat_coordinator.current_revision("chat-1") == 1


@pytest.mark.asyncio
async def test_gateway_ws_duplicate_channel_id_rejected_without_takeover(tmp_path):
    """The rejection keeps its HTTP shape through the ASGI websocket
    denial-response extension, which uvicorn implements (BEP 17 §3.6.4, §7.8)."""
    async with _gateway_server(_workspace(tmp_path), FakeHarness()) as (_gateway, addr):
        async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1", max_size=None) as ws:
            json.loads(await ws.recv())
            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1"):
                    pass

    assert excinfo.value.response.status_code == 409
    assert json.loads(excinfo.value.response.body) == {"ok": False, "error": "duplicate_channel_id"}


@pytest.mark.asyncio
async def test_gateway_ws_requires_a_channel_id(tmp_path):
    async with _gateway_server(_workspace(tmp_path), FakeHarness()) as (_gateway, addr):
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://{addr}/ws"):
                pass

    assert excinfo.value.response.status_code == 400
    assert json.loads(excinfo.value.response.body)["error"] == "channel_id is required"


@pytest.mark.asyncio
async def test_mounted_ws_is_denied_while_the_mount_is_not_live(tmp_path):
    """The mount's state gate, not just ``_gateway is None``: a restart installs
    the new Gateway before its actors and channels are up, and a websocket
    accepted in that window gets a consumer that does not exist yet
    (BEP 17 §3.4.3)."""
    from bos.runner.mount import GatewayMount

    workspace = _workspace(tmp_path)
    mount = GatewayMount(lambda: workspace)
    mount._gateway = Gateway(runtime=workspace.resolve_gateway_runtime(), harness=FakeHarness())
    mount._state = "restarting"

    async with serve_asgi(mount.build_app()) as addr:
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://{addr}/ws?channel_id=tui-a"):
                pass

    assert excinfo.value.response.status_code == 503
    assert json.loads(excinfo.value.response.body) == {"ok": False, "error": "restarting"}


@pytest.mark.asyncio
async def test_ws_route_without_a_handler_denies_with_501():
    from bos.gateway import ResolvedGatewayConfig
    from bos.gateway.http import create_gateway_app

    app = create_gateway_app(config_provider=ResolvedGatewayConfig, status_provider=lambda: {"actors": {}})
    async with serve_asgi(app) as addr:
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://{addr}/ws"):
                pass

    assert excinfo.value.response.status_code == 501
    assert json.loads(excinfo.value.response.body)["error"] == "ws_not_implemented"


@pytest.mark.asyncio
async def test_gateway_ws_stale_message_returns_missing_history(tmp_path):
    harness = FakeHarness()
    await harness.chat_store.commit_turn(
        "chat-1",
        [Message(llm_message={"role": "assistant", "content": "already here"})],
        turn_id="turn-1",
    )
    async with _gateway_server(_workspace(tmp_path), harness) as (_gateway, addr):
        async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1", max_size=None) as ws:
            json.loads(await ws.recv())
            await ws.send(json.dumps({"content": "stale", "content_type": MessageType.MESSAGE, "base_revision": 0}))
            response = json.loads(await asyncio.wait_for(ws.recv(), 2))

            assert response["content_type"] == MessageType.SYSTEM
            content = json.loads(response["content"])
            assert content["event"] == "stale_chat"
            assert content["current_revision"] == 1
            assert content["missing_messages"]


@pytest.mark.asyncio
async def test_gateway_ws_missing_base_revision_is_rejected(tmp_path):
    async with _gateway_server(_workspace(tmp_path), FakeHarness()) as (gateway, addr):
        async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1", max_size=None) as ws:
            json.loads(await ws.recv())
            await ws.send(json.dumps({"content": "hello", "content_type": MessageType.MESSAGE}))
            response = json.loads(await asyncio.wait_for(ws.recv(), 2))

            assert response["content_type"] == MessageType.SYSTEM
            content = json.loads(response["content"])
            assert content["event"] == "missing_base_revision"
            assert await gateway.chat_coordinator.current_revision("chat-1") == 0


@pytest.mark.asyncio
async def test_gateway_ws_client_tracks_ack_revision_before_send(tmp_path):
    """The GatewayClient against the ported server — the wire protocol did not
    change, so this holds across the transport swap on either side."""
    harness = FakeHarness()
    await harness.chat_store.commit_turn(
        "chat-1",
        [Message(llm_message={"role": "assistant", "content": "already here"})],
        turn_id="turn-1",
    )
    async with _gateway_server(_workspace(tmp_path), harness) as (_gateway, addr):
        host, port = addr.split(":")
        client = GatewayClient(host, int(port), channel_id="tui-a", chat_id="chat-1")
        try:
            await client.connect()
            assert client.current_revision == 1

            # The session ack is forwarded to the consumer with the transcript.
            ack = await client.receive()
            assert ack.metadata.get("event") == "session"
            hydrated = [m["llm_message"]["content"] for m in ack.metadata["missing_messages"]]
            assert hydrated == ["already here"]

            await client.send("fresh")
            response = await client.receive()

            assert response.content == "echo: fresh"
            assert client.current_revision == 2
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_gateway_ws_channel_id_can_reconnect_after_normal_close(tmp_path):
    async with _gateway_server(_workspace(tmp_path), FakeHarness()) as (gateway, addr):
        url = f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1"
        async with connect(url, max_size=None) as ws:
            json.loads(await ws.recv())

        for _ in range(200):
            if "tui-a" not in gateway.channel_manager.channels:
                break
            await asyncio.sleep(0.01)

        async with connect(url, max_size=None) as ws2:
            ack = json.loads(await ws2.recv())
            assert ack["metadata"]["event"] == "session"


@pytest.mark.asyncio
async def test_gateway_ws_new_command_updates_channel_cursor(tmp_path):
    async with _gateway_server(_workspace(tmp_path), FakeHarness()) as (gateway, addr):
        async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1", max_size=None) as ws:
            json.loads(await ws.recv())
            await ws.send(json.dumps({"content": "/new", "content_type": MessageType.COMMAND, "base_revision": 0}))
            response = json.loads(await asyncio.wait_for(ws.recv(), 2))

            assert response["content_type"] == MessageType.COMMAND_RESULT
            payload = json.loads(response["content"])
            assert payload["name"] == "new"
            assert payload["ok"] is True
            assert payload["chat_id"] != "chat-1"
            assert (
                gateway.chat_coordinator.get_cursor(gateway.channel_manager.channels["tui-a"].channel.ref)
                == payload["chat_id"]
            )


@pytest.mark.asyncio
async def test_gateway_ws_interrupt_targets_the_mention_routed_actor(tmp_path):
    """An interrupt belongs to the turn in flight, not to this channel's default
    actor: a @mention-routed turn runs elsewhere and an INTERRUPT_ABORT pinned to
    main_actor would land in an idle mailbox, so the running turn never notices.
    """
    harness = TwoActorHarness()
    async with _gateway_server(_two_actor_workspace(tmp_path), harness) as (gateway, addr):
        async with connect(f"ws://{addr}/ws?channel_id=tui-a&chat_id=chat-1", max_size=None) as ws:
            json.loads(await ws.recv())

            await ws.send(
                json.dumps({"content": "@scout dig", "content_type": MessageType.MESSAGE, "base_revision": 0})
            )
            await asyncio.wait_for(harness.scout.started.wait(), timeout=2)
            active = gateway.chat_coordinator.active_turn_status("chat-1")
            assert active is not None and active["actor"] == "scout"

            await ws.send(
                json.dumps({"content": "", "content_type": MessageType.INTERRUPT_ABORT, "base_revision": 0})
            )
            events = []
            while "turn_aborted" not in events:
                message = json.loads(await asyncio.wait_for(ws.recv(), 2))
                events.append(message["metadata"].get("event"))

            assert gateway.chat_coordinator.active_turn_status("chat-1") is None


@pytest.mark.asyncio
async def test_gateway_client_forwards_session_ack_envelope():
    client = GatewayClient("127.0.0.1", 1, channel_id="tui-a")

    class _FakeWS:
        async def recv(self):
            return json.dumps({
                "sender": "agent@main",
                "content": "connected",
                "content_type": "system",
                "chat_id": "chat-9",
                "metadata": {
                    "event": "session",
                    "channel_id": "tui-a",
                    "chat_id": "chat-9",
                    "current_revision": 4,
                    "missing_messages": [{"llm_message": {"role": "user", "content": "hi"}}],
                },
            })

    client._ws = _FakeWS()
    await client._receive_session_ack()

    env = await client.receive()
    assert env.content_type == MessageType.SYSTEM
    assert env.chat_id == "chat-9"
    assert env.metadata["event"] == "session"
    assert env.metadata["missing_messages"][0]["llm_message"]["content"] == "hi"
    assert client.current_revision == 4
    assert client.chat_id == "chat-9"


class _RecordingWS:
    """Captures what send() put on the wire. ``state`` is what ``connected``
    reads, and websockets frames text, so every payload arrives as a string."""

    state = State.OPEN

    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent

    async def send(self, payload):
        self._sent.append(json.loads(payload))


@pytest.mark.asyncio
async def test_gateway_client_send_stamps_workdir():
    client = GatewayClient("127.0.0.1", 1, channel_id="ask-1", workdir="/home/user/proj")
    sent: list[dict] = []
    client._ws = _RecordingWS(sent)
    client._connected.set()

    await client.send("hello")
    await client.send("explicit", metadata={"workdir": "/elsewhere"})

    assert sent[0]["metadata"]["workdir"] == "/home/user/proj"
    assert sent[1]["metadata"]["workdir"] == "/elsewhere"


@pytest.mark.asyncio
async def test_gateway_client_send_omits_workdir_when_unset():
    client = GatewayClient("127.0.0.1", 1, channel_id="ask-1")
    sent: list[dict] = []
    client._ws = _RecordingWS(sent)
    client._connected.set()

    await client.send("hello")

    assert "workdir" not in sent[0]["metadata"]


@pytest.mark.asyncio
async def test_gateway_client_connect_failure_closes_transport():
    """A refused connect must not leave an unclosed httpx client behind."""
    client = GatewayClient("127.0.0.1", 1, channel_id="probe")

    with pytest.raises(OSError):
        await client.connect()

    assert client._session is None
    assert client._ws is None


@pytest.mark.asyncio
async def test_gateway_client_bad_session_ack_closes_transport():
    """A socket that opens but never acks must not leak the ws or the client."""
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def _handler(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"content": "not an ack", "content_type": MessageType.MESSAGE})

    app = Starlette(routes=[WebSocketRoute("/ws", _handler)])
    async with serve_asgi(app) as addr:
        host, port = addr.split(":")
        client = GatewayClient(host, int(port), channel_id="probe")
        with pytest.raises(RuntimeError):
            await client.connect()

        assert client._ws is None
        assert client._session is None
