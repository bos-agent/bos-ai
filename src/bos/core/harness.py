from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from textwrap import dedent
from typing import Any, Literal

from bos.protocol import MessageType

from ._utils import _aclose, _create_extension_instance, _load_ext_modules, _load_ext_paths, _safe_format
from .agent import ChainReactInterceptor, ReactAgent
from .contract import Consolidator, EventSink, MailBox, MailRoute, MemoryStore, MessageStore, SkillsLoader, ep_agent
from .events import derive_event_sink
from .llm import LLMClient
from .registry import ToolRegistry
from .tasks import ActorRef, TaskLedger, TaskLedgerError, task_chat_id, task_metadata

logger = logging.getLogger(__name__)


def bootstrap_platform(
    bos_dir: str | Path = ".bos",
    envs: dict[str, str] | None = None,
    envfile: str | None = None,
    extensions: list[str] | None = None,
    agents: list[dict[str, Any]] | None = None,
    agent_defaults: dict[str, Any] | None = None,
) -> None:
    bos_root = Path(bos_dir).expanduser().resolve()
    bos_root.mkdir(parents=True, exist_ok=True)

    if envs:
        os.environ.update(envs)
    if envfile:
        from dotenv import load_dotenv

        load_dotenv((bos_root / Path(envfile).expanduser()).resolve())

    if extensions:
        modules, paths = [], []
        for ext in extensions:
            p = bos_root / Path(ext).expanduser()
            if p.exists():
                paths.append(p)
            else:
                modules.append(ext)
        if modules:
            _load_ext_modules(modules=modules)
        if paths:
            _load_ext_paths(paths=paths)

    if agents:
        defaults = agent_defaults or {}
        for agent_spec in agents:
            ReactAgent.register(**(defaults | agent_spec))


CURRENT_HARNESS: contextvars.ContextVar[AgentHarness] = contextvars.ContextVar("current_harness")
CURRENT_MAILBOX: contextvars.ContextVar[MailBox] = contextvars.ContextVar("current_mailbox")


