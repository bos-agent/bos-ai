"""Tests for TaskCreate, TaskUpdate, TaskList, TaskGet tools via TaskPlugin."""

import asyncio

import pytest
from conftest import create_test_agent

from bos.plugins.task import TaskAgentPlugin


def _create_agent(**kwargs):
    kwargs.setdefault("system_prompt", "test")
    return create_test_agent(plugins=[TaskAgentPlugin()], **kwargs)


async def _create_one(agent, subject: str, description: str = "d", chat_id: str = "") -> str:
    """Create a single task and return its id."""
    result = await agent._invoke_tool(
        "TaskCreate", tasks=[{"subject": subject, "description": description}], chat_id=chat_id
    )
    return _extract_task_ids(result)[0]


async def _update_one(agent, taskId: str, **fields) -> str:
    chat_id = fields.pop("chat_id", "")
    return await agent._invoke_tool("TaskUpdate", updates=[{"taskId": taskId, **fields}], chat_id=chat_id)


class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_creates_task_with_id_and_subject(self):
        agent = _create_agent()
        result = await agent._invoke_tool(
            "TaskCreate", tasks=[{"subject": "Fix auth bug", "description": "Fix login redirect loop."}]
        )
        assert "Fix auth bug" in result
        assert "status: pending" in result
        assert "Task created" in result

    @pytest.mark.asyncio
    async def test_creates_multiple_tasks_in_one_call(self):
        agent = _create_agent()
        result = await agent._invoke_tool(
            "TaskCreate",
            tasks=[
                {"subject": "First", "description": "d1"},
                {"subject": "Second", "description": "d2"},
                {"subject": "Third", "description": "d3"},
            ],
        )
        assert "3 tasks created" in result
        ids = _extract_task_ids(result)
        assert len(ids) == 3
        listing = await agent._invoke_tool("TaskList")
        subjects = [line for line in listing.split("\n") if line.startswith("[")]
        assert [s.split("] ", 1)[0] + "]" for s in subjects] == [f"[{i}]" for i in ids]
        assert "First" in subjects[0]
        assert "Second" in subjects[1]
        assert "Third" in subjects[2]

    @pytest.mark.asyncio
    async def test_rejects_empty_list_and_blank_subject(self):
        agent = _create_agent()
        assert "Error" in await agent._invoke_tool("TaskCreate", tasks=[])
        result = await agent._invoke_tool(
            "TaskCreate",
            tasks=[{"subject": "Valid", "description": "d"}, {"subject": "  ", "description": "d"}],
        )
        assert "Error" in result
        assert "index 1" in result
        # Nothing partially created.
        assert "No tasks created" in await agent._invoke_tool("TaskList")

    @pytest.mark.asyncio
    async def test_tasks_are_scoped_by_chat_id(self):
        agent = _create_agent()
        await _create_one(agent, "Task A", chat_id="chat-1")
        await _create_one(agent, "Task B", chat_id="chat-2")
        list1 = await agent._invoke_tool("TaskList", chat_id="chat-1")
        list2 = await agent._invoke_tool("TaskList", chat_id="chat-2")
        assert "Task A" in list1
        assert "Task B" not in list1
        assert "Task B" in list2
        assert "Task A" not in list2


