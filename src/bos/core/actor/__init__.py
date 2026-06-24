"""The actor ring — the system foundation (BEP 13 §2).

A zero-dependency peer of the agent ring: it imports the stdlib and itself
only — not ``bos.core.agent``, not the harness/outer rings. Outer rings import
``Envelope``/``MessageType`` *from* it, never the reverse, so the actor
foundation can be lifted out to build other long-lived component systems with
no agent/conversation baggage.

Owns the messaging primitives (``Envelope``, ``MessageType``, ``MailBox``,
``EventBus``, ``Event``, and the base ``Actor``). Domain specializations like
``AgentActor`` compose this with the agent ring and live one ring out (the
gateway).
"""

from __future__ import annotations

from .base import Actor
from .envelope import Envelope
from .event_bus import Event, EventBus
from .mailbox import MailBox
from .message_types import MessageType

__all__ = [
    "Actor",
    "Envelope",
    "Event",
    "EventBus",
    "MailBox",
    "MessageType",
]
