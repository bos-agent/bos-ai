import asyncio

import pytest

from bos.core import AgentActor, InMemMailRoute
from bos.protocol import Envelope, MessageType
from bos.team.runtime import PeerTaskRuntime
from bos.team.tasks import TaskLedger, task_chat_id, task_metadata


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
    actor = AgentActor(agent, route.bind("agent@main"), actor_runtime=PeerTaskRuntime(ledger, "agent@main"))
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
    assert response.chat_id is None
    assert "bound to actor" in response.content
    assert agent.calls == []


@pytest.mark.asyncio
async def test_actor_does_not_bounce_system_errors_on_task_chats():
    main_address = "agent@main-no-bounce"
    researcher_address = "agent@researcher-no-bounce"
    ledger = TaskLedger()
    task_record = ledger.create_task(goal="Research", created_by=main_address, assigned_to=researcher_address)
    chat_id = task_chat_id(task_record.id)
    ledger.bind_chat(task_id=task_record.id, chat_id=chat_id, actor_address=researcher_address)
    route = InMemMailRoute()
    actor = AgentActor(
        RecordingAgent(),
        route.bind(main_address),
        actor_runtime=PeerTaskRuntime(ledger, main_address),
    )

    task = asyncio.create_task(actor.run())
    try:
        await route.deliver(
            Envelope(
                sender=researcher_address,
                recipient=main_address,
                content="(error: missing metadata)",
                content_type=MessageType.SYSTEM,
                chat_id=chat_id,
                metadata={"actor_runtime_validation_error": "previous failure"},
            )
        )
        await asyncio.sleep(0.1)
        response = await route.receive_nowait(researcher_address)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert response is None


@pytest.mark.asyncio
async def test_actor_accepts_task_chat_for_bound_actor():
    ledger = TaskLedger()
    task_record = ledger.create_task(goal="Research", created_by="agent@main", assigned_to="agent@researcher")
    chat_id = task_chat_id(task_record.id)
    ledger.bind_chat(task_id=task_record.id, chat_id=chat_id, actor_address="agent@researcher")
    route = InMemMailRoute()
    agent = RecordingAgent()
    actor = AgentActor(
        agent,
        route.bind("agent@researcher"),
        actor_runtime=PeerTaskRuntime(ledger, "agent@researcher"),
    )
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
    worker = AgentActor(
        worker_agent,
        route.bind(researcher_address),
        actor_runtime=PeerTaskRuntime(ledger, researcher_address),
    )
    coordinator = AgentActor(
        coordinator_agent,
        route.bind(coordinator_address),
        actor_runtime=PeerTaskRuntime(ledger, coordinator_address),
    )

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


def test_worker_task_reply_routes_to_original_chat_until_sibling_tasks_complete():
    coordinator_address = "agent@main-source-route"
    researcher_address = "agent@researcher-source-route"
    reviewer_address = "agent@reviewer-source-route"
    user_chat_id = "user-chat-source-route"
    user_sender = "channel@http"
    ledger = TaskLedger()
    first_task = ledger.create_task(
        goal="Pick a number",
        created_by=coordinator_address,
        assigned_to=researcher_address,
        metadata={"source_chat_id": user_chat_id, "source_sender": user_sender},
    )
    second_task = ledger.create_task(
        goal="Pick another number",
        created_by=coordinator_address,
        assigned_to=reviewer_address,
        metadata={"source_chat_id": user_chat_id, "source_sender": user_sender},
    )
    first_worker_chat = task_chat_id(first_task.id)
    second_worker_chat = task_chat_id(second_task.id)
    ledger.bind_chat(task_id=first_task.id, chat_id=first_worker_chat, actor_address=researcher_address)
    ledger.bind_chat(task_id=second_task.id, chat_id=second_worker_chat, actor_address=reviewer_address)
    researcher_runtime = PeerTaskRuntime(ledger, researcher_address)
    reviewer_runtime = PeerTaskRuntime(ledger, reviewer_address)

    ledger.append_event(first_task.id, "completed", actor=researcher_address, content="42", result="42")
    routed = researcher_runtime.route_response(
        source_chat_id=first_worker_chat,
        reply_chat_id=first_worker_chat,
        reply_recipient=coordinator_address,
        response="42",
    )

    assert routed.chat_id == user_chat_id
    assert routed.metadata["reply_to"] == user_sender
    assert routed.metadata["no_reply"] is True
    assert "Pending related task(s)" in routed.content
    assert first_task.id in routed.content
    assert second_task.id in routed.content

    ledger.append_event(second_task.id, "completed", actor=reviewer_address, content="7", result="7")
    routed = reviewer_runtime.route_response(
        source_chat_id=second_worker_chat,
        reply_chat_id=second_worker_chat,
        reply_recipient=coordinator_address,
        response="7",
    )

    assert routed.chat_id == user_chat_id
    assert routed.metadata["reply_to"] == user_sender
    assert "no_reply" not in routed.metadata
    assert "All related tasks" in routed.content
    assert "result: 42" in routed.content
    assert "result: 7" in routed.content


def test_actor_honors_reply_to_only_for_actor_senders():
    user_env = Envelope(sender="channel@http", recipient="agent@main", content="hi", metadata={"reply_to": "agent@x"})
    actor_env = Envelope(
        sender="agent@researcher",
        recipient="agent@main",
        content="done",
        metadata={"reply_to": "channel@http"},
    )

    assert AgentActor._reply_recipient_for(user_env) == "channel@http"
    assert AgentActor._reply_recipient_for(actor_env) == "channel@http"


async def _async_response(response: str) -> str:
    return response


async def _record_and_respond(agent: RecordingAgent, chat_id, content, response: str, **kwargs):
    agent.calls.append((chat_id, content, kwargs))
    return response
