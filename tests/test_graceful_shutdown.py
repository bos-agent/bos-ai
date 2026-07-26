"""Graceful shutdown: an interrupted turn closes with a handoff, not a corpse.

Shutdown does not wait for a turn's work to finish — a turn can run for minutes.
It asks the turn to stop, and the turn abandons what it was awaiting, closes with
a handoff summarizing what it established, persists it, and replies normally.
"""

import asyncio
import uuid

import pytest
from conftest import InMemChatStore, RecordingConsolidator, create_test_agent

from bos.core import LLMResponse, ToolCallRequest, ep_provider
from bos.core.actor import Envelope, MessageType
from bos.core.agent.agent import INTERRUPTED_TOOL_CONTENT, SHUTDOWN_CONTENT, SHUTDOWN_HANDOFF_INSTRUCTION
from bos.core.registry import ToolRegistry
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway.actors.agent_actor import AgentActor


class CaptureSink:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _tool_calling_provider(*, tool_name: str) -> str:
    """A provider that always asks for one more tool call, so the turn is
    mid-flight whenever the stop lands."""
    provider_name = f"test_shutdown_provider_{uuid.uuid4().hex}"

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"call_{uuid.uuid4().hex}", name=tool_name, arguments={})],
            finish_reason="tool_calls",
        )

    return provider_name


