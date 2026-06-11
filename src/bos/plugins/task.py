"""TaskPlugin — task tracking tools and events."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from xml.sax.saxutils import escape

from bos.core._utils import _xml_attr
from bos.core.contract import (
    AgentPlugin,
    PluginServices,
    TurnInterceptor,
    ep_plugin,
)
from bos.core.registry import ToolRegistry

if TYPE_CHECKING:
    from bos.core.agent import TurnContext


@dataclass
class _Task:
    id: str
    subject: str
    description: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)


@dataclass
class _TaskList:
    """Per-chat task list with version tracking for event emission."""

    tasks: dict[str, _Task] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    version: int = 0
    emitted_version: int = -1
    _next_id: int = field(default=1, init=False)

    def bump(self) -> None:
        self.version += 1

    def mark_emitted(self) -> None:
        self.emitted_version = self.version

    def needs_emit(self) -> bool:
        return self.version != self.emitted_version

    def next_id(self) -> str:
        nid = str(self._next_id)
        self._next_id += 1
        return nid

    def to_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "subject": t.subject,
                "status": t.status,
                "blocked_by": t.blocked_by,
                "blocks": t.blocks,
            }
            for t in sorted(self.tasks.values(), key=lambda t: t.created_at)
        ]


def _apply_task_update(
    tl: _TaskList,
    *,
    taskId: str,
    status: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    addBlocks: list[str] | None = None,
    addBlockedBy: list[str] | None = None,
) -> tuple[str, bool]:
    """Apply one task update; returns (result line, whether state changed)."""
    store = tl.tasks
    if taskId not in store:
        return (f"Error: Task '{taskId}' not found. Use TaskList to see available tasks.", False)
    task = store[taskId]
    if status == "deleted":
        for other in store.values():
            other.blocked_by = [x for x in other.blocked_by if x != taskId]
            other.blocks = [x for x in other.blocks if x != taskId]
        del store[taskId]
        return (f"Task '{taskId}' deleted.", True)
    # Validate dependency ids before mutating anything.
    for blocked_id in addBlocks or []:
        if blocked_id not in store:
            return (f"Error: Blocked task '{blocked_id}' not found.", False)
    for blocker_id in addBlockedBy or []:
        if blocker_id not in store:
            return (f"Error: Blocker task '{blocker_id}' not found.", False)
    if status is not None:
        task.status = status
        if status == "completed":
            for other in store.values():
                other.blocked_by = [x for x in other.blocked_by if x != taskId]
                other.blocks = [x for x in other.blocks if x != taskId]
            task.blocked_by.clear()
            task.blocks.clear()
    if subject is not None:
        task.subject = subject
    if description is not None:
        task.description = description
    if addBlocks:
        task.blocks.extend(addBlocks)
        for blocked_id in addBlocks:
            store[blocked_id].blocked_by.append(taskId)
    if addBlockedBy:
        task.blocked_by.extend(addBlockedBy)
        for blocker_id in addBlockedBy:
            store[blocker_id].blocks.append(taskId)
    return (f"Task '{taskId}' updated. Status: {task.status}.", True)


def _render_current_tasks(task_list: _TaskList) -> str:
    lines = ["<current_tasks>"]
    for task in sorted(task_list.tasks.values(), key=lambda t: t.created_at):
        blocked_by = ",".join(task.blocked_by)
        blocks = ",".join(task.blocks)
        lines.extend(
            [
                (
                    f'<task id="{_xml_attr(task.id)}" status="{_xml_attr(task.status)}" '
                    f'blocked_by="{_xml_attr(blocked_by)}" blocks="{_xml_attr(blocks)}">'
                ),
                f"<subject>{escape(task.subject)}</subject>",
                f"<description>{escape(task.description)}</description>",
                "</task>",
            ]
        )
    lines.append("</current_tasks>")
    return "\n".join(lines)


class TaskEventInterceptor:
    """Injects task state into LLM context and emits task state events."""

    def __init__(self, task_lists: dict[str, _TaskList]) -> None:
        self._task_lists = task_lists

    async def intercept(
        self,
        stage: Literal[
            "prepare", "before_llm", "after_llm", "after_tool",
            "final_response", "max_iteration",
        ],
        context: TurnContext,
    ) -> None:
        if stage == "before_llm":
            task_list = self._task_lists.get(context.chat_id)
            if task_list is not None and task_list.tasks:
                context.set_ephemeral_message(
                    "task.current_tasks",
                    {"role": "user", "content": _render_current_tasks(task_list)},
                )
            else:
                context.clear_ephemeral_message("task.current_tasks")
            return
        if stage not in ("after_tool", "final_response"):
            return
        event_sink = getattr(context, "event_sink", None)
        if event_sink is None:
            return
        task_list = self._task_lists.get(context.chat_id)
        if task_list is not None and task_list.needs_emit():
            from bos.protocol import TurnEvent

            await event_sink.emit(
                TurnEvent(
                    event_type="task",
                    phase="update",
                    chat_id=context.chat_id,
                    turn_id=context.turn_id,
                    agent_name=context.agent_name,
                    stage=stage,
                    detail="task_state",
                    content="",
                    metadata={"tasks": task_list.to_payload()},
                )
            )
            task_list.mark_emitted()


_TASK_TOOL_USAGE = {
    "TaskCreate": """Create one or more tasks in the task list.

