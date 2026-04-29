from __future__ import annotations

import json
from dataclasses import asdict
from textwrap import dedent
from typing import Any

from bos.core.harness import CURRENT_MAILBOX
from bos.protocol import MessageType

from .tasks import TaskLedgerError, task_chat_id, task_metadata

PEER_TASK_COMMON_TOOLS = (
    "ListActors",
    "CreateTask",
    "UpdateTask",
    "StartTask",
    "WaitForInput",
    "CompleteTask",
    "FailTask",
)
PEER_TASK_COORDINATOR_TOOLS = PEER_TASK_COMMON_TOOLS + ("ProvideTaskInput", "AbortTask")
PEER_TASK_WORKER_TOOLS = PEER_TASK_COMMON_TOOLS


def peer_task_tool_names_for_role(role: str) -> tuple[str, ...]:
    if role == "coordinator":
        return PEER_TASK_COORDINATOR_TOOLS
    return PEER_TASK_WORKER_TOOLS


def register_peer_task_tools(tools: Any, harness: Any, current_agent_name: str) -> None:
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
        sender: str | None = None,
    ) -> str:
        assigned_address = normalize_actor_address(assigned_to)
        created_by = current_actor_address()
        parent = parent_id or None
        task_context: dict[str, Any] = {}
        if chat_id:
            task_context["source_chat_id"] = chat_id
        if sender:
            task_context["source_sender"] = sender
        task_context["source_actor"] = created_by
        task = harness.task_ledger.create_task(
            goal=goal,
            created_by=created_by,
            assigned_to=assigned_address,
            parent_id=parent,
            context=context,
            metadata=task_context,
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