def _blocking_tool(started: asyncio.Event) -> ToolRegistry:
    tools = ToolRegistry(f"_block_tools:{uuid.uuid4().hex}")

    @tools(
        name="SlowStep",
        description="Work that outlives the shutdown grace.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def slow_step() -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    return tools


def _slow_teardown_tool(started: asyncio.Event, tearing_down: asyncio.Event, *, teardown: float) -> ToolRegistry:
    """A tool that blocks, and whose cancellation teardown itself takes time —
    the shape that used to let an abandoned tool hold the whole stop open."""
    tools = ToolRegistry(f"_slow_teardown_tools:{uuid.uuid4().hex}")

    @tools(
        name="SlowStep",
        description="Work with slow async teardown on cancellation.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def slow_step() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tearing_down.set()
            await asyncio.sleep(teardown)
            raise
        return "never"

    return tools


# ── agent: closing a turn on request ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_during_tool_call_closes_with_handoff():
    """The long pole is a running tool. Stop abandons it rather than waiting."""
    started = asyncio.Event()
    provider_name = _tool_calling_provider(tool_name="SlowStep")
    consolidator = RecordingConsolidator("**Done** — read BEP 4.\n**Left** — write §3.")
    store = InMemChatStore()
    sink = CaptureSink()

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=_blocking_tool(started),
            tools=["SlowStep"],
            chat_store=store,
            consolidator=consolidator,
        )
        turn = asyncio.create_task(agent.ask("shutdown-chat", "Write §3.", event_sink=sink))
        await asyncio.wait_for(started.wait(), timeout=2)
        agent.request_stop()
        result = await asyncio.wait_for(turn, timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    # The turn answered instead of dying: marker, then the handoff.
    assert result.startswith(SHUTDOWN_CONTENT)
    assert consolidator.summary in result
    assert consolidator.calls[0][1] == SHUTDOWN_HANDOFF_INSTRUCTION

    # The reply is the persisted assistant turn, so a restarted gateway resumes
    # from the handoff rather than from a truncated turn.
    persisted = await store.get_messages("shutdown-chat")
    assert persisted[-1].llm_message["role"] == "assistant"
    assert persisted[-1].llm_message["content"] == result

    event = [e for e in sink.events if e.detail == "shutdown"][0]
    assert event.metadata["handoff"] is True
    assert event.metadata["closure_reason"] == "shutdown"


@pytest.mark.asyncio
async def test_abandoned_tool_calls_still_get_results():
    """Every advertised tool call needs a result or the persisted turn is a
    sequence the next turn cannot replay."""
    started = asyncio.Event()
    provider_name = _tool_calling_provider(tool_name="SlowStep")
    store = InMemChatStore()

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=_blocking_tool(started),
            tools=["SlowStep"],
            chat_store=store,
            consolidator=RecordingConsolidator(),
        )
        turn = asyncio.create_task(agent.ask("tool-repair-chat", "go", event_sink=CaptureSink()))
        await asyncio.wait_for(started.wait(), timeout=2)
        agent.request_stop()
        await asyncio.wait_for(turn, timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    persisted = await store.get_messages("tool-repair-chat")
    advertised = {
        call["id"]
        for m in persisted
        for call in (m.llm_message.get("tool_calls") or [])
    }
    answered = {m.llm_message.get("tool_call_id") for m in persisted if m.llm_message.get("role") == "tool"}
    assert advertised
    assert advertised <= answered

    interrupted = [m for m in persisted if m.llm_message.get("content") == INTERRUPTED_TOOL_CONTENT]
    assert len(interrupted) == len(advertised)


def _parallel_pair(fast_done: asyncio.Event, slow_started: asyncio.Event) -> ToolRegistry:
    """Two parallel-safe tools, so the turn runs them as one batch. ``AskSubagent``
    is the real instance of this shape: parallel-safe *and* side-effecting."""
    tools = ToolRegistry(f"_parallel_stop_tools:{uuid.uuid4().hex}")

    @tools(
        name="FastStep",
        description="Completes before the stop lands.",
        parameters={"type": "object", "properties": {}, "required": []},
        parallel_safe=True,
    )
    async def fast_step() -> str:
        fast_done.set()
        return "FAST_RESULT"

    @tools(
        name="SlowStep",
        description="Work that outlives the shutdown grace.",
        parameters={"type": "object", "properties": {}, "required": []},
        parallel_safe=True,
    )
    async def slow_step() -> str:
        slow_started.set()
        await asyncio.Event().wait()
        return "never"

    return tools


@pytest.mark.asyncio
async def test_completed_calls_in_an_abandoned_batch_keep_their_results():
    """A parallel batch is abandoned as a unit, but a call that already returned
    has already had its effect. Recording it as "abandoned unfinished" tells the
    next turn to redo it."""
    fast_done, slow_started = asyncio.Event(), asyncio.Event()
    provider_name = f"test_shutdown_parallel_{uuid.uuid4().hex}"
    store = InMemChatStore()

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="call_fast", name="FastStep", arguments={}),
                ToolCallRequest(id="call_slow", name="SlowStep", arguments={}),
            ],
            finish_reason="tool_calls",
        )

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=_parallel_pair(fast_done, slow_started),
            tools=["FastStep", "SlowStep"],
            chat_store=store,
            consolidator=RecordingConsolidator("**Done** — ran FastStep."),
        )
        turn = asyncio.create_task(agent.ask("parallel-stop-chat", "go", event_sink=CaptureSink()))
        await asyncio.wait_for(fast_done.wait(), timeout=2)
        await asyncio.wait_for(slow_started.wait(), timeout=2)
        agent.request_stop()
        await asyncio.wait_for(turn, timeout=3)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    persisted = await store.get_messages("parallel-stop-chat")
    results = {
        m.llm_message["tool_call_id"]: m.llm_message["content"]
        for m in persisted
        if m.llm_message.get("role") == "tool"
    }
    # Both calls are answered — the persisted turn stays replayable …
    assert set(results) == {"call_fast", "call_slow"}
    # … but only the one that really did not finish is marked interrupted.
    assert results["call_fast"] == "FAST_RESULT"
    assert results["call_slow"] == INTERRUPTED_TOOL_CONTENT