class TestTaskUpdate:
    @pytest.mark.asyncio
    async def test_set_status_in_progress(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "T")
        await _update_one(agent, task_id, status="in_progress")
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "in_progress" in detail

    @pytest.mark.asyncio
    async def test_set_status_completed(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "T")
        await _update_one(agent, task_id, status="completed")
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "completed" in detail

    @pytest.mark.asyncio
    async def test_updates_multiple_tasks_in_one_call(self):
        agent = _create_agent()
        result = await agent._invoke_tool(
            "TaskCreate",
            tasks=[{"subject": "Done", "description": "d"}, {"subject": "Next", "description": "d"}],
        )
        id1, id2 = _extract_task_ids(result)
        update = await agent._invoke_tool(
            "TaskUpdate",
            updates=[{"taskId": id1, "status": "completed"}, {"taskId": id2, "status": "in_progress"}],
        )
        assert f"Task '{id1}' updated. Status: completed." in update
        assert f"Task '{id2}' updated. Status: in_progress." in update

    @pytest.mark.asyncio
    async def test_bulk_update_continues_past_per_item_errors(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "T")
        result = await agent._invoke_tool(
            "TaskUpdate",
            updates=[
                {"taskId": "nonexistent", "status": "completed"},
                {"taskId": task_id, "status": "in_progress"},
            ],
        )
        assert "Error: Task 'nonexistent' not found" in result
        assert f"Task '{task_id}' updated. Status: in_progress." in result
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "in_progress" in detail

    @pytest.mark.asyncio
    async def test_delete_removes_task(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "T")
        del_result = await _update_one(agent, task_id, status="deleted")
        assert "deleted" in del_result
        get_result = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "not found" in get_result

    @pytest.mark.asyncio
    async def test_delete_cleans_up_blocked_by_references(self):
        agent = _create_agent()
        id1 = await _create_one(agent, "Backend")
        id2 = await _create_one(agent, "Frontend")
        await _update_one(agent, id2, addBlockedBy=[id1])
        # delete the blocker
        await _update_one(agent, id1, status="deleted")
        detail = await agent._invoke_tool("TaskGet", taskId=id2)
        assert "Blocked by:" not in detail

    @pytest.mark.asyncio
    async def test_delete_cleans_up_blocks_references(self):
        agent = _create_agent()
        id1 = await _create_one(agent, "Blocker")
        id2 = await _create_one(agent, "Blocked")
        await _update_one(agent, id1, addBlocks=[id2])
        # delete the blocked task
        await _update_one(agent, id2, status="deleted")
        detail = await agent._invoke_tool("TaskGet", taskId=id1)
        assert "Blocks:" not in detail

    @pytest.mark.asyncio
    async def test_completed_blocker_clears_blocked_by(self):
        agent = _create_agent()
        id1 = await _create_one(agent, "Design")
        id2 = await _create_one(agent, "Implement")
        await _update_one(agent, id2, addBlockedBy=[id1])
        # complete the blocker — blocked_by should clear
        await _update_one(agent, id1, status="completed")
        detail = await agent._invoke_tool("TaskGet", taskId=id2)
        assert "Blocked by:" not in detail

    @pytest.mark.asyncio
    async def test_invalid_task_id_returns_error(self):
        agent = _create_agent()
        await _create_one(agent, "T")
        result = await _update_one(agent, "nonexistent", status="in_progress")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_update_without_any_tasks_returns_error(self):
        agent = _create_agent()
        result = await _update_one(agent, "1", status="in_progress")
        assert "no tasks exist" in result

    @pytest.mark.asyncio
    async def test_invalid_blocked_task_leaves_state_untouched(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "T")
        result = await _update_one(agent, task_id, status="in_progress", addBlocks=["nonexistent"])
        assert "not found" in result
        # Dependency validation happens before mutation: status unchanged too.
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "pending" in detail

    @pytest.mark.asyncio
    async def test_update_subject_and_description(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "Old", "Old desc")
        await _update_one(agent, task_id, subject="New", description="New desc")
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "Old" not in detail
        assert "New" in detail
        assert "New desc" in detail


class TestTaskList:
    @pytest.mark.asyncio
    async def test_empty_store_returns_placeholder(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskList")
        assert "No tasks created" in result

    @pytest.mark.asyncio
    async def test_lists_tasks_sorted_by_creation_order(self):
        agent = _create_agent()
        id1 = await _create_one(agent, "First")
        await asyncio.sleep(0.01)
        id2 = await _create_one(agent, "Second")
        await asyncio.sleep(0.01)
        id3 = await _create_one(agent, "Third")
        result = await agent._invoke_tool("TaskList")
        lines = result.split("\n")
        subjects = [line for line in lines if line.startswith("[")]
        assert subjects[0].startswith(f"[{id1}]")
        assert subjects[1].startswith(f"[{id2}]")
        assert subjects[2].startswith(f"[{id3}]")

    @pytest.mark.asyncio
    async def test_shows_blocked_by_and_blocks(self):
        agent = _create_agent()
        id1 = await _create_one(agent, "Design")
        id2 = await _create_one(agent, "Implement")
        await _update_one(agent, id2, addBlockedBy=[id1])
        result = await agent._invoke_tool("TaskList")
        assert "blocked by:" in result
        assert "blocks:" in result


class TestTaskGet:
    @pytest.mark.asyncio
    async def test_returns_full_details(self):
        agent = _create_agent()
        task_id = await _create_one(agent, "My Task", "Do the thing.")
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "My Task" in detail
        assert "Do the thing." in detail
        assert "pending" in detail

    @pytest.mark.asyncio
    async def test_invalid_task_id_returns_error(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskGet", taskId="nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_shows_dependencies(self):
        agent = _create_agent()
        id1 = await _create_one(agent, "Setup")
        id2 = await _create_one(agent, "Build")
        await _update_one(agent, id2, addBlockedBy=[id1])
        detail = await agent._invoke_tool("TaskGet", taskId=id2)
        assert f"Blocked by: {id1}" in detail


def _extract_task_ids(result: str) -> list[str]:
    """Extract task ids from a TaskCreate result with '[id] Subject' lines."""
    ids = []
    for line in result.split("\n"):
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line[1 : line.index("]")])
    return ids
