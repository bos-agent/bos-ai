from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bos.core import ChatStore

from .chat_coordinator import ChannelConversationRef, ChatCoordinator

# Retire a chat's session on the target actor (cancel any in-flight turn and
# emit session_close). Injected by the gateway over the ActorManager.
RetireSession = Callable[[str, str], Awaitable[None]]  # (target_actor, chat_id)


@dataclass(frozen=True)
class CommandResult:
    name: str
    ok: bool
    chat_id: str | None = None
    result: Any = None
    error: str | None = None


class CommandHandler:
    """Gateway control plane for client slash-commands (BEP 13 / OPEN-D).

    Mailbox-free: a channel detects a leading ``/`` in inbound content and calls
    :meth:`run` directly. Commands never become ``COMMAND`` envelopes and never
    reach the agent actor, which is pure data plane. Client→chat cursor state is
    owned solely by the ``ChatCoordinator`` (no parallel ``ChatState``).
    """

    def __init__(self, coordinator: ChatCoordinator, chat_store: ChatStore, retire_session: RetireSession) -> None:
        self._coordinator = coordinator
        self._chat_store = chat_store
        self._retire = retire_session

    async def run(self, ref: ChannelConversationRef, command: str, *, target_actor: str) -> CommandResult:
        parts = command.split(None, 1)
        name = parts[0].lstrip("/") if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            if name == "new":
                return await self._new(ref, target_actor)
            if name == "resume":
                return await self._resume(ref, arg, target_actor)
            if name == "chats":
                return await self._chats()
            return CommandResult(name=name, ok=False, error=f"Invalid command `{name}`")
        except Exception as exc:
            return CommandResult(name=name, ok=False, error=str(exc))

    async def _new(self, ref: ChannelConversationRef, target_actor: str) -> CommandResult:
        old = self._coordinator.get_cursor(ref)
        chat_id = self._coordinator.new_chat(ref)
        if old:
            await self._retire(target_actor, old)
        return CommandResult(name="new", ok=True, chat_id=chat_id, result="chat reset")

    async def _resume(self, ref: ChannelConversationRef, arg: str, target_actor: str) -> CommandResult:
        if not arg:
            return CommandResult(name="resume", ok=False, error="Usage: /resume <chat-id>")
        old = self._coordinator.get_cursor(ref)
        revision = await self._coordinator.current_revision(arg)
        self._coordinator.set_cursor(ref, arg, observed_revision=revision)
        if old and old != arg:
            await self._retire(target_actor, old)
        return CommandResult(name="resume", ok=True, chat_id=arg, result=f"resumed {arg}")

    async def _chats(self) -> CommandResult:
        chats = await self._chat_store.list_chats()
        metas = sorted(
            chats.values(),
            key=lambda m: (m.last_activity is not None, m.last_activity),
            reverse=True,
        )
        result = [
            {
                "chat_id": m.chat_id,
                "message_count": m.message_count,
                "last_activity": m.last_activity.isoformat() if m.last_activity else None,
                "description": m.description,
            }
            for m in metas
        ]
        return CommandResult(name="chats", ok=True, result=result)
