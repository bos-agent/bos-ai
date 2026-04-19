import logging
import uuid

import pytest

from bos.core import AgentHarness, LLMResponse, ToolCallRequest, ep_agent, ep_provider
from bos.core.agent import ReactAgent
from bos.extensions.mailboxes import jsonl_mailbox  # noqa: F401


def test_harness_local_tools_describe_ask_subagent(caplog):
    harness = AgentHarness()

    with caplog.at_level(logging.WARNING):
        tools = harness._create_local_tools()

    ask_subagent = tools.get("AskSubagent")
    assert ask_subagent.description.lstrip().startswith("Delegate a task to a named subagent and return its response.")
    assert not any("Tool AskSubagent is missing description" in record.message for record in caplog.records)

    schema = tools.to_openai_schema()["AskSubagent"]
    assert schema["function"]["description"] == ask_subagent.description


def test_harness_rejects_unknown_capability_mode():
    with pytest.raises(ValueError, match="capability_mode must be 'defensive' or 'offensive'"):
        AgentHarness(capability_mode="sandbox")


@pytest.mark.asyncio
async def test_harness_send_mail_falls_back_to_agent_address(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(mail_route={"name": "JsonlMailRoute", "store_dir": tmp_path}, bos_dir=bos_dir) as harness:
        receiver = harness.mail_route.bind("bob")
        await receiver.receive_nowait()

        agent = harness.create_agent()
        result = await agent._local_tools.invoke_async("SendMail", {"recipient": "bob", "content": "hello"})

        assert result == "(Sent to bob)"

        message = await receiver.receive_nowait()
        assert message is not None
        assert message.sender == "agent@__unknown__"
        assert message.content == "hello"


@pytest.mark.asyncio
async def test_harness_create_agent_defaults_to_defensive_mode(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        agent = harness.create_agent()

        assert agent._tools == []
        assert agent._skills == []
        assert agent._memories == []
        assert agent._subagents == []
        assert agent._get_tool_defs() == []


@pytest.mark.asyncio
async def test_harness_create_agent_offensive_mode_enables_all_capabilities(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path, capability_mode="offensive") as harness:
        agent = harness.create_agent()
        tool_names = {tool_def["function"]["name"] for tool_def in agent._get_tool_defs()}

        assert agent._tools is None
        assert agent._skills is None
        assert agent._memories is None
        assert agent._subagents is None
        assert {"SendMail", "AskSubagent", "LoadSkill", "UnloadSkill", "SearchSkills", "ListAgents"} <= tool_names


@pytest.mark.asyncio
async def test_react_agent_returns_placeholder_for_empty_model_response():
    suffix = uuid.uuid4().hex
    provider_name = f"test_empty_response_provider_{suffix}"

    @ep_provider(name=provider_name)
    async def empty_provider(messages, model=None, **kwargs):
        return LLMResponse(content=None)

    try:
        agent = ReactAgent(model=f"{provider_name}/empty")
        result = await agent.ask("empty-response-conversation", "Say something.")

        assert result == "(empty model response)"
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_harness_ask_subagent_delegates_to_named_specialist(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"test_subagent_provider_{suffix}"
    manager_name = f"manager_{suffix}"
    researcher_name = f"researcher_{suffix}"

    @ep_provider(name=provider_name)
    async def scripted_provider(messages, model=None, **kwargs):
        if model == "manager":
            tool_messages = [message for message in messages if message.get("role") == "tool"]
            if tool_messages:
                return LLMResponse(content=f"Manager synthesized: {tool_messages[-1]['content']}")
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_ask_subagent",
                        name="AskSubagent",
                        arguments={
                            "agent_name": researcher_name,
                            "conversation_id": "parent-conversation_researcher",
                            "message": "Summarize BOS subagent orchestration in one line.",
                            "ref_conversation_id": "parent-conversation",
                        },
                    )
                ],
            )

        assert model == "researcher"
        assert any(
            "Sub-agent Instructions" in str(message.get("content", ""))
            for message in messages
            if message.get("role") == "user"
        )
        return LLMResponse(content="Researcher says BOS delegates to named specialists via AskSubagent.")

    try:
        ReactAgent.register(
            name=manager_name,
            description="Manager",
            model=f"{provider_name}/manager",
            tools=["AskSubagent", "ListAgents"],
            subagents=[researcher_name],
            system_prompt={"_default": "Delegate focused work to the researcher when useful."},
        )
        ReactAgent.register(
            name=researcher_name,
            description="Researcher",
            model=f"{provider_name}/researcher",
            tools=[],
            system_prompt={"_default": "Return concise delegated research findings."},
        )

        bos_dir = tmp_path / ".bos"
        bos_dir.mkdir()
        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
            subagents=[
                {
                    "name": "_default",
                    "task_template": "--- Sub-agent Instructions ---\n{task}",
                }
            ],
        ) as harness:
            manager = harness.create_agent(manager_name)
            listed_agents = await manager._local_tools.invoke_async("ListAgents", {})
            result = await manager.ask("parent-conversation", "Explain the orchestration pattern.")

            conversations = await harness.message_store.list_conversations()

        assert researcher_name in listed_agents
        assert "Researcher" in listed_agents
        assert result == "Manager synthesized: Researcher says BOS delegates to named specialists via AskSubagent."
        assert "parent-conversation" in conversations
        assert "parent-conversation_researcher" in conversations
    finally:
        ep_provider._extensions.pop(provider_name, None)
        ep_agent._extensions.pop(manager_name, None)
        ep_agent._extensions.pop(researcher_name, None)
