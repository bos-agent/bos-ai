from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

TaskStatus = Literal[
    "queued",
    "running",
    "waiting_input",
    "completed",
    "failed",
    "aborted",
    "interrupted",
]

ActorStatus = Literal["starting", "idle", "busy", "waiting_input", "unavailable"]
ActorRole = Literal["coordinator", "worker"]
ChatBindingPurpose = Literal["coordinator", "worker", "child", "retry", "recovery", "inspection"]


class TaskLedgerError(ValueError):
    """Raised when task ledger invariants would be violated."""


@dataclass(frozen=True)
class ActorRef:
    name: str
    address: str
    role: ActorRole = "worker"
    description: str | None = None
    capabilities: list[str] = field(default_factory=list)
    status: ActorStatus = "idle"
    agent: str | None = None


@dataclass
class TaskRecord:
    id: str
    parent_id: str | None
    root_id: str
    created_by: str
    assigned_to: str
    status: TaskStatus
    goal: str
    context: Any = ""
    result: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    recovery_policy: str = "coordinator_decides"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskEvent:
    id: str
    task_id: str
    type: str
    actor: str | None = None
    content: Any = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class ChatTaskBinding:
    chat_id: str
    task_id: str
    actor_address: str | None = None
    purpose: ChatBindingPurpose = "worker"
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)


def task_chat_id(task_id: str, purpose: str = "worker") -> str:
    safe_purpose = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in purpose).strip("-") or "worker"
    return f"task:{task_id}:{safe_purpose}:{uuid.uuid4().hex[:8]}"


def task_metadata(task: TaskRecord, *, chat_id: str | None = None) -> dict[str, Any]:
    metadata = {
        "task_id": task.id,
        "root_task_id": task.root_id,
        "parent_task_id": task.parent_id,
        "assigned_to": task.assigned_to,
        "created_by": task.created_by,
    }
    if chat_id:
        metadata["task_chat_id"] = chat_id
    return metadata