class AgentHarness:
    """Lifecycle-owning container for shared agent services."""

    def __init__(
        self,
        *,
        mail_route: dict[str, Any] | None = None,
        message_store: dict[str, Any] | None = None,
        memory_store: dict[str, Any] | None = None,
        consolidator: dict[str, Any] | None = None,
        skills_loader: dict[str, Any] | None = None,
        providers: dict[str, dict[str, Any]] | None = None,
        interceptors: list[str | dict[str, Any]] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
        subagents: list[dict[str, Any]] | None = None,
        actors: list[dict[str, Any]] | None = None,
        task_ledger: dict[str, Any] | str | Path | None = None,
        capability_mode: Literal["defensive", "offensive"] = "defensive",
    ) -> None:
        if capability_mode not in {"defensive", "offensive"}:
            raise ValueError("capability_mode must be 'defensive' or 'offensive'.")

        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._subagents_cfg = {cfg.get("name", "_default"): cfg for cfg in subagents} if subagents else {}
        self._capability_mode = capability_mode

        self._mail_route_cfg = mail_route
        self._message_store_cfg = message_store
        self._memory_store_cfg = memory_store
        self._consolidator_cfg = consolidator
        self._skills_loader_cfg = skills_loader
        self._providers_cfg = providers
        self._interceptors_cfg = interceptors
        self._tools_cfg = tools or {}
        self._actors_cfg = actors
        self.actor_registry = self._create_actor_registry(actors)
        self.peer_tasks_enabled = len(self.actor_registry) > 1
        self.task_ledger = TaskLedger(self._resolve_task_ledger_path(task_ledger))

        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self._original_cwd: Path | None = None
        self.mail_route = None
        self.message_store = None
        self.memory_store = None
        self.consolidator = None
        self.skills_loader = None
        self.interceptor = None
        self.llm = None

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

    async def __aenter__(self):
        if self._token is not None:
            raise RuntimeError(
                "AgentHarness is already active. Use CURRENT_HARNESS.get() to access "
                "the current harness instead of re-entering."
            )

        self._original_cwd = Path.cwd()
        os.chdir(self._bos_root)

        self.mail_route = self._create_and_own("ep_mail_route", MailRoute, self._mail_route_cfg)
        self.message_store = self._create_and_own("ep_message_store", MessageStore, self._message_store_cfg)
        self.memory_store = self._create_and_own("ep_memory_store", MemoryStore, self._memory_store_cfg)
        self.consolidator = self._create_and_own("ep_consolidator", Consolidator, self._consolidator_cfg)
        self.skills_loader = self._create_and_own("ep_skills_loader", SkillsLoader, self._skills_loader_cfg)
        self.interceptor = ChainReactInterceptor(self._interceptors_cfg)
        self.llm = LLMClient(self._providers_cfg)

        os.chdir(self._workspace)
        self._token = CURRENT_HARNESS.set(self)
        return self

    async def __aexit__(self, *exc) -> None:
        await _aclose(self.interceptor)
        for resource in reversed(self._owned):
            await _aclose(resource)
        self._owned.clear()

        if self._token is not None:
            CURRENT_HARNESS.reset(self._token)
            self._token = None

        if self._original_cwd is not None:
            os.chdir(self._original_cwd)
            self._original_cwd = None

    def create_agent(self, agent_name: str | None = None, agent_cfg: dict[str, Any] = None) -> ReactAgent:
        if CURRENT_HARNESS.get(None) is None:
            raise RuntimeError("create_agent must be called within an active AgentHarness context.")

        if not any([agent_name, agent_cfg]):
            capability_default = [] if self._capability_mode == "defensive" else None
            agent_cfg = {
                "system_prompt": "You are a helpful assistant.",
                "model": os.getenv("BOS_MODEL"),
                "tools": capability_default,
                "skills": capability_default,
                "memories": capability_default,
                "subagents": capability_default,
            }

        local_tools = self._create_local_tools(agent_name=agent_name)
        kwargs = (agent_cfg or {}) | {
            "agent_name": agent_name or (agent_cfg or {}).get("name"),
            "llm": self.llm,
            "message_store": self.message_store,
            "memory_store": self.memory_store,
            "consolidator": self.consolidator,
            "skills_loader": self.skills_loader,
            "interceptor": self.interceptor,
            "local_tools": local_tools,
            "tool_configs": self._tools_cfg,
        }

        return ep_agent.invoke(agent_name, kwargs) if agent_name else ReactAgent(**kwargs)

    def _create_and_own(self, ep_name: str, protocol: type, cfg: Any) -> Any:
        from . import __dict__ as core_exports

        instance = _create_extension_instance(core_exports[ep_name], protocol, cfg)
        if instance is not None:
            self._owned.append(instance)
        return instance

    def _create_local_tools(self, agent_name: str | None = None):
        harness = self
        current_agent_name = agent_name or "__unknown__"
        tools = ToolRegistry("Harness-scoped tools for this agent.")

        def current_actor_address() -> str:
            mailbox = CURRENT_MAILBOX.get(None)
            if mailbox is not None:
                return mailbox.address
            return f"agent@{current_agent_name}"

        def normalize_actor_address(value: str) -> str:
            value = str(value or "").strip()
            if not value:
                raise TaskLedgerError("actor address must be non-empty.")
            address = value if value.startswith("agent@") else f"agent@{value}"
            if address not in harness.actor_registry:
                raise TaskLedgerError(f"Actor {address!r} is not registered.")
            return address

        def coordinator_address() -> str:
            for actor in harness.actor_registry.values():
                if actor.role == "coordinator":
                    return actor.address
            return "agent@main"

        def result_payload(value: Any) -> str:
            return json.dumps(value, default=str)

        @tools(
            name="SendMail",
            description=("Send a message to the recipient's address."),
            parameters={
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "Recipient address"},
                    "content": {"type": "string", "description": "Message content"},
                },
                "required": ["recipient", "content"],
            },
        )
        async def tool_send_mail(recipient: str, content: str) -> str:
            mailbox = CURRENT_MAILBOX.get(None) or harness.mail_route.bind(f"agent@{current_agent_name}")
            await mailbox.send(recipient, content)
            return f"(Sent to {recipient})"

        if harness.peer_tasks_enabled:

            @tools(
                name="ListActors",
                description="List configured peer actors and their current registry status.",
                parameters={"type": "object", "properties": {}, "required": []},
            )
            async def tool_list_actors() -> str:
                return result_payload([asdict(actor) for actor in harness.actor_registry.values()])

            @tools(
                name="CreateTask",
                description=dedent("""
                Create a durable peer task assigned to an actor and dispatch it asynchronously.

                This is the peer-actor delegation primitive. It records the task in
                the task ledger, binds a task-owned worker chat, sends the task to
                the assigned actor, and returns immediately.
                """),
                parameters={
                    "type": "object",
                    "properties": {
                        "assigned_to": {"type": "string", "description": "Actor name or address to receive the task."},
                        "goal": {"type": "string", "description": "Task goal."},
                        "context": {"type": "string", "description": "Optional task context."},
                        "parent_id": {"type": "string", "description": "Optional parent task id."},
                    },
                    "required": ["assigned_to", "goal"],
                },
            )
            async def tool_create_task(
                assigned_to: str,
                goal: str,
                context: str = "",
                parent_id: str | None = None,
                chat_id: str | None = None,
            ) -> str:
                assigned_address = normalize_actor_address(assigned_to)
                created_by = current_actor_address()
                parent = parent_id or None
                task = harness.task_ledger.create_task(
                    goal=goal,
                    created_by=created_by,
                    assigned_to=assigned_address,
                    parent_id=parent,
                    context=context,
                    metadata={"source_chat_id": chat_id} if chat_id else {},
                )
                worker_chat_id = task_chat_id(task.id, "worker")
                binding = harness.task_ledger.bind_chat(
                    task_id=task.id,
                    chat_id=worker_chat_id,
                    actor_address=assigned_address,
                    purpose="worker",
                )
                metadata = task_metadata(task, chat_id=worker_chat_id)
                mailbox = CURRENT_MAILBOX.get(None) or harness.mail_route.bind(created_by)
                await mailbox.send(
                    assigned_address,
                    f"Task {task.id}: {goal}\n\n{context}".strip(),
                    chat_id=worker_chat_id,
                    metadata=metadata,
                )
                return result_payload({"task": asdict(task), "binding": asdict(binding)})

            @tools(
                name="UpdateTask",
                description="Append progress or metadata to a durable task.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "progress": {"type": "string"},
                    },
                    "required": ["task_id", "progress"],
                },
            )
            async def tool_update_task(task_id: str, progress: str) -> str:
                event = harness.task_ledger.append_event(
                    task_id,
                    "progress",
                    actor=current_actor_address(),
                    content=progress,
                )
                return result_payload({"event": asdict(event), "task": asdict(harness.task_ledger.get_task(task_id))})

            @tools(
                name="StartTask",
                description="Mark a durable task as running.",
                parameters={
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            )
            async def tool_start_task(task_id: str) -> str:
                event = harness.task_ledger.append_event(
                    task_id,
                    "started",
                    actor=current_actor_address(),
                    status="running",
                )
                return result_payload({"event": asdict(event), "task": asdict(harness.task_ledger.get_task(task_id))})

            @tools(
                name="WaitForInput",
                description="Mark a task as waiting for input and notify the coordinator actor.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                    "required": ["task_id", "prompt"],
                },
            )
            async def tool_wait_for_input(task_id: str, prompt: str) -> str:
                event = harness.task_ledger.append_event(
                    task_id,
                    "waiting_input",
                    actor=current_actor_address(),
                    content=prompt,
                )
                task = harness.task_ledger.get_task(task_id)
                owner_address = task.created_by if task.created_by in harness.actor_registry else coordinator_address()
                owner_binding = harness.task_ledger.first_binding(
                    task.id,
                    actor_address=owner_address,
                    purpose="coordinator",
                )
                if owner_binding is None:
                    owner_binding = harness.task_ledger.bind_chat(
                        task_id=task.id,
                        chat_id=task_chat_id(task.id, "coordinator"),
                        actor_address=owner_address,
                        purpose="coordinator",
                    )
                mailbox = CURRENT_MAILBOX.get(None) or harness.mail_route.bind(current_actor_address())
                await mailbox.send(
                    owner_address,
                    f"Task {task_id} is waiting for input:\n\n{prompt}",
                    chat_id=owner_binding.chat_id,
                    metadata=task_metadata(task, chat_id=owner_binding.chat_id)
                    | {"task_notification": True, "no_reply": True},
                )
                return result_payload({"event": asdict(event), "task": asdict(task)})

            @tools(
                name="ProvideTaskInput",
                description="Provide input for a waiting task and route it to the bound worker chat.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["task_id", "content"],
                },
            )
            async def tool_provide_task_input(task_id: str, content: str) -> str:
                task = harness.task_ledger.get_task(task_id)
                binding = harness.task_ledger.first_binding(task_id, actor_address=task.assigned_to, purpose="worker")
                if binding is None or binding.actor_address is None:
                    raise TaskLedgerError(f"Task {task_id!r} has no bound worker chat.")
                event = harness.task_ledger.append_event(
                    task_id,
                    "input_provided",
                    actor=current_actor_address(),
                    content=content,
                    status="running",
                )
                mailbox = CURRENT_MAILBOX.get(None) or harness.mail_route.bind(current_actor_address())
                await mailbox.send(
                    binding.actor_address,
                    content,
                    chat_id=binding.chat_id,
                    metadata=task_metadata(task, chat_id=binding.chat_id),
                )
                return result_payload({"event": asdict(event), "binding": asdict(binding)})

            @tools(
                name="CompleteTask",
                description="Mark a durable task as completed with a result.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "result": {"type": "string"},
                    },
                    "required": ["task_id", "result"],
                },
            )
            async def tool_complete_task(task_id: str, result: str) -> str:
                event = harness.task_ledger.append_event(
                    task_id,
                    "completed",
                    actor=current_actor_address(),
                    content=result,
                    result=result,
                )
                return result_payload({"event": asdict(event), "task": asdict(harness.task_ledger.get_task(task_id))})

            @tools(
                name="FailTask",
                description="Mark a durable task as failed with a reason.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["task_id", "reason"],
                },
            )
            async def tool_fail_task(task_id: str, reason: str) -> str:
                event = harness.task_ledger.append_event(
                    task_id,
                    "failed",
                    actor=current_actor_address(),
                    reason=reason,
                    status="failed",
                )
                return result_payload({"event": asdict(event), "task": asdict(harness.task_ledger.get_task(task_id))})

            @tools(
                name="AbortTask",
                description="Mark a durable task as aborted and send an abort interrupt to its worker chat when bound.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["task_id"],
                },
            )
            async def tool_abort_task(task_id: str, reason: str = "aborted") -> str:
                task = harness.task_ledger.get_task(task_id)
                event = harness.task_ledger.append_event(
                    task_id,
                    "aborted",
                    actor=current_actor_address(),
                    reason=reason,
                    status="aborted",
                )
                binding = harness.task_ledger.first_binding(task_id, actor_address=task.assigned_to, purpose="worker")
                if binding is not None and binding.actor_address is not None:
                    mailbox = CURRENT_MAILBOX.get(None) or harness.mail_route.bind(current_actor_address())
                    await mailbox.send(
                        binding.actor_address,
                        reason,
                        content_type=MessageType.INTERRUPT_ABORT,
                        chat_id=binding.chat_id,
                        metadata=task_metadata(task, chat_id=binding.chat_id),
                    )
                return result_payload({"event": asdict(event), "task": asdict(task)})

        @tools(
            name="AskSubagent",
            description=dedent("""
            Delegate a task to a named subagent and return its response.

            The runtime always creates a fresh subagent chat id derived
            from the current parent chat, so the delegated run starts in
            an isolated thread instead of reusing model-chosen history.
            """),
            parameters={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Name of the agent."},
                    "message": {"type": "string", "description": "Message to send."},
                },
                "required": ["agent_name", "message"],
            },
        )
        async def tool_ask_subagent(
            agent_name: str,
            message: str,
            chat_id: str,
            turn_id: str,
            event_sink: EventSink | None = None,
        ) -> str:
            if not ep_agent.has(agent_name):
                return f"Error: Agent '{agent_name}' not found."
            subagent_cfg = harness._get_subagent_config(agent_name)
            if task_template := subagent_cfg.get("task_template"):
                message = _safe_format(task_template, task=message, agent_name=agent_name, workspace=harness.workspace)

            child_chat_id = harness._make_subagent_chat_id(chat_id, agent_name)
            agent = harness.create_agent(agent_name, subagent_cfg)
            child_event_sink = derive_event_sink(
                event_sink,
                parent_turn_id=turn_id,
                parent_chat_id=chat_id,
                parent_agent_name=current_agent_name,
            )
            return await agent.ask(
                child_chat_id,
                message,
                ctx_metadata={"subagent": agent_name, "ref_chat_id": chat_id},
                event_sink=child_event_sink,
            )

        return tools

    def _get_subagent_config(self, agent_name: str) -> dict[str, Any]:
        default = self._subagents_cfg.get("_default", {})
        config = self._subagents_cfg.get(agent_name, {})
        return default | config

    @staticmethod
    def _make_subagent_chat_id(parent_chat_id: str, agent_name: str) -> str:
        agent_tag = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-") or "agent"
        agent_tag = agent_tag[:10]
        return f"{parent_chat_id}~{agent_tag}{uuid.uuid4().hex[:8]}"
