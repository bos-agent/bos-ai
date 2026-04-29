from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bos.core.actor import ActorResponseRoute

from .tasks import TaskLedger, task_chat_id, task_metadata


@dataclass(frozen=True)
class PeerTaskRoute(ActorResponseRoute):
    chat_id: str | None
    metadata: dict[str, Any]
    content: str


class PeerTaskRuntime:
    """Task-aware routing hook for the optional multi-actor team runtime."""

    def __init__(self, ledger: TaskLedger, actor_address: str) -> None:
        self._ledger = ledger
        self._actor_address = actor_address

    def validate_envelope(self, *, chat_id: str, metadata: dict[str, Any]) -> None:
        self._ledger.validate_task_envelope(
            chat_id=chat_id,
            metadata=metadata,
            actor_address=self._actor_address,
        )

    def route_response(
        self,
        *,
        source_chat_id: str,
        reply_chat_id: str | None,
        reply_recipient: str,
        response: str,
    ) -> PeerTaskRoute:
        binding = self._ledger.get_binding(source_chat_id)
        if binding is None or binding.actor_address != self._actor_address:
            return PeerTaskRoute(reply_chat_id, {}, response)

        task = self._ledger.get_task(binding.task_id)
        self._ledger.append_event(
            task.id,
            "progress",
            actor=self._actor_address,
            content=response,
            metadata={"source_chat_id": source_chat_id, "reply_recipient": reply_recipient},
        )
        if (
            binding.purpose != "worker"
            or not reply_recipient.startswith("agent@")
            or reply_recipient == self._actor_address
        ):
            return PeerTaskRoute(reply_chat_id, task_metadata(task, chat_id=reply_chat_id or source_chat_id), response)

        original_chat_id = task.metadata.get("source_chat_id")
        original_sender = task.metadata.get("source_sender")
        if (
            isinstance(original_chat_id, str)
            and original_chat_id
            and isinstance(original_sender, str)
            and original_sender
        ):
            source_tasks = self._tasks_for_source_chat(task, original_chat_id)
            pending_tasks = [
                source_task
                for source_task in source_tasks
                if source_task.id != task.id and source_task.status in {"queued", "running", "waiting_input"}
            ]
            metadata = task_metadata(task, chat_id=original_chat_id) | {
                "task_notification": True,
                "source_chat_id": original_chat_id,
                "reply_to": original_sender,
            }
            if pending_tasks:
                metadata["no_reply"] = True
            return PeerTaskRoute(
                original_chat_id,
                metadata,
                self._format_task_completion_notification(task, response, source_tasks, pending_tasks),
            )

        notify_binding = self._ledger.first_binding(
            task.id,
            actor_address=reply_recipient,
            purpose="coordinator",
        )
        if notify_binding is None:
            notify_binding = self._ledger.bind_chat(
                task_id=task.id,
                chat_id=task_chat_id(task.id, "coordinator"),
                actor_address=reply_recipient,
                purpose="coordinator",
            )
        metadata = task_metadata(task, chat_id=notify_binding.chat_id) | {
            "task_notification": True,
            "no_reply": True,
        }
        return PeerTaskRoute(notify_binding.chat_id, metadata, response)

    def _tasks_for_source_chat(self, task: Any, source_chat_id: str) -> list[Any]:
        return [
            candidate
            for candidate in self._ledger.list_tasks()
            if candidate.created_by == task.created_by
            and candidate.metadata.get("source_chat_id") == source_chat_id
        ]

    @staticmethod
    def _format_task_completion_notification(
        task: Any,
        response: str,
        source_tasks: list[Any],
        pending_tasks: list[Any],
    ) -> str:
        task_result = task.result or response
        lines = [
            "Peer task update:",
            f"- task_id: {task.id}",
            f"- assigned_to: {task.assigned_to}",
            f"- status: {task.status}",
            f"- goal: {task.goal}",
        ]
        if task_result:
            lines.append(f"- result: {task_result}")

        if source_tasks:
            lines.append("")
            lines.append("Related tasks for this user request:")
            for source_task in source_tasks:
                summary = source_task.result
                if summary is None and source_task.id == task.id:
                    summary = task_result
                result_text = f", result: {summary}" if summary else ""
                lines.append(
                    f"- {source_task.id}: {source_task.status}, assigned_to: {source_task.assigned_to}, "
                    f"goal: {source_task.goal}{result_text}"
                )

        lines.append("")
        if pending_tasks:
            pending_ids = ", ".join(source_task.id for source_task in pending_tasks)
            lines.append(f"Do not answer the user yet. Pending related task(s): {pending_ids}.")
            lines.append("Record this update and wait for the remaining task result(s).")
        else:
            lines.append("All related tasks for the original user request are complete.")
            lines.append("Use the original user request plus these task results to reply to the user now.")
        return "\n".join(lines)
