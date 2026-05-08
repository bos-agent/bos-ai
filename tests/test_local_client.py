import asyncio

import pytest

from bos.cli.local_client import LocalClient
from bos.core.chat_state import ChatState
from bos.extensions.mailboxes.in_memory import InMemMailRoute
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
def chat_state(tmp_path):
    return ChatState(bos_dir=tmp_path)


@pytest.fixture
def local_client(client_mbox, chat_state):
    return LocalClient(
        client_id="local:test",
        client_mbox=client_mbox,
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
    """send() routes to agent@main; actor reply arrives via client mailbox."""
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
async def test_local_client_list_actors(local_client):
    """list_actors() returns the hardcoded main actor."""
    actors = await local_client.list_actors()
    assert actors == {
        "main": {
            "display_name": None,
            "agent_kind": None,
            "is_default": True,
        }
    }


@pytest.mark.asyncio
async def test_local_client_send_targets_main(local_client, main_actor_mbox):
    """send() always targets agent@main."""
    await local_client.connect()
    await local_client.receive()  # drain SESSION_ACK

    async def check_recipient():
        env = await main_actor_mbox.receive()
        assert env.recipient == "agent@main"

    task = asyncio.create_task(check_recipient())
    await local_client.send("hi")
    await task


@pytest.mark.asyncio
async def test_local_client_aclose(local_client):
    """aclose() cleans up the reader task and marks closed."""
    await local_client.connect()
    assert not local_client._closed

    await local_client.aclose()
    assert local_client._closed is True