Use the task tools (TaskCreate, TaskUpdate, TaskList, TaskGet) to plan and track your work.

For complex or multi-part tasks: create a task list BEFORE starting work. Break the work into
concrete, verifiable steps and create the FULL list in a single TaskCreate call by passing every
step in order — do not create tasks one call at a time. After receiving new multi-part
instructions, capture them as tasks before starting implementation.

For simple single-step tasks: skip task creation and just do the work.

Mark each task in_progress when you begin it. After completing and verifying a task, mark it
completed and check TaskList to find what to work on next. Prefer working in creation order.

### When to Use

Use proactively when:
- A task requires 3 or more distinct steps or actions
- The task is non-trivial and needs careful planning
- The user provides multiple tasks (numbered or comma-separated)""",

    "TaskUpdate": """Update status, metadata, or dependencies for one or more tasks.

Batch related changes into a single call — e.g. mark the finished task completed and the next
one in_progress together, or wire several dependencies at once.

### Status Workflow

pending -> in_progress -> completed

IMPORTANT: Only mark completed when implementation and relevant verification are both done.
If tests fail, errors remain, verification was skipped, or implementation is partial, keep
in_progress and record the blocker or next action.""",

    "TaskList": """List all tasks with status and blockers. Use to:
- Check overall progress
- Find the next available task (pending, not blocked)
- See which tasks are blocked and why""",

    "TaskGet": """Fetch full details of a task including description and dependency state.
Use before starting work on a task to verify its blockedBy list is empty.""",
}

_TASK_PROMPT_SECTION = """<task_workflow>
Use task tools to track complex, multi-step work in the current conversation.

- Use TaskCreate when the request has multiple parts, needs careful sequencing, or benefits from progress tracking.
- Create the full task list in a single TaskCreate call; batch related status changes into one TaskUpdate call
  (e.g. mark the finished task completed and the next one in_progress together).
- The task list is the single source of truth for execution progress. When a plan exists, seed tasks from the
  approved plan's breakdown instead of re-deriving steps.