class TaskLedger:
    """In-memory task ledger with optional JSONL persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser().resolve() if path is not None else None
        self._tasks: dict[str, TaskRecord] = {}
        self._events: list[TaskEvent] = []
        self._bindings_by_chat: dict[str, ChatTaskBinding] = {}
        self._loaded = False

    def create_task(
        self,
        *,
        goal: str,
        created_by: str,
        assigned_to: str,
        parent_id: str | None = None,
        context: Any = "",
        task_id: str | None = None,
        recovery_policy: str = "coordinator_decides",
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        self._ensure_loaded()
        goal = self._require_nonempty("goal", goal)
        created_by = self._require_nonempty("created_by", created_by)
        assigned_to = self._require_nonempty("assigned_to", assigned_to)
        task_id = self._require_nonempty("task_id", task_id or f"task_{uuid.uuid4().hex}")
        if task_id in self._tasks:
            raise TaskLedgerError(f"Task {task_id!r} already exists.")
        if parent_id is not None and parent_id not in self._tasks:
            raise TaskLedgerError(f"Parent task {parent_id!r} does not exist.")

        now = datetime.now()
        root_id = self._tasks[parent_id].root_id if parent_id else task_id
        task = TaskRecord(
            id=task_id,
            parent_id=parent_id,
            root_id=root_id,
            created_by=created_by,
            assigned_to=assigned_to,
            status="queued",
            goal=goal,
            context=context,
            created_at=now,
            updated_at=now,
            recovery_policy=recovery_policy,
            metadata=dict(metadata or {}),
        )
        self._tasks[task.id] = task
        self._write_entry("task", task)
        self._append_event(
            TaskEvent(
                id=self._new_event_id(),
                task_id=task.id,
                type="created",
                actor=created_by,
                metadata={"assigned_to": assigned_to, "parent_id": parent_id, "root_id": root_id},
                created_at=now,
            )
        )
        return task

    def bind_chat(
        self,
        *,
        task_id: str,
        chat_id: str,
        actor_address: str | None = None,
        purpose: ChatBindingPurpose = "worker",
        metadata: dict[str, Any] | None = None,
    ) -> ChatTaskBinding:
        self._ensure_loaded()
        task_id = self._require_nonempty("task_id", task_id)
        chat_id = self._require_nonempty("chat_id", chat_id)
        if task_id not in self._tasks:
            raise TaskLedgerError(f"Task {task_id!r} does not exist.")
        existing = self._bindings_by_chat.get(chat_id)
        if existing is not None:
            if existing.task_id != task_id:
                raise TaskLedgerError(
                    f"Chat {chat_id!r} is already bound to task {existing.task_id!r}, not {task_id!r}."
                )
            return existing
        binding = ChatTaskBinding(
            chat_id=chat_id,
            task_id=task_id,
            actor_address=actor_address,
            purpose=purpose,
            metadata=dict(metadata or {}),
        )
        self._bindings_by_chat[chat_id] = binding
        self._write_entry("binding", binding)
        return binding

    def append_event(
        self,
        task_id: str,
        event_type: str,
        *,
        actor: str | None = None,
        content: Any = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: TaskStatus | None = None,
        result: str | None = None,
    ) -> TaskEvent:
        self._ensure_loaded()
        task = self.get_task(task_id)
        new_status = status or self._status_for_event(event_type)
        if new_status is not None:
            task.status = new_status
        if result is not None:
            task.result = result
        task.updated_at = datetime.now()
        event = TaskEvent(
            id=self._new_event_id(),
            task_id=task_id,
            type=event_type,
            actor=actor,
            content=content,
            reason=reason,
            metadata=dict(metadata or {}),
        )
        self._append_event(event)
        self._write_entry("task_update", task)
        return event

    def validate_envelope(self, *, chat_id: str, task_id: str | None) -> None:
        self._ensure_loaded()
        binding = self._bindings_by_chat.get(chat_id)
        if binding is None:
            if chat_id.startswith("task:"):
                raise TaskLedgerError(f"Task-owned chat {chat_id!r} is not bound in the task ledger.")
            return
        if task_id != binding.task_id:
            raise TaskLedgerError(
                f"Envelope task_id {task_id!r} does not match chat {chat_id!r} binding {binding.task_id!r}."
            )

    def validate_task_envelope(
        self,
        *,
        chat_id: str | None,
        metadata: dict[str, Any] | None,
        actor_address: str | None = None,
    ) -> None:
        if not chat_id:
            return
        metadata = metadata or {}
        task_id = metadata.get("task_id")
        self.validate_envelope(chat_id=chat_id, task_id=task_id if isinstance(task_id, str) else None)
        binding = self.get_binding(chat_id)
        if binding is not None and actor_address is not None and binding.actor_address != actor_address:
            raise TaskLedgerError(
                f"Task chat {chat_id!r} is bound to actor {binding.actor_address!r}, not {actor_address!r}."
            )

    def get_task(self, task_id: str) -> TaskRecord:
        self._ensure_loaded()
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskLedgerError(f"Task {task_id!r} does not exist.") from exc

    def list_tasks(self) -> list[TaskRecord]:
        self._ensure_loaded()
        return list(self._tasks.values())

    def list_events(self, task_id: str | None = None) -> list[TaskEvent]:
        self._ensure_loaded()
        if task_id is None:
            return list(self._events)
        return [event for event in self._events if event.task_id == task_id]

    def list_bindings(self, task_id: str | None = None) -> list[ChatTaskBinding]:
        self._ensure_loaded()
        bindings = list(self._bindings_by_chat.values())
        if task_id is None:
            return bindings
        return [binding for binding in bindings if binding.task_id == task_id]

    def get_binding(self, chat_id: str) -> ChatTaskBinding | None:
        self._ensure_loaded()
        return self._bindings_by_chat.get(chat_id)

    def first_binding(
        self,
        task_id: str,
        *,
        actor_address: str | None = None,
        purpose: ChatBindingPurpose | None = None,
    ) -> ChatTaskBinding | None:
        for binding in self.list_bindings(task_id):
            if actor_address is not None and binding.actor_address != actor_address:
                continue
            if purpose is not None and binding.purpose != purpose:
                continue
            return binding
        return None

    def mark_active_tasks_interrupted(
        self,
        *,
        exclude_assignees: set[str] | None = None,
        reason: str = "restart",
    ) -> int:
        self._ensure_loaded()
        count = 0
        exclude_assignees = exclude_assignees or set()
        for task in list(self._tasks.values()):
            if task.status not in {"queued", "running"} or task.assigned_to in exclude_assignees:
                continue
            self.append_event(task.id, "interrupted", reason=reason, status="interrupted")
            count += 1
        return count

    def mark_running_tasks_interrupted(
        self,
        *,
        exclude_assignees: set[str] | None = None,
        reason: str = "restart",
    ) -> int:
        return self.mark_active_tasks_interrupted(exclude_assignees=exclude_assignees, reason=reason)

    def _append_event(self, event: TaskEvent) -> None:
        self._events.append(event)
        self._write_entry("event", event)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            entry_type = raw.get("entry_type")
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                continue
            if entry_type == "task":
                task = _task_from_payload(payload)
                self._tasks[task.id] = task
            elif entry_type == "task_update":
                task = _task_from_payload(payload)
                self._tasks[task.id] = task
            elif entry_type == "event":
                self._events.append(_event_from_payload(payload))
            elif entry_type == "binding":
                binding = _binding_from_payload(payload)
                self._bindings_by_chat[binding.chat_id] = binding

    def _write_entry(self, entry_type: str, value: Any) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(value)
        for key in ("created_at", "updated_at"):
            if isinstance(payload.get(key), datetime):
                payload[key] = payload[key].isoformat()
        line = json.dumps({"entry_type": entry_type, "payload": payload}, default=str, sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)

    @staticmethod
    def _new_event_id() -> str:
        return f"event_{uuid.uuid4().hex}"

    @staticmethod
    def _require_nonempty(name: str, value: str | None) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TaskLedgerError(f"{name} must be non-empty.")
        return value.strip()

    @staticmethod
    def _status_for_event(event_type: str) -> TaskStatus | None:
        return {
            "assigned": "queued",
            "started": "running",
            "waiting_input": "waiting_input",
            "input_provided": "running",
            "completed": "completed",
            "failed": "failed",
            "aborted": "aborted",
            "interrupted": "interrupted",
        }.get(event_type)


def _task_from_payload(payload: dict[str, Any]) -> TaskRecord:
    data = dict(payload)
    data["created_at"] = _parse_datetime(data.get("created_at"))
    data["updated_at"] = _parse_datetime(data.get("updated_at"))
    return TaskRecord(**data)


def _event_from_payload(payload: dict[str, Any]) -> TaskEvent:
    data = dict(payload)
    data["created_at"] = _parse_datetime(data.get("created_at"))
    return TaskEvent(**data)


def _binding_from_payload(payload: dict[str, Any]) -> ChatTaskBinding:
    data = dict(payload)
    data["created_at"] = _parse_datetime(data.get("created_at"))
    return ChatTaskBinding(**data)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return datetime.now()