@pytest.mark.asyncio
async def test_stop_during_llm_call_abandons_it_without_a_handoff():
    """Nothing was established yet, so the marker is the honest answer — and no
    consolidator call is spent producing a paraphrase of the request."""
    provider_name = f"test_shutdown_provider_{uuid.uuid4().hex}"
    entered = asyncio.Event()
    consolidator = RecordingConsolidator()

    @ep_provider(name=provider_name)
    async def blocking_provider(messages, model=None, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return LLMResponse(content="never", tool_calls=[], finish_reason="stop")

    try:
        agent = create_test_agent(model=f"{provider_name}/block", consolidator=consolidator)
        turn = asyncio.create_task(agent.ask("llm-stop-chat", "hello"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        agent.request_stop()
        result = await asyncio.wait_for(turn, timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result == SHUTDOWN_CONTENT
    assert consolidator.calls == []


@pytest.mark.asyncio
async def test_abandoning_a_slow_unwinding_tool_is_bounded(monkeypatch):
    """A tool that drags its heels on cancellation must not extend the stop:
    the turn stops waiting for the unwind and goes on to close."""
    from bos.core.agent import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_ABANDON_TEARDOWN_SECONDS", 0.05)
    started, tearing_down = asyncio.Event(), asyncio.Event()
    provider_name = _tool_calling_provider(tool_name="SlowStep")

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=_slow_teardown_tool(started, tearing_down, teardown=30),
            tools=["SlowStep"],
            chat_store=InMemChatStore(),
            consolidator=RecordingConsolidator("**Done** — read BEP 4."),
        )
        turn = asyncio.create_task(agent.ask("slow-unwind-chat", "go", event_sink=CaptureSink()))
        await asyncio.wait_for(started.wait(), timeout=2)
        agent.request_stop()
        result = await asyncio.wait_for(turn, timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result.startswith(SHUTDOWN_CONTENT)
    assert tearing_down.is_set()  # the tool was cancelled, we just did not wait it out


@pytest.mark.asyncio
async def test_escalating_while_a_tool_unwinds_cancels_the_turn():
    """The hard path must stay hard. A cancel landing while the turn waits for
    an abandoned tool to unwind is *ours* — it must not be mistaken for the
    tool finishing, which would let a turn the host gave up on run a handoff."""
    started, tearing_down = asyncio.Event(), asyncio.Event()
    provider_name = _tool_calling_provider(tool_name="SlowStep")
    consolidator = RecordingConsolidator("should never be produced")

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=_slow_teardown_tool(started, tearing_down, teardown=30),
            tools=["SlowStep"],
            chat_store=InMemChatStore(),
            consolidator=consolidator,
        )
        turn = asyncio.create_task(agent.ask("escalate-chat", "go", event_sink=CaptureSink()))
        await asyncio.wait_for(started.wait(), timeout=2)
        agent.request_stop()
        await asyncio.wait_for(tearing_down.wait(), timeout=2)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert turn.cancelled()
    assert consolidator.calls == []  # no handoff was produced for an escalated turn


@pytest.mark.asyncio
async def test_stop_between_iterations_closes_on_completed_work():
    """A stop that lands between iterations keeps the finished tool result."""
    provider_name = _tool_calling_provider(tool_name="QuickStep")
    consolidator = RecordingConsolidator("**Done** — ran QuickStep.")
    store = InMemChatStore()
    tools = ToolRegistry(f"_quick_tools:{uuid.uuid4().hex}")
    agents: list = []

    @tools(
        name="QuickStep",
        description="Finish one step, then the host stops us.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def quick_step() -> str:
        agents[0].request_stop()
        return "checked docs/BEP/0004.md"

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=tools,
            tools=["QuickStep"],
            chat_store=store,
            consolidator=consolidator,
        )
        agents.append(agent)
        result = await asyncio.wait_for(agent.ask("boundary-chat", "go"), timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result.startswith(SHUTDOWN_CONTENT)
    assert consolidator.summary in result

    # The completed tool result survived — it was not replaced by the marker.
    persisted = await store.get_messages("boundary-chat")
    tool_results = [m.llm_message.get("content") for m in persisted if m.llm_message.get("role") == "tool"]
    assert tool_results == ["checked docs/BEP/0004.md"]


@pytest.mark.asyncio
async def test_shutdown_handoff_can_be_disabled():
    started = asyncio.Event()
    provider_name = _tool_calling_provider(tool_name="SlowStep")
    consolidator = RecordingConsolidator()

    try:
        agent = create_test_agent(
            model=f"{provider_name}/loop",
            local_tools=_blocking_tool(started),
            tools=["SlowStep"],
            consolidator=consolidator,
            shutdown_handoff=False,
        )
        turn = asyncio.create_task(agent.ask("no-handoff-chat", "go"))
        await asyncio.wait_for(started.wait(), timeout=2)
        agent.request_stop()
        result = await asyncio.wait_for(turn, timeout=2)
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result == SHUTDOWN_CONTENT
    assert consolidator.calls == []


@pytest.mark.asyncio
async def test_shutdown_handoff_config_key_reaches_agent_kwargs():
    from bos.config.schema import AgentConfig, _agent_config_to_core_kwargs

    kwargs = _agent_config_to_core_kwargs(AgentConfig.model_validate({"shutdown_handoff": False}))

    assert kwargs["shutdown_handoff"] is False
    assert AgentConfig().shutdown_handoff is True


# ── actor: draining in-flight turns ────────────────────────────────────────


class StopAwareAgent:
    """Closes its turn when asked, the way the real agent's handoff path does."""

    name = "main"

    def __init__(self, *, obeys: bool = True) -> None:
        self._stop = asyncio.Event()
        self._obeys = obeys
        self.started = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def ask(self, chat_id, content, **kwargs):
        self.started.set()
        if not self._obeys:
            await asyncio.Event().wait()
        await self._stop.wait()
        return "(interrupted) here is where I got to"


def _actor_with(agent) -> tuple[AgentActor, object, object]:
    InMemMailRoute._queues = {}
    route = InMemMailRoute()
    actor_box = route.bind("agent@main")
    client_box = route.bind("channel@demo")
    return AgentActor(agent, actor_box), client_box, route


@pytest.mark.asyncio
async def test_drain_delivers_the_handoff_reply():
    agent = StopAwareAgent()
    actor, client_box, _ = _actor_with(agent)
    task = asyncio.create_task(actor.run())
    try:
        await client_box.send("agent@main", "long job", chat_id="chat-1")
        await asyncio.wait_for(agent.started.wait(), timeout=2)

        await asyncio.wait_for(actor.drain(2.0), timeout=3)

        reply = await asyncio.wait_for(client_box.receive(), timeout=2)
        assert reply.content == "(interrupted) here is where I got to"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_draining_actor_refuses_new_turns():
    agent = StopAwareAgent()
    actor, client_box, _ = _actor_with(agent)
    await actor.drain(0)

    await actor.handle(
        Envelope(
            sender="channel@demo",
            recipient="agent@main",
            content="please start",
            content_type=MessageType.MESSAGE,
            chat_id="chat-2",
        )
    )

    reply = await asyncio.wait_for(client_box.receive(), timeout=2)
    assert reply.content_type == MessageType.SYSTEM
    assert reply.metadata["event"] == "shutting_down"
    assert not agent.started.is_set()


@pytest.mark.asyncio
async def test_drain_is_bounded_when_a_turn_ignores_the_stop():
    """The grace is a deadline, not a promise: a turn that will not close is
    left for the hard cancel rather than holding shutdown open."""
    agent = StopAwareAgent(obeys=False)
    actor, client_box, _ = _actor_with(agent)
    task = asyncio.create_task(actor.run())
    try:
        await client_box.send("agent@main", "stubborn job", chat_id="chat-3")
        await asyncio.wait_for(agent.started.wait(), timeout=2)

        await asyncio.wait_for(actor.drain(0.05), timeout=2)

        turn_task = actor._sessions["chat-3"].execution.task
        assert turn_task is not None and not turn_task.done()

        await actor.aclose()
        assert turn_task.cancelled()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ── gateway: stop ordering ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_stops_actors_before_channels(tmp_path, monkeypatch):
    """Channels must outlive the drain — they carry the handoff out. The old
    order stopped the consumer first and left clients waiting on nothing."""
    from bos.config import Workspace
    from bos.extensions.chat_stores.in_memory import InMemChatStore as Store
    from bos.gateway import Gateway

    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")

    class FakeHarness:
        def __init__(self) -> None:
            InMemMailRoute._queues = {}
            self.chat_store = Store()
            self.mail_route = InMemMailRoute()

        async def create_agent(self, kind=None, agent_cfg=None):
            return StopAwareAgent()

    ws = Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {
                    "port": 0,
                    "api_key_env": "BOS_TEST_GATEWAY_KEY",
                    "shutdown_grace_seconds": 0.1,
                },
                "main_actor": "main",
                "actors": {"main": {"agent": "main"}},
            }
        },
    )
    gateway = Gateway(runtime=ws.resolve_gateway_runtime(), harness=FakeHarness())

    order: list[str] = []
    actor_stop, channel_stop = gateway.actor_manager.stop_all, gateway.channel_manager.stop_all

    async def _actors(**kwargs):
        order.append(f"actors(drain_grace={kwargs.get('drain_grace')})")
        await actor_stop(**kwargs)

    async def _channels():
        order.append("channels")
        await channel_stop()

    monkeypatch.setattr(gateway.actor_manager, "stop_all", _actors)
    monkeypatch.setattr(gateway.channel_manager, "stop_all", _channels)

    run = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.1)
    assert not gateway.shutdown_requested

    gateway.request_shutdown()
    await asyncio.wait_for(run, timeout=5)

    assert order == ["actors(drain_grace=0.1)", "channels"]


