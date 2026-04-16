"""Tests for MailRoute, bound MailBox instances, and JSONL delivery semantics."""

from __future__ import annotations

import asyncio

import pytest

from bos.core import Envelope, MailBox, MailRoute
from bos.extensions.mailboxes.jsonl_mailbox import JsonlMailRoute


class FakeAgent:
    def __init__(self, response: str = "ack"):
        self._response = response
        self.calls: list[tuple[str, str]] = []

    async def ask(self, conversation_id: str, message: str) -> str:
        self.calls.append((conversation_id, message))
        return self._response


class TestMailRouteProtocol:
    def test_jsonl_mail_route_satisfies_protocol(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        assert isinstance(route, MailRoute)

    def test_bind_returns_mailbox_with_address(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        mailbox = route.bind("alice")
        assert isinstance(mailbox, MailBox)
        assert mailbox.address == "alice"


class TestJsonlMailRoute:
    @pytest.mark.asyncio
    async def test_bound_send_creates_inbox_file(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        sender = route.bind("sender")
        await sender.send("receiver", "hello")
        assert (tmp_path / "receiver.jsonl").exists()

    @pytest.mark.asyncio
    async def test_receive_nowait_returns_none_when_empty(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        mailbox = route.bind("agent_a")
        result = await mailbox.receive_nowait()
        assert result is None

    @pytest.mark.asyncio
    async def test_bound_send_stamps_sender(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        sender = route.bind("alice")
        receiver = route.bind("bob")
        await receiver.receive_nowait()

        await sender.send(
            "bob",
            "ping",
            content_type="research",
            conversation_id="thread-1",
        )
        result = await receiver.receive_nowait()

        assert result is not None
        assert result.sender == "alice"
        assert result.recipient == "bob"
        assert result.content == "ping"
        assert result.content_type == "research"
        assert result.conversation_id == "thread-1"

    @pytest.mark.asyncio
    async def test_cursor_advances(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        sender = route.bind("alice")
        receiver = route.bind("bob")
        await receiver.receive_nowait()

        await sender.send("bob", "first")
        await sender.send("bob", "second")

        r1 = await receiver.receive_nowait()
        r2 = await receiver.receive_nowait()
        r3 = await receiver.receive_nowait()

        assert r1 is not None
        assert r2 is not None
        assert r1.sender == "alice"
        assert r1.content == "first"
        assert r2.sender == "alice"
        assert r2.content == "second"
        assert r3 is None

    @pytest.mark.asyncio
    async def test_multiple_senders(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        alice = route.bind("alice")
        charlie = route.bind("charlie")
        bob = route.bind("bob")
        await bob.receive_nowait()

        await alice.send("bob", "from alice")
        await charlie.send("bob", "from charlie")

        r1 = await bob.receive_nowait()
        r2 = await bob.receive_nowait()

        assert r1 is not None
        assert r2 is not None
        senders = {r1.sender, r2.sender}
        assert senders == {"alice", "charlie"}

    @pytest.mark.asyncio
    async def test_receive_blocks_then_returns(self, tmp_path):
        """receive() should block until a message appears, then return."""
        route = JsonlMailRoute(store_dir=tmp_path)
        sender = route.bind("alice")
        receiver = route.bind("bob")

        async def delayed_send():
            await asyncio.sleep(0.3)
            await sender.send("bob", "delayed")

        asyncio.create_task(delayed_send())
        result = await asyncio.wait_for(receiver.receive(), timeout=2.0)
        assert result.sender == "alice"
        assert result.content == "delayed"

    @pytest.mark.asyncio
    async def test_privileged_deliver_preserves_sender(self, tmp_path):
        route = JsonlMailRoute(store_dir=tmp_path)
        receiver = route.bind("bob")
        await receiver.receive_nowait()

        await route.deliver(Envelope(sender="relay@system", recipient="bob", content="admin"))

        result = await receiver.receive_nowait()
        assert result is not None
        assert result.sender == "relay@system"
        assert result.content == "admin"
