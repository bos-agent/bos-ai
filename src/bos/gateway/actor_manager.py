from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bos.config.workspace import ResolvedActorConfig
from bos.core import MailBox

from .agent_actor import AgentActor
from .chat_coordinator import ChatCoordinator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from bos.config import Workspace
    from bos.core import AgentHarness

logger = logging.getLogger(__name__)


@dataclass
class ManagedActor:
    name: str
    config: ResolvedActorConfig
    mailbox: MailBox | None = None
    actor: AgentActor | None = None
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
            name: ManagedActor(name=name, config=config) for name, config in workspace.resolve_gateway_actors().items()
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
        agent_cfg["history_attribution"] = len(self._actors) > 1
        agent = await self.harness.create_agent(record.config.agent, agent_cfg=agent_cfg)
        mailbox = self.harness.mail_route.bind(record.config.address)
        record.mailbox = mailbox
        bus = getattr(self.harness, "events", None)

        record.actor = AgentActor(
            agent,
            mailbox,
            lifecycle_bus=bus,
            chat_coordinator=self.chat_coordinator,
        )
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
