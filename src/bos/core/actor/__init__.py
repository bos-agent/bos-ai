"""The actor ring — the system foundation (BEP 13 §2).

A zero-dependency peer of the agent ring: it imports the stdlib and itself
only — not ``bos.protocol``, not ``bos.core.agent``. ``bos.protocol`` depends on
*it* (re-exporting ``Envelope``/``MessageType``), never the reverse, so the
actor foundation can be lifted out to build other long-lived component systems
with no agent/conversation baggage.

Owns the messaging primitives (``Envelope``, ``MessageType``, and — landing in
later increments — ``MailBox``, ``EventBus``, ``Event``, and the base
``Actor``). Domain specializations like ``AgentActor`` compose this with the
agent ring and live one ring out (the harness ring).
"""

from __future__ import annotations

from .envelope import Envelope
from .mailbox import MailBox
from .message_types import MessageType

__all__ = [
    "Envelope",
    "MailBox",
    "MessageType",
]