@pytest.mark.asyncio
async def test_cancelling_the_gateway_skips_the_drain(tmp_path, monkeypatch):
    """Cancellation is the forceful path — it must not hold the stop open for
    turns that were told to hurry."""
    from bos.config import Workspace
    from bos.extensions.chat_stores.in_memory import InMemChatStore as Store
    from bos.gateway import Gateway

    monkeypatch.setenv("BOS_TEST_GATEWAY_KEY", "secret")

    class FakeHarness:
        def __init__(self) -> None:
            InMemMailRoute._queues = {}
            self.chat_store = Store()
            self.mail_route = InMemMailRoute()

        async def create_agent(self, kind=None, agent_cfg=None):
            return StopAwareAgent()

    ws = Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {
                    "port": 0,
                    "api_key_env": "BOS_TEST_GATEWAY_KEY",
                    "shutdown_grace_seconds": 30,
                },
                "main_actor": "main",
                "actors": {"main": {"agent": "main"}},
            }
        },
    )
    gateway = Gateway(runtime=ws.resolve_gateway_runtime(), harness=FakeHarness())

    grace_used: list[float] = []
    actor_stop = gateway.actor_manager.stop_all

    async def _actors(**kwargs):
        grace_used.append(kwargs.get("drain_grace"))
        await actor_stop(**kwargs)

    monkeypatch.setattr(gateway.actor_manager, "stop_all", _actors)

    run = asyncio.create_task(gateway.run())
    await asyncio.sleep(0.1)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)

    assert grace_used == [0.0]


# ── consolidator projection ────────────────────────────────────────────────


def test_consolidation_prompt_has_no_unpaired_tool_messages():
    """A handoff summarizes a turn that was mid-tool-call, so its history
    carries tool traffic. Projecting that to a bare `role: "tool"` message
    drops the call it answers, and strict providers reject the whole prompt —
    which would silently degrade every handoff to the static marker."""
    from bos.core import Message
    from bos.core.defaults.consolidator import _project_history

    projected = _project_history([
        Message(llm_message={"role": "user", "content": "sleep then report"}),
        Message(
            llm_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Sleep", "arguments": "{}"}}],
            }
        ),
        Message(llm_message={"role": "tool", "tool_call_id": "call_1", "name": "Sleep", "content": "(interrupted)"}),
    ])

    assert [m["role"] for m in projected] == ["user", "assistant", "user"]
    assert all("tool_call_id" not in m and "tool_calls" not in m for m in projected)
    # The tool traffic is still legible to the summarizer, just as plain text.
    assert "Sleep" in projected[1]["content"]
    assert "(interrupted)" in projected[2]["content"]
