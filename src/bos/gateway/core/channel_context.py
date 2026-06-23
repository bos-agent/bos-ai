from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bos.core import MailRoute

from .actor_resolver import ActorResolver
from .chat_coordinator import ChatCoordinator


@dataclass(frozen=True)
class ChannelRuntimeContext:
    actor_resolver: ActorResolver
    chat_coordinator: ChatCoordinator
    mail_route: MailRoute
    state_changed: Callable[[], Awaitable[None]] | None = None
