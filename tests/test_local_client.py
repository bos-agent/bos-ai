import asyncio

import pytest

from bos.cli.local_client import LocalClient
from bos.core.chat_state import ChatState
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.named_actors.registry import ActorRegistry
from bos.protocol import MessageType


@pytest.fixture(autouse=True)
def _cleanup_queues():
    """Clear the class-level InMemMailRoute._queues before each test."""
    InMemMailRoute._queues.clear()
    yield
    InMemMailRoute._queues.clear()


@pytest.fixture
def mail_route():
    return InMemMailRoute()


@pytest.fixture
def client_mbox(mail_route):
    return mail_route.bind("client@local")


@pytest.fixture
def main_actor_mbox(mail_route):
    return mail_route.bind("agent@main")


@pytest.fixture
def registry(main_actor_mbox):
    reg = ActorRegistry()
    reg.register("main", main_actor_mbox, is_default=True)
    return reg


@pytest.fixture
def chat_state(tmp_path):
    return ChatState(bos_dir=tmp_path)


@pytest.fixture
def local_client(client_mbox, registry, chat_state):
    return LocalClient(
        client_id="local:test",
        client_mbox=client_mbox,
        registry=registry,
        chat_state=chat_state,
    )


@pytest.mark.asyncio
async def test_local_client_connected_property(local_client):
    """connected is always True in local mode."""
    assert local_client.connected is True


@pytest.mark.asyncio
async def test_local_client_client_id_property(local_client):
    assert local_client.client_id == "local:test"


@pytest.mark.asyncio
async def test_local_client_chat_id_starts_none(local_client):
    assert local_client.chat_id is None


@pytest.mark.asyncio
async def test_local_client_update_chat_id(local_client):
    local_client.update_chat_id("chat-123")
    assert local_client.chat_id == "chat-123"


@pytest.mark.asyncio
async def test_local_client_update_chat_id_rejects_empty(local_client):
    with pytest.raises(ValueError):
        local_client.update_chat_id("")


@pytest.mark.asyncio
async def test_local_client_connect_sends_session_ack(local_client):
    """connect() generates a chat_id and puts a SESSION_ACK in the receive queue."""
    await local_client.connect()

    assert local_client.chat_id is not None
    assert len(local_client.chat_id) > 0

    env = await local_client.receive()
    assert env.content_type == MessageType.SYSTEM
    assert env.metadata.get("event") == "session"
    assert env.metadata.get("client_id") == "local:test"
    assert env.metadata.get("chat_id") == local_client.chat_id


@pytest.mark.asyncio
async def test_local_client_send_and_receive(local_client, main_actor_mbox):
    """send() routes to the default actor mailbox; actor reply arrives via client mailbox."""
    await local_client.connect()
    chat_id = local_client.chat_id

    # Drain the SESSION_ACK
    await local_client.receive()

    # Start a background task that simulates the actor receiving and replying
    async def actor_reply():
        env = await main_actor_mbox.receive()
        assert env.content == "hello"
        # Reply back to sender (the client mailbox)
        await main_actor_mbox.send(
            env.sender,
            "hi back",
            content_type=MessageType.MESSAGE,
            chat_id=env.chat_id,
        )

    reply_task = asyncio.create_task(actor_reply())

    await local_client.send("hello", chat_id=chat_id)

    # Receive the reply
    env = await local_client.receive()
    assert env.content == "hi back"
    assert env.content_type == MessageType.MESSAGE

    await reply_task


@pytest.mark.asyncio
async def test_local_client_at_mention_routing(local_client, main_actor_mbox, mail_route):
    """@mention routes to the named actor, not the default."""
    # Register a second actor
    coder_mbox = mail_route.bind("agent@coder")
    local_client._registry.register("coder", coder_mbox)

    await local_client.connect()
    chat_id = local_client.chat_id

    # Drain the SESSION_ACK
    await local_client.receive()

    async def coder_reply():
        env = await coder_mbox.receive()
        assert env.content == "write code"
        await coder_mbox.send(
            env.sender,
            "code written",
            content_type=MessageType.MESSAGE,
            chat_id=env.chat_id,
        )

    reply_task = asyncio.create_task(coder_reply())

    await local_client.send("@coder write code", chat_id=chat_id)

    env = await local_client.receive()
    assert env.content == "code written"

    await reply_task


@pytest.mark.asyncio
async def test_local_client_list_actors(local_client, mail_route):
    """list_actors() returns registered actors from the registry."""
    coder_mbox = mail_route.bind("agent@coder")
    local_client._registry.register("coder", coder_mbox, agent_kind="coder")

    actors = await local_client.list_actors()
    assert "main" in actors
    assert actors["main"]["agent_kind"] is None
    assert actors["main"]["is_default"] is True
    assert "coder" in actors
    assert actors["coder"]["agent_kind"] == "coder"


@pytest.mark.asyncio
async def test_local_client_aclose(local_client):
    """aclose() cleans up the reader task and marks closed."""
    await local_client.connect()
    assert not local_client._closed

    await local_client.aclose()
    assert local_client._closed is True
