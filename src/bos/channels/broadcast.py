"""BroadcastChannel — virtual mediator that multiplexes channels behind one address.

Sits between real channels and the actor.  All channels send **to** the
broadcast address (instead of directly to the actor).  BroadcastChannel:

- **Inbound** (sender is a member channel):
  1. *Echo* the message to all **other** member channels
     (``content_type="echo"``).
  2. *Rewrite* ``sender`` to the broadcast address and *forward* to the actor.

- **Outbound** (sender is the actor / not a member):
  1. *Fan out* a copy to every member channel.

This keeps the actor completely channel-agnostic — it only ever sees
``sender=channel@user`` — which is critical for multi-agent systems where
agent-to-agent mail must not leak to user-facing channels.
"""

from __future__ import annotations

import asyncio
import logging

from bos.core import Envelope, Mailbox

logger = logging.getLogger(__name__)


class BroadcastChannel:
    """Reads from a shared address, echoes inbound, fans out outbound."""

    def __init__(self, member_addresses: list[str], actor_address: str):
        self._members = set(member_addresses)
        self._actor = actor_address

    async def run(self, mailbox: Mailbox, address: str) -> None:
        """Main mediator loop."""
        logger.info(
            "BroadcastChannel %r → %s (actor=%s)",
            address,
            ", ".join(sorted(self._members)),
            self._actor,
        )
        try:
            while True:
                env = await mailbox.receive(address)

                if env.sender in self._members:
                    # ── Inbound: from a real channel ──
                    origin = env.sender

                    # 1. Echo to other members
                    for member in self._members:
                        if member != origin:
                            await mailbox.send(
                                Envelope(
                                    sender=address,
                                    recipient=member,
                                    content=env.content,
                                    content_type="echo",
                                    conversation_id=env.conversation_id,
                                )
                            )

                    # 2. Rewrite sender and forward to actor
                    await mailbox.send(
                        Envelope(
                            sender=address,
                            recipient=self._actor,
                            content=env.content,
                            content_type=env.content_type,
                            conversation_id=env.conversation_id,
                        )
                    )

                else:
                    # ── Outbound: from the actor (or any non-member) ──
                    for member in self._members:
                        await mailbox.send(
                            Envelope(
                                sender=env.sender,
                                recipient=member,
                                content=env.content,
                                content_type=env.content_type,
                                conversation_id=env.conversation_id,
                            )
                        )

        except asyncio.CancelledError:
            pass
