"""BroadcastChannel — virtual mediator that multiplexes channels behind one address.

For single-user setups this channel owns one canonical conversation id shared by
all member channels. Each channel may still keep a channel-local conversation id
for delivery purposes (for example Telegram uses ``telegram:<chat_id>``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from bos.core import MailBox
from bos.protocol import ChannelCommandName, Envelope, MessageType

logger = logging.getLogger(__name__)


class BroadcastChannel:
    """Reads from a shared address, syncs conversation ids, echoes inbound, fans out outbound."""

    def __init__(self, member_addresses: list[str], actor_address: str):
        self._members = set(member_addresses)
        self._actor = actor_address
        self._conversation_id = self._new_conversation_id()
        self._member_conversation_ids: dict[str, str] = {}

    async def run(self, mailbox: MailBox) -> None:
        """Main mediator loop."""
        address = mailbox.address
        logger.info(
            "BroadcastChannel %r → %s (actor=%s, conversation_id=%s)",
            address,
            ", ".join(sorted(self._members)),
            self._actor,
            self._conversation_id,
        )
        try:
            while True:
                env = await mailbox.receive()

                if env.sender in self._members:
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

                else:
                    for member in self._members:
                        await mailbox.send(
                            member,
                            env.content,
                            content_type=env.content_type,
                            conversation_id=self._conversation_for_member(member),
                        )

        except asyncio.CancelledError:
            pass

    def _remember_member_conversation(self, member: str, conversation_id: str | None) -> None:
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
