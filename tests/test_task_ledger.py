import pytest

from bos.team.tasks import TaskLedger, TaskLedgerError, task_chat_id


def test_create_root_task_persists_and_reloads(tmp_path):
    path = tmp_path / "tasks.jsonl"
    ledger = TaskLedger(path)

    task = ledger.create_task(goal="Research actor topology", created_by="agent@main", assigned_to="agent@researcher")

    assert task.id
    assert task.root_id == task.id
    assert task.parent_id is None
    assert task.status == "queued"

    reloaded = TaskLedger(path)
    loaded = reloaded.get_task(task.id)

    assert loaded.goal == "Research actor topology"
    assert loaded.created_by == "agent@main"
    assert loaded.assigned_to == "agent@researcher"
    assert loaded.status == "queued"


def test_create_child_task_inherits_root_id():
    ledger = TaskLedger()
    root = ledger.create_task(goal="Root", created_by="agent@main", assigned_to="agent@researcher")

    child = ledger.create_task(
        goal="Child",
        created_by="agent@researcher",
        assigned_to="agent@reviewer",
        parent_id=root.id,
    )

    assert child.parent_id == root.id
    assert child.root_id == root.id


def test_task_events_update_status_and_result():
    ledger = TaskLedger()
    task = ledger.create_task(goal="Do work", created_by="agent@main", assigned_to="agent@researcher")

    ledger.append_event(task.id, "started")
    assert ledger.get_task(task.id).status == "running"

    ledger.append_event(task.id, "waiting_input", content="Need a branch name")
    assert ledger.get_task(task.id).status == "waiting_input"

    ledger.append_event(task.id, "input_provided", content="Use main")
    assert ledger.get_task(task.id).status == "running"

    ledger.append_event(task.id, "completed", result="done")
    completed = ledger.get_task(task.id)
    assert completed.status == "completed"
    assert completed.result == "done"


def test_multiple_chat_bindings_for_one_task_are_allowed():
    ledger = TaskLedger()
    task = ledger.create_task(goal="Do work", created_by="agent@main", assigned_to="agent@researcher")

    first = ledger.bind_chat(task_id=task.id, chat_id=task_chat_id(task.id, "worker"), actor_address="agent@researcher")
    second = ledger.bind_chat(task_id=task.id, chat_id=task_chat_id(task.id, "retry"), actor_address="agent@researcher")

    assert first.task_id == task.id
    assert second.task_id == task.id
    assert first.chat_id != second.chat_id
    assert len(ledger.list_bindings(task.id)) == 2


def test_one_chat_cannot_bind_to_multiple_tasks():
    ledger = TaskLedger()
    first = ledger.create_task(goal="First", created_by="agent@main", assigned_to="agent@researcher")
    second = ledger.create_task(goal="Second", created_by="agent@main", assigned_to="agent@researcher")
    chat_id = "task:shared:worker:abc"

    ledger.bind_chat(task_id=first.id, chat_id=chat_id)

    with pytest.raises(TaskLedgerError, match="already bound"):
        ledger.bind_chat(task_id=second.id, chat_id=chat_id)


def test_task_envelope_validation_rejects_mismatched_task_id():
    ledger = TaskLedger()
    task = ledger.create_task(goal="Do work", created_by="agent@main", assigned_to="agent@researcher")
    chat_id = task_chat_id(task.id)
    ledger.bind_chat(task_id=task.id, chat_id=chat_id)

    ledger.validate_envelope(chat_id=chat_id, task_id=task.id)

    with pytest.raises(TaskLedgerError, match="does not match"):
        ledger.validate_envelope(chat_id=chat_id, task_id="task_other")


def test_unbound_task_namespaced_chat_is_rejected():
    ledger = TaskLedger()

    with pytest.raises(TaskLedgerError, match="not bound"):
        ledger.validate_envelope(chat_id="task:missing:worker:abc", task_id="task_missing")


def test_running_worker_tasks_can_be_marked_interrupted_on_restart():
    ledger = TaskLedger()
    coordinator = ledger.create_task(goal="Coordinate", created_by="agent@main", assigned_to="agent@main")
    worker = ledger.create_task(goal="Research", created_by="agent@main", assigned_to="agent@researcher")
    queued = ledger.create_task(goal="Review", created_by="agent@main", assigned_to="agent@reviewer")
    ledger.append_event(coordinator.id, "started")
    ledger.append_event(worker.id, "started")

    count = ledger.mark_active_tasks_interrupted(exclude_assignees={"agent@main"}, reason="restart")

    assert count == 2
    assert ledger.get_task(coordinator.id).status == "running"
    assert ledger.get_task(worker.id).status == "interrupted"
    assert ledger.get_task(queued.id).status == "interrupted"
    assert ledger.list_events(worker.id)[-1].reason == "restart"
