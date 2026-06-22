from typing import TYPE_CHECKING, Any

# ``bos.protocol`` is a downstream facade over the two foundation rings — it owns
# no types of its own. ``Envelope``/``MessageType`` are owned by the actor
# foundation (``bos.core.actor``); ``MessageContent``/``MessageContentPart``/
# ``TurnEvent`` by the agent core (``bos.core.agent``). All are re-exported here
# lazily (via ``__getattr__``) so importing ``bos.protocol`` never triggers
# ``bos.core`` at module-init time, keeping it a leaf and avoiding import-order
# deadlock. In-tree ``bos.core`` modules import these from their owning ring
# directly; the lazy re-export serves outer rings and back-compat call sites.

if TYPE_CHECKING:
    from bos.core.actor import Envelope, MessageType
    from bos.core.agent import MessageContent, MessageContentPart, TurnEvent

WS_TAKEOVER_CLOSE_CODE = 4001
WS_TAKEOVER_CLOSE_REASON = "Another interactive channel took over this WebSocket session."

_LAZY_FROM_AGENT = ("MessageContent", "MessageContentPart", "TurnEvent")
_LAZY_FROM_ACTOR = ("Envelope", "MessageType")

__all__ = [
    "Envelope",
    "MessageContent",
    "MessageContentPart",
    "MessageType",
    "TurnEvent",
    "WS_TAKEOVER_CLOSE_CODE",
    "WS_TAKEOVER_CLOSE_REASON",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY_FROM_AGENT:
        import bos.core.agent as _agent

        return getattr(_agent, name)
    if name in _LAZY_FROM_ACTOR:
        import bos.core.actor as _actor

        return getattr(_actor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
