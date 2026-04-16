import asyncio

import pytest

from bos.core import Channel, Envelope
from bos.extensions.channels.broadcast import BroadcastChannel


class FakeMailbox:
    def __init__(self, address: str, inbound: list[Envelope]) -> None:
        self.address = address
        self._inbound = asyncio.Queue()
        for env in inbound:
            self._inbound.put_nowait(env)
        self.sent: list[Envelope] = []

    async def receive(self) -> Envelope:
        return await self._inbound.get()

    async def send(
        self,
        recipient: str,
        content: str,
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
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            return None


def test_broadcast_channel_satisfies_channel_protocol():
    assert isinstance(BroadcastChannel(["channel@http"], "agent@main"), Channel)


def test_broadcast_rewrites_inbound_to_canonical_conversation_id():
    channel = BroadcastChannel(["channel@http", "channel@telegram"], "agent@main")
    canonical = channel._conversation_id

    channel._remember_member_conversation("channel@http", "http-local-1")
    channel._remember_member_conversation("channel@telegram", "telegram:42")

    assert channel._conversation_for_member("channel@http") == "http-local-1"
    assert channel._conversation_for_member("channel@telegram") == "telegram:42"
    assert canonical != channel._conversation_for_member("channel@telegram")


def test_rotate_conversation_updates_http_but_keeps_telegram_sticky_id():
    channel = BroadcastChannel(["channel@http", "channel@telegram"], "agent@main")
    old_canonical = channel._conversation_id
    channel._remember_member_conversation("channel@http", "http-local-1")
    channel._remember_member_conversation("channel@telegram", "telegram:42")

    channel._rotate_conversation("channel@http", requested_local_id="http-local-2")

    assert channel._conversation_id != old_canonical
    assert channel._conversation_for_member("channel@http") == "http-local-2"
    assert channel._conversation_for_member("channel@telegram") == "telegram:42"


def test_new_conversation_request_detects_channel_and_slash_commands():
    channel = BroadcastChannel(["channel@http"], "agent@main")

    assert channel._is_new_conversation_request(
        Envelope(
            sender="channel@http",
            recipient="channel@user",
            content="new_conversation",
            content_type="channel_command",
        )
    )
    assert channel._is_new_conversation_request(
        Envelope(sender="channel@telegram", recipient="channel@user", content="/new", content_type="command")
    )
    assert not channel._is_new_conversation_request(
        Envelope(sender="channel@telegram", recipient="channel@user", content="/history", content_type="command")
    )


@pytest.mark.asyncio
async def test_broadcast_run_fans_out_actor_messages_via_bound_mailbox():
    channel = BroadcastChannel(["channel@http", "channel@telegram"], "agent@main")
    channel._remember_member_conversation("channel@http", "http-local-1")
    channel._remember_member_conversation("channel@telegram", "telegram:42")
    mailbox = FakeMailbox(
        "channel@user",
        [
            Envelope(
                sender="agent@main",
                recipient="channel@user",
                content="hello",
                conversation_id=channel._conversation_id,
            )
        ],
    )

    task = asyncio.create_task(channel.run(mailbox))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(mailbox.sent) == 2
    assert {(env.sender, env.recipient, env.content) for env in mailbox.sent} == {
        ("channel@user", "channel@http", "hello"),
        ("channel@user", "channel@telegram", "hello"),
    }
    assert {env.conversation_id for env in mailbox.sent} == {"http-local-1", "telegram:42"}
