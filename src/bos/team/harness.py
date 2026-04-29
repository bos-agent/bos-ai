from __future__ import annotations

from pathlib import Path
from typing import Any

from bos.core.contract import ep_agent
from bos.core.harness import AgentHarness

from .runtime import PeerTaskRuntime
from .tasks import ActorRef, TaskLedger, is_task_chat_id
from .tools import peer_task_tool_names_for_role, register_peer_task_tools


class TeamHarness(AgentHarness):
    """AgentHarness extension that owns the optional peer task protocol."""

    def __init__(
        self,
        *,
        actors: list[dict[str, Any]] | None = None,
        task_ledger: dict[str, Any] | str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.actor_registry = self._create_actor_registry(actors)
        self.peer_tasks_enabled = len(self.actor_registry) > 1
        self.task_ledger = TaskLedger(self._resolve_task_ledger_path(task_ledger))

    def actor_runtime_for(self, actor_address: str) -> PeerTaskRuntime:
        return PeerTaskRuntime(self.task_ledger, actor_address)

    def is_reserved_chat_id(self, chat_id: str) -> bool:
        return is_task_chat_id(chat_id)

    def _prepare_agent_cfg(self, agent_name: str | None, agent_cfg: dict[str, Any]) -> dict[str, Any]:
        cfg = dict(agent_cfg)
        configured_tools = cfg.get("tools")
        if "tools" not in cfg and agent_name and ep_agent.has(agent_name):
            configured_tools = ep_agent.get(agent_name).defaults.get("tools")
        if not isinstance(configured_tools, list):
            return cfg

        peer_tools = self._peer_task_tool_names_for_agent(agent_name or cfg.get("name"))
        if not peer_tools:
            return cfg

        tools = list(configured_tools)
        tools.extend(tool_name for tool_name in peer_tools if tool_name not in tools)
        cfg["tools"] = tools
        return cfg

    def _create_local_tools(self, agent_name: str | None = None):
        tools = super()._create_local_tools(agent_name=agent_name)
        if self.peer_tasks_enabled:
            register_peer_task_tools(tools, self, agent_name or "__unknown__")
        return tools

    def _peer_task_tool_names_for_agent(self, agent_name: str | None) -> tuple[str, ...]:
        if not self.peer_tasks_enabled or not agent_name:
            return ()

        matched_actor = next(
            (
                actor
                for actor in self.actor_registry.values()
                if actor.name == agent_name or actor.agent == agent_name
            ),
            None,
        )
        if matched_actor is None:
            return ()
        return peer_task_tool_names_for_role(matched_actor.role)

    def _resolve_task_ledger_path(self, cfg: dict[str, Any] | str | Path | None) -> Path | None:
        if cfg is None:
            return self._bos_root / "state" / "tasks.jsonl"
        if isinstance(cfg, (str, Path)):
            return Path(cfg).expanduser().resolve()
        if isinstance(cfg, dict):
            path = cfg.get("path")
            if path in (None, ""):
                return self._bos_root / "state" / "tasks.jsonl"
            task_path = Path(str(path)).expanduser()
            return task_path if task_path.is_absolute() else (self._bos_root / task_path).resolve()
        raise TypeError("task_ledger must be a path, table, or None.")

    @staticmethod
    def _create_actor_registry(actors: list[dict[str, Any]] | None) -> dict[str, ActorRef]:
        if not actors:
            return {
                "agent@main": ActorRef(
                    name="main",
                    agent="main",
                    address="agent@main",
                    role="coordinator",
                )
            }
        registry: dict[str, ActorRef] = {}
        for raw_actor in actors:
            name = str(raw_actor["name"])
            ref = ActorRef(
                name=name,
                agent=raw_actor.get("agent"),
                address=str(raw_actor["address"]),
                role="coordinator" if name == "main" else "worker",
                description=raw_actor.get("description"),
                capabilities=list(raw_actor.get("capabilities") or []),
            )
            registry[ref.address] = ref
        return registry
