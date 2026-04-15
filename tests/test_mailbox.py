"""Tests for Mailbox protocol, JsonlMailbox, and mailbox workers."""

from __future__ import annotations

import asyncio

import pytest

from bos.core import Envelope, Mailbox
from bos.mailboxes.jsonl_mailbox import JsonlMailbox


class FakeAgent:
    def __init__(self, response: str = "ack"):
        self._response = response
        self.calls: list[tuple[str, str]] = []

    async def ask(self, conversation_id: str, message: str) -> str:
        self.calls.append((conversation_id, message))
        return self._response


class TestMailboxProtocol:
    def test_jsonl_mailbox_satisfies_protocol(self, tmp_path):
        mailbox = JsonlMailbox(store_dir=tmp_path)
        assert isinstance(mailbox, Mailbox)


class TestJsonlMailbox:
    @pytest.mark.asyncio
    async def test_send_creates_inbox_file(self, tmp_path):
        mailbox = JsonlMailbox(store_dir=tmp_path)
        await mailbox.send(Envelope(sender="sender", recipient="receiver", content="hello"))
        assert (tmp_path / "receiver.jsonl").exists()

    @pytest.mark.asyncio
    async def test_receive_nowait_returns_none_when_empty(self, tmp_path):
        mailbox = JsonlMailbox(store_dir=tmp_path)
        result = await mailbox.receive_nowait("agent_a")
        assert result is None

    @pytest.mark.asyncio
    async def test_send_and_receive_nowait(self, tmp_path):
        sender = JsonlMailbox(store_dir=tmp_path)
        receiver = JsonlMailbox(store_dir=tmp_path)
        await receiver.receive_nowait("bob")

        await sender.send(
            Envelope(
                sender="alice",
                recipient="bob",
                content="ping",
                content_type="research",
                conversation_id="thread-1",
            )
        )
        result = await receiver.receive_nowait("bob")

        assert result is not None
        assert result.sender == "alice"
        assert result.recipient == "bob"
        assert result.content == "ping"
        assert result.content_type == "research"
        assert result.conversation_id == "thread-1"

    @pytest.mark.asyncio
    async def test_cursor_advances(self, tmp_path):
        sender = JsonlMailbox(store_dir=tmp_path)
        receiver = JsonlMailbox(store_dir=tmp_path)
        await receiver.receive_nowait("bob")

        await sender.send(Envelope(sender="alice", recipient="bob", content="first"))
        await sender.send(Envelope(sender="alice", recipient="bob", content="second"))

        r1 = await receiver.receive_nowait("bob")
        r2 = await receiver.receive_nowait("bob")
        r3 = await receiver.receive_nowait("bob")

        assert r1 is not None
        assert r2 is not None
        assert r1.sender == "alice"
        assert r1.content == "first"
        assert r2.sender == "alice"
        assert r2.content == "second"
        assert r3 is None  # no more messages

    @pytest.mark.asyncio
    async def test_multiple_senders(self, tmp_path):
        alice = JsonlMailbox(store_dir=tmp_path)
        charlie = JsonlMailbox(store_dir=tmp_path)
        bob = JsonlMailbox(store_dir=tmp_path)
        await bob.receive_nowait("bob")

        await alice.send(Envelope(sender="alice", recipient="bob", content="from alice"))
        await charlie.send(Envelope(sender="charlie", recipient="bob", content="from charlie"))

        r1 = await bob.receive_nowait("bob")
        r2 = await bob.receive_nowait("bob")

        assert r1 is not None
        assert r2 is not None
        senders = {r1.sender, r2.sender}
        assert senders == {"alice", "charlie"}

    @pytest.mark.asyncio
    async def test_receive_blocks_then_returns(self, tmp_path):
        """receive() should block until a message appears, then return."""
        sender = JsonlMailbox(store_dir=tmp_path)
        receiver = JsonlMailbox(store_dir=tmp_path)

        async def delayed_send():
            await asyncio.sleep(0.3)
            await sender.send(Envelope(sender="alice", recipient="bob", content="delayed"))

        asyncio.create_task(delayed_send())
        result = await asyncio.wait_for(receiver.receive("bob"), timeout=2.0)
        assert result.sender == "alice"
        assert result.content == "delayed"
