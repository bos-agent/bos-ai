from __future__ import annotations

import asyncio
import logging
import uuid

from bos.protocol import ChannelCommandName, Envelope, MessageType

from .contract import MailBox, ep_channel

logger = logging.getLogger(__name__)


@ep_channel(name="BroadcastChannel")
class BroadcastChannel:
    """Sync conversation ids within one group and fan actor replies back to known members."""

    def __init__(self, target_address: str) -> None:
        self._members: set[str] = set()
        self._actor = target_address
        self._conversation_id = self._new_conversation_id()
        self._member_conversation_ids: dict[str, str] = {}

    async def run(self, mailbox: MailBox) -> None:
        """Main mediator loop."""
        address = mailbox.address
        logger.info("BroadcastChannel %r -> %s (conversation_id=%s)", address, self._actor, self._conversation_id)
        try:
            while True:
                env = await mailbox.receive()

                if env.sender == self._actor:
                    for member in self._members:
                        await mailbox.send(
                            member,
                            env.content,
                            content_type=env.content_type,
                            conversation_id=self._conversation_for_member(member),
                        )
                    continue

                origin = env.sender
                self._remember_member_conversation(origin, env.conversation_id)

                if self._is_new_conversation_request(env):
                    self._rotate_conversation(origin, requested_local_id=env.conversation_id)
                    await self._notify_new_conversation(mailbox)
                    continue

                for member in self._members:
                    if member != origin:
                        await mailbox.send(
                            member,
                            env.content,
                            content_type=MessageType.ECHO,
                            conversation_id=self._conversation_for_member(member),
                        )

                await mailbox.send(
                    self._actor,
                    env.content,
                    content_type=env.content_type,
                    conversation_id=self._conversation_id,
                )

        except asyncio.CancelledError:
            pass

    def _remember_member_conversation(self, member: str, conversation_id: str | None) -> None:
        self._members.add(member)
        if conversation_id:
            self._member_conversation_ids[member] = conversation_id

    def _conversation_for_member(self, member: str) -> str:
        return self._member_conversation_ids.get(member, self._conversation_id)

    def _is_new_conversation_request(self, env: Envelope) -> bool:
        return (
            env.content_type == MessageType.CHANNEL_COMMAND
            and env.content.strip() == ChannelCommandName.NEW_CONVERSATION
            or env.content_type == MessageType.COMMAND
            and env.content.strip() == "/new"
        )

    def _rotate_conversation(self, origin: str, requested_local_id: str | None = None) -> None:
        self._conversation_id = self._new_conversation_id()
        for member in self._members:
            current = self._member_conversation_ids.get(member)
            if member == origin:
                if requested_local_id and not self._is_sticky_local_id(requested_local_id):
                    self._member_conversation_ids[member] = requested_local_id
                elif not self._is_sticky_local_id(current):
                    self._member_conversation_ids[member] = self._conversation_id
                continue

            if not self._is_sticky_local_id(current):
                self._member_conversation_ids[member] = self._conversation_id

    async def _notify_new_conversation(self, mailbox: MailBox) -> None:
        for member in self._members:
            await mailbox.send(
                member,
                "Started a new shared conversation.",
                content_type=MessageType.SYSTEM,
                conversation_id=self._conversation_for_member(member),
            )

    @staticmethod
    def _is_sticky_local_id(conversation_id: str | None) -> bool:
        return bool(conversation_id and conversation_id.startswith("telegram:"))

    @staticmethod
    def _new_conversation_id() -> str:
        return uuid.uuid4().hex
