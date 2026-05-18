"""Tests for TaskCreate, TaskUpdate, TaskList, TaskGet tools."""

import asyncio

import pytest
from conftest import InMemMemoryExtension, InMemMessageStore, MessageOnlyConsolidator

from bos.core import ReactAgent
from bos.core.defaults.agent_spec import bos_maxims
from bos.core.defaults.skills_loader import FileSystemSkillsLoader


def _create_agent(**kwargs):
    kwargs.setdefault("message_store", InMemMessageStore())
    kwargs.setdefault("memory", InMemMemoryExtension())
    kwargs.setdefault("consolidator", MessageOnlyConsolidator())
    kwargs.setdefault("skills_loader", FileSystemSkillsLoader())
    kwargs.setdefault("maxims", bos_maxims)
    kwargs.setdefault("system_prompt", "test")
    return ReactAgent(**kwargs)


class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_creates_task_with_id_and_subject(self):
        agent = _create_agent()
        result = await agent._invoke_tool(
            "TaskCreate", subject="Fix auth bug", description="Fix login redirect loop."
        )
        assert "Fix auth bug" in result
        assert "status: pending" in result
        assert "Task created:" in result

    @pytest.mark.asyncio
    async def test_tasks_are_scoped_by_chat_id(self):
        agent = _create_agent()
        await agent._invoke_tool("TaskCreate", subject="Task A", description="desc", chat_id="chat-1")
        await agent._invoke_tool("TaskCreate", subject="Task B", description="desc", chat_id="chat-2")
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
        result = await agent._invoke_tool("TaskCreate", subject="T", description="d")
        task_id = _extract_task_id(result)
        await agent._invoke_tool("TaskUpdate", taskId=task_id, status="in_progress")
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "in_progress" in detail

    @pytest.mark.asyncio
    async def test_set_status_completed(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskCreate", subject="T", description="d")
        task_id = _extract_task_id(result)
        await agent._invoke_tool("TaskUpdate", taskId=task_id, status="completed")
        detail = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "completed" in detail

    @pytest.mark.asyncio
    async def test_delete_removes_task(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskCreate", subject="T", description="d")
        task_id = _extract_task_id(result)
        del_result = await agent._invoke_tool("TaskUpdate", taskId=task_id, status="deleted")
        assert "deleted" in del_result
        get_result = await agent._invoke_tool("TaskGet", taskId=task_id)
        assert "not found" in get_result

    @pytest.mark.asyncio
    async def test_delete_cleans_up_blocked_by_references(self):
        agent = _create_agent()
        r1 = await agent._invoke_tool("TaskCreate", subject="Backend", description="d")
        r2 = await agent._invoke_tool("TaskCreate", subject="Frontend", description="d")
        id1 = _extract_task_id(r1)
        id2 = _extract_task_id(r2)
        await agent._invoke_tool("TaskUpdate", taskId=id2, addBlockedBy=[id1])
        # delete the blocker
        await agent._invoke_tool("TaskUpdate", taskId=id1, status="deleted")
        detail = await agent._invoke_tool("TaskGet", taskId=id2)
        assert "Blocked by:" not in detail

    @pytest.mark.asyncio
    async def test_delete_cleans_up_blocks_references(self):
        agent = _create_agent()
        r1 = await agent._invoke_tool("TaskCreate", subject="Blocker", description="d")
        r2 = await agent._invoke_tool("TaskCreate", subject="Blocked", description="d")
        id1 = _extract_task_id(r1)
        id2 = _extract_task_id(r2)
        await agent._invoke_tool("TaskUpdate", taskId=id1, addBlocks=[id2])
        # delete the blocked task
        await agent._invoke_tool("TaskUpdate", taskId=id2, status="deleted")
        detail = await agent._invoke_tool("TaskGet", taskId=id1)
        assert "Blocks:" not in detail

    @pytest.mark.asyncio
    async def test_completed_blocker_clears_blocked_by(self):
        agent = _create_agent()
        r1 = await agent._invoke_tool("TaskCreate", subject="Design", description="d")
        r2 = await agent._invoke_tool("TaskCreate", subject="Implement", description="d")
        id1 = _extract_task_id(r1)
        id2 = _extract_task_id(r2)
        await agent._invoke_tool("TaskUpdate", taskId=id2, addBlockedBy=[id1])
        # complete the blocker — blocked_by should clear
        await agent._invoke_tool("TaskUpdate", taskId=id1, status="completed")
        detail = await agent._invoke_tool("TaskGet", taskId=id2)
        assert "Blocked by:" not in detail

    @pytest.mark.asyncio
    async def test_invalid_task_id_returns_error(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskUpdate", taskId="nonexistent", status="in_progress")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_invalid_blocked_task_returns_error(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskCreate", subject="T", description="d")
        task_id = _extract_task_id(result)
        result = await agent._invoke_tool("TaskUpdate", taskId=task_id, addBlocks=["nonexistent"])
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_update_subject_and_description(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskCreate", subject="Old", description="Old desc")
        task_id = _extract_task_id(result)
        await agent._invoke_tool("TaskUpdate", taskId=task_id, subject="New", description="New desc")
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
        r1 = await agent._invoke_tool("TaskCreate", subject="First", description="d")
        await asyncio.sleep(0.01)
        r2 = await agent._invoke_tool("TaskCreate", subject="Second", description="d")
        await asyncio.sleep(0.01)
        r3 = await agent._invoke_tool("TaskCreate", subject="Third", description="d")
        id1 = _extract_task_id(r1)
        id2 = _extract_task_id(r2)
        id3 = _extract_task_id(r3)
        result = await agent._invoke_tool("TaskList")
        lines = result.split("\n")
        subjects = [line for line in lines if line.startswith("[")]
        assert subjects[0].startswith(f"[{id1}]")
        assert subjects[1].startswith(f"[{id2}]")
        assert subjects[2].startswith(f"[{id3}]")

    @pytest.mark.asyncio
    async def test_shows_blocked_by_and_blocks(self):
        agent = _create_agent()
        r1 = await agent._invoke_tool("TaskCreate", subject="Design", description="d")
        r2 = await agent._invoke_tool("TaskCreate", subject="Implement", description="d")
        id1 = _extract_task_id(r1)
        id2 = _extract_task_id(r2)
        await agent._invoke_tool("TaskUpdate", taskId=id2, addBlockedBy=[id1])
        result = await agent._invoke_tool("TaskList")
        assert "blocked by:" in result
        assert "blocks:" in result


class TestTaskGet:
    @pytest.mark.asyncio
    async def test_returns_full_details(self):
        agent = _create_agent()
        result = await agent._invoke_tool("TaskCreate", subject="My Task", description="Do the thing.")
        task_id = _extract_task_id(result)
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
        r1 = await agent._invoke_tool("TaskCreate", subject="Setup", description="d")
        r2 = await agent._invoke_tool("TaskCreate", subject="Build", description="d")
        id1 = _extract_task_id(r1)
        id2 = _extract_task_id(r2)
        await agent._invoke_tool("TaskUpdate", taskId=id2, addBlockedBy=[id1])
        detail = await agent._invoke_tool("TaskGet", taskId=id2)
        assert f"Blocked by: {id1}" in detail


def _extract_task_id(result: str) -> str:
    """Extract task id from a TaskCreate result like 'Task created: [abc12345] Subj (status: pending)'."""
    bracket_start = result.index("[") + 1
    bracket_end = result.index("]")
    return result[bracket_start:bracket_end]
