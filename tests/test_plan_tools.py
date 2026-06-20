from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import create_test_agent, dummy_turn_context

from bos.config.default_agent_spec import default_agent_spec
from bos.core import LLMResponse, ep_provider
from bos.plugins.plan import (
    PlanAgentPlugin,
    PlanContextInterceptor,
    PlanEventInterceptor,
    _Plan,
    _render_current_plan,
)


@pytest.mark.asyncio
async def test_plan_tools_create_get_update_and_clear_current_plan():
    plugin = PlanAgentPlugin()
    agent = create_test_agent(plugins=[plugin], tools=["PlanCreate", "PlanGet", "PlanUpdate", "PlanClear"])

    created = json.loads(
        await agent._invoke_tool(
            "PlanCreate",
            chat_id="plan-chat",
            objective="Add structured plan mode",
            user_value="Align before implementation",
            appetite="Small first slice",
            constraints=["No client-side question tool"],
            breakdown=["Add plugin", "Add tests"],
            verification=["Run targeted pytest"],
            open_questions=["Should approval be explicit?"],
            status="needs_input",
        )
    )
    assert created["result"] == "Plan created."
    assert created["plan"]["objective"] == "Add structured plan mode"
    assert created["plan"]["status"] == "needs_input"

    fetched = json.loads(await agent._invoke_tool("PlanGet", chat_id="plan-chat"))
    assert fetched["plan"]["breakdown"] == ["Add plugin", "Add tests"]

    updated = json.loads(
        await agent._invoke_tool(
            "PlanUpdate",
            chat_id="plan-chat",
            status="in_progress",
            open_questions=[],
            shaped_solution="Use a prompt section plus per-chat state tools.",
        )
    )
    assert updated["plan"]["status"] == "in_progress"
    assert updated["plan"]["open_questions"] == []
    assert updated["plan"]["shaped_solution"] == "Use a prompt section plus per-chat state tools."

    cleared = json.loads(await agent._invoke_tool("PlanClear", chat_id="plan-chat"))
    assert cleared == {"result": "Plan cleared."}
    assert json.loads(await agent._invoke_tool("PlanGet", chat_id="plan-chat")) == {"plan": None}


@pytest.mark.asyncio
async def test_plan_update_requires_existing_plan_and_valid_status():
    plugin = PlanAgentPlugin()
    agent = create_test_agent(plugins=[plugin], tools=["PlanCreate", "PlanUpdate"])

    missing = json.loads(await agent._invoke_tool("PlanUpdate", chat_id="missing-chat", status="approved"))
    assert missing["error"] == "No plan exists for this conversation. Use PlanCreate first."

    invalid = json.loads(
        await agent._invoke_tool(
            "PlanCreate",
            chat_id="bad-status-chat",
            objective="Do work",
            status="waiting",
        )
    )
    assert "Invalid status 'waiting'" in invalid["error"]


@pytest.mark.asyncio
async def test_plan_plugin_keeps_current_plan_out_of_system_prompt():
    plugin = PlanAgentPlugin()
    agent = create_test_agent(plugins=[plugin], tools=["PlanCreate"])
    await agent._invoke_tool(
        "PlanCreate",
        chat_id="render-chat",
        objective="Implement <plan> support",
        constraints=["Keep prompt XML safe"],
        open_questions=["Proceed?"],
        status="needs_input",
    )
    prompt = await agent._build_system_prompt(dummy_turn_context())
    system_end = prompt.index("</system_prompt>")

    assert prompt.index("<plan_workflow>") < system_end
    assert "<current_plan" not in prompt[:system_end]
    assert "Implement &lt;plan&gt; support" not in prompt[:system_end]
    assert prompt.index("<available_tools>") > system_end


@pytest.mark.asyncio
async def test_plan_plugin_renders_without_current_plan():
    plugin = PlanAgentPlugin()
    create_test_agent(plugins=[plugin])

    section = await plugin.get_system_prompt_section(None)

    assert "<plan_workflow>" in section
    assert "PlanCreate" in section
    assert "complex but clear" in section
    assert "multiple sources, calculations, external/current data" in section
    assert "status needs_input" in section
    assert "<current_plan" not in section


def test_plan_render_hides_breakdown_once_in_progress():
    plan = _Plan(objective="Ship feature", breakdown=["Step one", "Step two"], status="approved")

    rendered = _render_current_plan(plan)
    assert "<breakdown>" in rendered
    assert "- Step one" in rendered

    plan.status = "in_progress"
    rendered = _render_current_plan(plan)
    assert "<breakdown>" not in rendered
    assert "Step one" not in rendered


class _FakeTurnContext:
    def __init__(self, chat_id: str = "chat") -> None:
        self.chat_id = chat_id
        self.ephemeral: dict[str, dict] = {}

    def set_ephemeral_message(self, key: str, message: dict) -> None:
        self.ephemeral[key] = message

    def clear_ephemeral_message(self, key: str) -> None:
        self.ephemeral.pop(key, None)


