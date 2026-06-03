from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bos.config.workspace import ResolvedActorConfig
from bos.core import ActorTurnContext, ActorTurnResult, AgentActor, MailBox
from bos.protocol import Envelope

from .agent import GatewayActorIdentity, GatewayAgent
from .chat_coordinator import ChannelConversationRef, ChatCoordinationError, ChatCoordinator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bos.config import Workspace
    from bos.core import AgentHarness

logger = logging.getLogger(__name__)


class CoordinatedActor(AgentActor):
    """AgentActor variant that fences turns through ChatCoordinator hooks."""

    def __init__(self, *args: Any, chat_coordinator: ChatCoordinator, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._chat_coordinator = chat_coordinator

    async def _on_turn_started(self, ctx: ActorTurnContext) -> None:
        ref = _ctx_channel_ref(ctx)
        if ref is None:
            raise ChatCoordinationError("Gateway actor turn is missing channel metadata.")
        if ctx.base_revision is None:
            raise ChatCoordinationError("Gateway actor turn is missing base_revision.")
        await self._chat_coordinator.begin_turn(
            chat_id=ctx.chat_id,
            ref=ref,
            actor=ctx.actor_name,
            turn_id=ctx.turn_id,
            base_revision=ctx.base_revision,
        )

    async def _on_turn_finished(self, ctx: ActorTurnContext, result: ActorTurnResult) -> None:
        self._chat_coordinator.end_turn(
            chat_id=ctx.chat_id,
            turn_id=ctx.turn_id,
            committed_revision=result.committed_revision,
        )

    def _turn_metadata(self, reply_recipient: str, inbound_env: Envelope | None = None) -> dict[str, Any]:
        metadata = super()._turn_metadata(reply_recipient, inbound_env)
        actor_name = self._actor_name()
        metadata["assistant_message_metadata"] = {
            "actor": actor_name,
            "actor_address": self._address,
        }
        if inbound_env is not None:
            target_actor = inbound_env.metadata.get("target_actor")
            target_display = inbound_env.metadata.get("target_display")
            if isinstance(target_actor, str) and target_actor:
                user_metadata: dict[str, Any] = {"target_actor": target_actor}
                if isinstance(target_display, str) and target_display:
                    user_metadata["target_display"] = target_display
                metadata["user_message_metadata"] = user_metadata
                if target_actor == actor_name and isinstance(target_display, str) and target_display:
                    metadata["assistant_message_metadata"]["actor_display"] = target_display
        return metadata


@dataclass
class ManagedActor:
    name: str
    config: ResolvedActorConfig
    mailbox: MailBox | None = None
    actor: CoordinatedActor | None = None
    task: asyncio.Task[None] | None = None
    status: str = "configured"
    restart_count: int = 0
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent": self.config.agent,
            "display_name": self.config.display_name,
            "status": self.status,
            "address": self.config.address,
            "active_turns": 0,
            "restart_count": self.restart_count,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


class ActorManager:
    """Gateway-owned actor lifecycle manager."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        harness: AgentHarness,
        chat_coordinator: ChatCoordinator,
        state_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.workspace = workspace
        self.harness = harness
        self.chat_coordinator = chat_coordinator
        self._state_changed = state_changed
        self._actors = {
            name: ManagedActor(name=name, config=config)
            for name, config in workspace.resolve_gateway_actors().items()
        }

    @property
    def actors(self) -> dict[str, ManagedActor]:
        return dict(self._actors)

    async def start_all(self) -> None:
        for record in self._actors.values():
            if record.task is None:
                await self._start_record(record)
        await self._notify_state_changed()

    async def stop_all(self) -> None:
        tasks = [record.task for record in self._actors.values() if record.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for record in self._actors.values():
            record.status = "stopped"
            record.task = None
            record.actor = None
            record.mailbox = None
        await self._notify_state_changed()

    def status_payload(self) -> dict[str, dict[str, Any]]:
        payload = {name: record.snapshot() for name, record in self._actors.items()}
        active_counts: dict[str, int] = {}
        for active in self.chat_coordinator.active_turns_status().values():
            actor = active.get("actor")
            if isinstance(actor, str):
                active_counts[actor] = active_counts.get(actor, 0) + 1
        for name, count in active_counts.items():
            if name in payload:
                payload[name]["active_turns"] = count
        return payload

    async def _start_record(self, record: ManagedActor) -> None:
        if self.harness.mail_route is None:
            raise RuntimeError("ActorManager requires an active AgentHarness mail_route service.")
        agent_cfg = dict(record.config.agent_overrides)
        agent_cfg["agent_name"] = record.name
        agent_cfg["actor_identity"] = _actor_identity(record)
        agent_cfg["actor_roster"] = [_actor_identity(actor) for actor in self._actors.values()]
        agent = await self.harness.create_agent(record.config.agent, agent_cfg=agent_cfg, agent_cls=GatewayAgent)
        mailbox = self.harness.mail_route.bind(record.config.address)
        record.mailbox = mailbox
        record.actor = CoordinatedActor(agent, mailbox, chat_coordinator=self.chat_coordinator)
        record.status = "running"
        record.error = None
        record.task = asyncio.create_task(self._run_record(record), name=f"bos-actor:{record.name}")

    async def _run_record(self, record: ManagedActor) -> None:
        assert record.actor is not None
        try:
            await record.actor.run()
        except asyncio.CancelledError:
            record.status = "stopped"
            raise
        except Exception as exc:
            await self._handle_actor_failure(record, exc)

    async def _handle_actor_failure(self, record: ManagedActor, exc: BaseException) -> None:
        self.chat_coordinator.clear_active_turns(actor=record.name)
        record.error = str(exc)
        record.status = "error"
        logger.exception("Actor %s failed", record.name)
        await self._notify_state_changed()

        if not record.config.restart_on_error or record.restart_count >= record.config.max_restarts:
            record.task = None
            return

        record.restart_count += 1
        record.status = "restarting"
        await self._notify_state_changed()
        await self._start_record(record)

    async def _notify_state_changed(self) -> None:
        if self._state_changed is not None:
            await self._state_changed()


def _ctx_channel_ref(ctx: ActorTurnContext) -> ChannelConversationRef | None:
    raw = ctx.channel_ref
    if not isinstance(raw, dict):
        return None
    channel_id = raw.get("channel_id")
    conversation_id = raw.get("channel_conversation_id")
    if not isinstance(channel_id, str) or not isinstance(conversation_id, str):
        return None
    return ChannelConversationRef(channel_id=channel_id, channel_conversation_id=conversation_id)


def _actor_identity(record: ManagedActor) -> GatewayActorIdentity:
    return GatewayActorIdentity(
        name=record.name,
        display_name=record.config.display_name,
        agent_kind=record.config.agent,
    )