- Skip task tools for simple one-step requests where tracking would add noise.
- Mark a task in_progress with TaskUpdate when you begin it.
- Use TaskGet before starting a task when you need its full description or dependency state.
- Use TaskList after completing or unblocking work to choose the next pending, unblocked task.
- Only mark a task completed after the described work and relevant verification are both done.
- Keep blocked, partial, unverified, or failing work in_progress and record the next action in the task.
</task_workflow>"""


@ep_plugin(name="TaskPlugin")
class TaskHarnessPlugin:
    @property
    def name(self) -> str:
        return "TaskPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {}

    async def setup(self, services: PluginServices) -> None:
        pass

    def validate_config(self, config: Mapping[str, Any]) -> None:
        pass

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        return TaskAgentPlugin()

    async def teardown(self) -> None:
        pass


class TaskAgentPlugin:
    def __init__(self) -> None:
        self._task_lists: dict[str, _TaskList] = {}

    @property
    def name(self) -> str:
        return "TaskPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        task_lists = self._task_lists

        @registry(
            name="TaskCreate",
            description=(
                "Create one or more tasks to track progress on complex, multi-step work."
                " Pass the full task list in a single call."
            ),
            usage=_TASK_TOOL_USAGE["TaskCreate"],
            parameters={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "description": "Tasks to create, in intended execution order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subject": {
                                    "type": "string",
                                    "description": (
                                        "Brief, actionable title in imperative form (e.g., 'Fix auth bug')."
                                    ),
                                },
                                "description": {
                                    "type": "string",
                                    "description": "What needs to be done. 1-2 sentences.",
                                },
                            },
                            "required": ["subject", "description"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        )
        async def task_create(tasks: list[dict[str, Any]], chat_id: str = "") -> str:
            if not tasks:
                return "Error: 'tasks' must contain at least one task."
            for index, item in enumerate(tasks):
                if not isinstance(item, dict) or not str(item.get("subject", "")).strip():
                    return f"Error: task at index {index} requires a non-empty 'subject'."
            tl = task_lists.setdefault(chat_id, _TaskList())
            lines = []
            for item in tasks:
                task_id = tl.next_id()
                task = _Task(
                    id=task_id,
                    subject=str(item["subject"]).strip(),
                    description=str(item.get("description", "")).strip(),
                )
                tl.tasks[task_id] = task
                lines.append(f"[{task_id}] {task.subject}")
            tl.bump()
            noun = "Task" if len(lines) == 1 else f"{len(lines)} tasks"
            return f"{noun} created (status: pending):\n" + "\n".join(lines)

        @registry(
            name="TaskUpdate",
            description=(
                "Update status or metadata for one or more tasks in a single call."
                " Status flows: pending -> in_progress -> completed."
                " Only mark completed when FULLY done."
            ),
            usage=_TASK_TOOL_USAGE["TaskUpdate"],
            parameters={
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "minItems": 1,
                        "description": "Updates to apply, each targeting one task by id.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "taskId": {"type": "string", "description": "Task ID to update."},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "deleted"],
                                    "description": "New status.",
                                },
                                "subject": {"type": "string", "description": "New title."},
                                "description": {"type": "string", "description": "New description."},
                                "addBlocks": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Task IDs that this task blocks.",
                                },
                                "addBlockedBy": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Task IDs that block this task.",
                                },
                            },
                            "required": ["taskId"],
                        },
                    },
                },
                "required": ["updates"],
            },
        )
        async def task_update(updates: list[dict[str, Any]], chat_id: str = "") -> str:
            if not updates:
                return "Error: 'updates' must contain at least one update."
            tl = task_lists.get(chat_id)
            if tl is None:
                return "Error: no tasks exist for this conversation. Use TaskCreate first."
            lines = []
            changed = False
            for item in updates:
                if not isinstance(item, dict):
                    lines.append("Error: each update must be an object with a 'taskId'.")
                    continue
                line, applied = _apply_task_update(
                    tl,
                    taskId=str(item.get("taskId", "")),
                    status=item.get("status"),
                    subject=item.get("subject"),
                    description=item.get("description"),
                    addBlocks=item.get("addBlocks"),
                    addBlockedBy=item.get("addBlockedBy"),
                )
                lines.append(line)
                changed = changed or applied
            if changed:
                tl.bump()
            return "\n".join(lines)

        @registry(
            name="TaskList",
            description="List all tasks with status and blockers. Shows which tasks are available to work on.",
            usage=_TASK_TOOL_USAGE["TaskList"],
            parameters={"type": "object", "properties": {}, "required": []},
        )
        async def task_list(chat_id: str = "") -> str:
            tl = task_lists.get(chat_id)
            if tl is None or not tl.tasks:
                return "(No tasks created yet.)"
            lines = []
            for tid in sorted(tl.tasks, key=lambda k: tl.tasks[k].created_at):
                t = tl.tasks[tid]
                blocked = f" (blocked by: {', '.join(t.blocked_by)})" if t.blocked_by else ""
                blocks = f" [blocks: {', '.join(t.blocks)}]" if t.blocks else ""
                lines.append(f"[{t.id}] {t.status:<12} {t.subject}{blocked}{blocks}")
            return "Tasks:\n" + "\n".join(lines)

        @registry(
            name="TaskGet",
            description="Fetch full details of a specific task, including description and dependency state.",
            usage=_TASK_TOOL_USAGE["TaskGet"],
            parameters={
                "type": "object",
                "properties": {"taskId": {"type": "string", "description": "Task ID to fetch."}},
                "required": ["taskId"],
            },
        )
        async def task_get(taskId: str, chat_id: str = "") -> str:
            tl = task_lists.get(chat_id)
            if tl is None or taskId not in tl.tasks:
                return f"Error: Task '{taskId}' not found. Use TaskList to see available tasks."
            t = tl.tasks[taskId]
            parts = [
                f"Task: {t.subject}",
                f"ID: {t.id}",
                f"Status: {t.status}",
                f"Description: {t.description}",
            ]
            if t.blocked_by:
                parts.append(f"Blocked by: {', '.join(t.blocked_by)}")
            if t.blocks:
                parts.append(f"Blocks: {', '.join(t.blocks)}")
            return "\n".join(parts)

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:
        return _TASK_PROMPT_SECTION

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        return [TaskEventInterceptor(self._task_lists)]
