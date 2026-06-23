"""Gateway runtime components for BEP 7."""

from .actors.actor_manager import ActorManager, ManagedActor
from .actors.agent_actor import AgentActor
from .channels.channel_manager import ChannelFactoryError, ChannelManager, ChannelStatus, ManagedChannel
from .channels.ws_channel import WS_TAKEOVER_CLOSE_CODE, WS_TAKEOVER_CLOSE_REASON, WSChannel
from .client import GatewayClient
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
