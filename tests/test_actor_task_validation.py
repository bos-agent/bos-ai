import asyncio

import pytest

from bos.core import AgentActor, InMemMailRoute
from bos.core.tasks import TaskLedger, task_chat_id, task_metadata
from bos.protocol import Envelope, MessageType


class RecordingAgent:
    def __init__(self):
        self.calls = []

    async def ask(self, chat_id, content, **kwargs):
        self.calls.append((chat_id, content, kwargs))
        return "ok"


@pytest.mark.asyncio
async def test_actor_rejects_task_chat_bound_to_another_actor():
    ledger = TaskLedger()
    task_record = ledger.create_task(goal="Research", created_by="agent@main", assigned_to="agent@researcher")
    chat_id = task_chat_id(task_record.id)
    ledger.bind_chat(task_id=task_record.id, chat_id=chat_id, actor_address="agent@researcher")
    route = InMemMailRoute()
    agent = RecordingAgent()
    actor = AgentActor(agent, route.bind("agent@main"), task_ledger=ledger)
    sender = route.bind("agent@researcher")

    task = asyncio.create_task(actor.run())
    try:
        await sender.send(
            "agent@main",
            "worker reply",
            chat_id=chat_id,
            metadata=task_metadata(task_record, chat_id=chat_id),
        )
        response = await asyncio.wait_for(sender.receive(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert response.content_type == MessageType.SYSTEM
    assert "bound to actor" in response.content
    assert agent.calls == []


@pytest.mark.asyncio
async def test_actor_accepts_task_chat_for_bound_actor():
    ledger = TaskLedger()
    task_record = ledger.create_task(goal="Research", created_by="agent@main", assigned_to="agent@researcher")
    chat_id = task_chat_id(task_record.id)
    ledger.bind_chat(task_id=task_record.id, chat_id=chat_id, actor_address="agent@researcher")
    route = InMemMailRoute()
    agent = RecordingAgent()
    actor = AgentActor(agent, route.bind("agent@researcher"), task_ledger=ledger)
    sender = route.bind("agent@main")

    task = asyncio.create_task(actor.run())
    try:
        await sender.send(
            "agent@researcher",
            "task input",
            chat_id=chat_id,
            metadata=task_metadata(task_record, chat_id=chat_id),
        )
        response = await asyncio.wait_for(sender.receive(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert response.content == "ok"
    assert agent.calls[0][0] == chat_id


@pytest.mark.asyncio
async def test_worker_task_reply_is_routed_to_coordinator_owned_task_chat_without_reply_loop():
    suffix = "roundtrip"
    researcher_address = f"agent@researcher-{suffix}"
    coordinator_address = f"agent@main-{suffix}"
    ledger = TaskLedger()
    task_record = ledger.create_task(goal="Research", created_by=coordinator_address, assigned_to=researcher_address)
    worker_chat_id = task_chat_id(task_record.id)
    ledger.bind_chat(task_id=task_record.id, chat_id=worker_chat_id, actor_address=researcher_address)
    route = InMemMailRoute()
    worker_agent = RecordingAgent()
    worker_agent.ask = lambda *args, **kwargs: _async_response("worker result")  # type: ignore[method-assign]
    coordinator_agent = RecordingAgent()
    coordinator_agent.ask = lambda *args, **kwargs: _record_and_respond(  # type: ignore[method-assign]
        coordinator_agent,
        *args,
        response="coordinator saw worker result",
        **kwargs,
    )
    worker = AgentActor(worker_agent, route.bind(researcher_address), task_ledger=ledger)
    coordinator = AgentActor(coordinator_agent, route.bind(coordinator_address), task_ledger=ledger)

    worker_task = asyncio.create_task(worker.run())
    coordinator_task = asyncio.create_task(coordinator.run())
    try:
        await route.deliver(
            Envelope(
                sender=coordinator_address,
                recipient=researcher_address,
                content="task input",
                chat_id=worker_chat_id,
                metadata=task_metadata(task_record, chat_id=worker_chat_id),
            )
        )
        for _ in range(20):
            if coordinator_agent.calls:
                break
            await asyncio.sleep(0.05)
    finally:
        worker_task.cancel()
        coordinator_task.cancel()
        await asyncio.gather(worker_task, coordinator_task, return_exceptions=True)

    assert coordinator_agent.calls
    coordinator_chat_id = coordinator_agent.calls[0][0]
    assert coordinator_chat_id != worker_chat_id
    assert ledger.get_binding(coordinator_chat_id).actor_address == coordinator_address
    assert "worker result" in coordinator_agent.calls[0][1]


async def _async_response(response: str) -> str:
    return response


async def _record_and_respond(agent: RecordingAgent, chat_id, content, response: str, **kwargs):
    agent.calls.append((chat_id, content, kwargs))
    return response