@pytest.mark.asyncio
async def test_needs_input_plan_injects_end_turn_nudge():
    plans = {"chat": _Plan(objective="Ship", status="needs_input", open_questions=["Which DB?"])}
    interceptor = PlanContextInterceptor(plans)
    context = _FakeTurnContext()

    await interceptor.intercept("before_llm", context)

    assert "plan.current_plan" in context.ephemeral
    nudge = context.ephemeral["plan.needs_input"]["content"]
    assert "<plan_needs_input>" in nudge
    assert "End the turn now" in nudge

    # Status moves on: the nudge is cleared, the plan context stays.
    plans["chat"].status = "in_progress"
    await interceptor.intercept("before_llm", context)
    assert "plan.needs_input" not in context.ephemeral
    assert "plan.current_plan" in context.ephemeral

    # Terminal status clears both.
    plans["chat"].status = "verified"
    await interceptor.intercept("before_llm", context)
    assert context.ephemeral == {}


class _SinkCapture:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_plan_event_interceptor_emits_on_change_and_clear():
    plans: dict[str, _Plan] = {}
    interceptor = PlanEventInterceptor(plans)
    sink = _SinkCapture()
    context = SimpleNamespace(chat_id="chat", turn_id="turn", agent_name="agent", event_sink=sink)

    # No plan and nothing previously emitted: no event.
    await interceptor.intercept("after_tool", context)
    assert sink.events == []

    plans["chat"] = _Plan(objective="Ship feature")
    await interceptor.intercept("after_tool", context)
    assert len(sink.events) == 1
    assert sink.events[0].event_type == "plan"
    assert sink.events[0].detail == "plan_state"
    assert sink.events[0].metadata["plan"]["objective"] == "Ship feature"

    # Unchanged plan: no re-emit.
    await interceptor.intercept("final_response", context)
    assert len(sink.events) == 1

    # Updated plan: re-emit.
    plans["chat"].updated_at += 1
    await interceptor.intercept("after_tool", context)
    assert len(sink.events) == 2

    # Cleared plan: emit a None payload once.
    del plans["chat"]
    await interceptor.intercept("after_tool", context)
    assert len(sink.events) == 3
    assert sink.events[-1].metadata["plan"] is None
    await interceptor.intercept("final_response", context)
    assert len(sink.events) == 3


@pytest.mark.asyncio
async def test_plan_event_interceptor_ignores_non_emitting_stages():
    plans = {"chat": _Plan(objective="Ship feature")}
    interceptor = PlanEventInterceptor(plans)
    sink = _SinkCapture()
    context = SimpleNamespace(chat_id="chat", turn_id="turn", agent_name="agent", event_sink=sink)

    await interceptor.intercept("prepare", context)
    await interceptor.intercept("before_llm", context)
    assert sink.events == []


def test_default_agent_enables_plan_plugin_before_task_plugin():
    plugins = default_agent_spec["plugins"]["enabled"]

    assert "PlanPlugin" in plugins
    assert plugins.index("MemoryPlugin") < plugins.index("PlanPlugin") < plugins.index("TaskPlugin")


@pytest.mark.asyncio
async def test_plan_plugin_auto_triggers_current_plan_for_complex_request():
    provider_name = "test_plan_auto_complex"
    captured: dict[str, object] = {}

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        captured["messages"] = messages
        return LLMResponse(content="ok")

    try:
        plugin = PlanAgentPlugin()
        agent = create_test_agent(
            model=f"{provider_name}/model",
            plugins=[plugin],
            tools=["PlanCreate", "PlanUpdate", "PlanGet"],
        )

        await agent.ask(
            "complex-plan-chat",
            (
                "Cross-reference the latest USDA crop production report's corn yield estimates with current "
                "CBOT corn futures. Calculate the implied volatility of at-the-money options expiring next "
                "month, and output a summary matrix."
            ),
        )

        assert [message["role"] for message in captured["messages"]].count("system") == 1
        system_prompt = captured["messages"][0]["content"]
        assert "<plan_workflow>" in system_prompt
        assert "<current_plan" not in system_prompt
        plan_context = captured["messages"][-1]["content"]
        assert captured["messages"][-1]["role"] == "user"
        assert '<current_plan status="in_progress">' in plan_context
        assert "Plan auto-triggered because the request appears complex." in plan_context
        assert "Before doing substantive work, update the plan" in plan_context
        assert "_ephemeral_key" not in captured["messages"][-1]
        assert not any("<plan_trigger>" in message.get("content", "") for message in captured["messages"])
        plan = json.loads(await agent._invoke_tool("PlanGet", chat_id="complex-plan-chat"))["plan"]
        assert plan["status"] == "in_progress"
        assert "Cross-reference the latest USDA" in plan["objective"]
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_plan_plugin_does_not_auto_trigger_for_simple_request():
    provider_name = "test_plan_auto_simple"

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content="ok")

    try:
        plugin = PlanAgentPlugin()
        agent = create_test_agent(
            model=f"{provider_name}/model",
            plugins=[plugin],
            tools=["PlanCreate", "PlanUpdate", "PlanGet"],
        )

        await agent.ask("simple-plan-chat", "Say hello.")

        assert json.loads(await agent._invoke_tool("PlanGet", chat_id="simple-plan-chat")) == {"plan": None}
    finally:
        ep_provider._extensions.pop(provider_name, None)
