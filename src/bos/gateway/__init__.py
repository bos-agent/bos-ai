"""Gateway runtime components for BEP 7."""

from typing import TYPE_CHECKING, Any

from .actors.actor_manager import ActorManager, ManagedActor
from .actors.agent_actor import AgentActor
from .channels.channel_manager import ChannelFactoryError, ChannelManager, ChannelStatus, ManagedChannel
from .channels.ws_channel import WS_TAKEOVER_CLOSE_CODE, WS_TAKEOVER_CLOSE_REASON, WSChannel
from .config import (
    GatewayRuntimeConfig,
    ResolvedActorConfig,
    ResolvedGatewayChannelConfig,
    ResolvedGatewayConfig,
)
from .core.actor_resolver import ActorDescriptor, ActorResolutionError, ActorResolver, ActorRouteResult
from .core.channel_context import ChannelRuntimeContext
from .core.chat_coordinator import (
    ActiveTurn,
    ChannelConversationRef,
    ChatCoordinationError,
    ChatCoordinator,
    PrepareSendResult,
)
from .gateway import Gateway

if TYPE_CHECKING:
    from .client import GatewayClient

__all__ = [
    "ActiveTurn",
    "ActorDescriptor",
    "ActorResolutionError",
    "ActorResolver",
    "ActorRouteResult",
    "ActorManager",
    "AgentActor",
    "ChannelFactoryError",
    "ChannelManager",
    "ChannelConversationRef",
    "ChannelRuntimeContext",
    "ChannelStatus",
    "Gateway",
    "GatewayClient",
    "ChatCoordinationError",
    "ChatCoordinator",
    "GatewayRuntimeConfig",
    "ManagedChannel",
    "ManagedActor",
    "PrepareSendResult",
    "ResolvedActorConfig",
    "ResolvedGatewayChannelConfig",
    "ResolvedGatewayConfig",
    "WSChannel",
    "WS_TAKEOVER_CLOSE_CODE",
    "WS_TAKEOVER_CLOSE_REASON",
]


def __getattr__(name: str) -> Any:
    """Import the wire client lazily, so mounting the server does not pull it in.

    ``bos.gateway`` is the server surface; ``GatewayClient`` is what the TUI uses
    to talk *to* a gateway. Importing it eagerly made every mounted gateway
    import httpx and websockets for nothing (BEP 17 §3.9). Both
    ``from bos.gateway import GatewayClient`` and
    ``from bos.gateway.client import GatewayClient`` keep working, and
    ``__all__`` is unchanged.
    """
    if name == "GatewayClient":
        from .client import GatewayClient

        return GatewayClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
