import json
import logging
import uuid

import pytest

from bos.core import AgentHarness, LLMResponse, ToolCallRequest, ep_agent, ep_provider
from bos.core.agent import ReactAgent
from bos.core.registry import ToolRegistry
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
    properties = schema["function"]["parameters"]["properties"]
    assert set(properties) == {"agent_name", "message"}
    assert schema["function"]["parameters"]["required"] == ["agent_name", "message"]


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
        assert {"SendMail", "AskSubagent", "LoadSkill", "SearchSkills", "ListAgents"} <= tool_names
        assert "UnloadSkill" not in tool_names


@pytest.mark.asyncio
async def test_react_agent_returns_placeholder_for_empty_model_response():
    suffix = uuid.uuid4().hex
    provider_name = f"test_empty_response_provider_{suffix}"

    @ep_provider(name=provider_name)
    async def empty_provider(messages, model=None, **kwargs):
        return LLMResponse(content=None)

    try:
        agent = ReactAgent(model=f"{provider_name}/empty")
        result = await agent.ask("empty-response-chat", "Say something.")

        assert result == "(empty model response)"
    finally:
        ep_provider._extensions.pop(provider_name, None)


def test_react_agent_rejects_dict_system_prompt():
    with pytest.raises(TypeError, match="system_prompt must be a string or None"):
        ReactAgent(system_prompt={"_default": "nope"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_skill_tools_return_metadata_and_full_skill_body(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "youtube-searcher"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """
---
name: youtube-searcher
description: Search YouTube.
---
Use this skill to search YouTube.
""".lstrip(),
        encoding="utf-8",
    )

    async with AgentHarness(
        bos_dir=bos_dir,
        workspace=tmp_path,
        capability_mode="offensive",
        skills_loader={"skill_dirs": [skills_dir]},
    ) as harness:
        agent = harness.create_agent()
        search_result = await agent._local_tools.invoke_async("SearchSkills", {"query": "youtube"})
        load_result = await agent._local_tools.invoke_async("LoadSkill", {"name": "youtube-searcher"})

    assert json.loads(search_result) == [
        {
            "name": "youtube-searcher",
            "location": str(skill_file),
            "summary": "name: youtube-searcher\ndescription: Search YouTube.",
        }
    ]
    assert load_result == skill_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_react_agent_first_turn_passes_only_user_text():
    suffix = uuid.uuid4().hex
    provider_name = f"test_plain_input_provider_{suffix}"
    captured: dict[str, object] = {}

    @ep_provider(name=provider_name)
    async def capture_provider(messages, model=None, **kwargs):
        captured["messages"] = messages
        return LLMResponse(content="ok")

    try:
        agent = ReactAgent(model=f"{provider_name}/plain", system_prompt="Reply plainly.")
        result = await agent.ask("thread-123", "Hello.")

        user_messages = [message for message in captured["messages"] if message.get("role") == "user"]

        assert result == "ok"
        assert len(user_messages) == 1
        assert user_messages[0]["content"] == "Hello."
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_react_agent_injects_runtime_tool_context():
    suffix = uuid.uuid4().hex
    provider_name = f"test_tool_context_provider_{suffix}"
    tools = ToolRegistry("test tools")

    @tools(
        name="EchoWithContext",
        description="Echo the user text plus injected runtime identifiers.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    def echo_with_context(text: str, chat_id: str, turn_id: str) -> dict[str, str]:
        return {"text": text, "chat_id": chat_id, "turn_id": turn_id}

    @ep_provider(name=provider_name)
    async def tool_context_provider(messages, model=None, **kwargs):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if tool_messages:
            return LLMResponse(content=tool_messages[-1]["content"])
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_echo_with_context",
                    name="EchoWithContext",
                    arguments={"text": "hello"},
                )
            ],
        )

    try:
        agent = ReactAgent(
            model=f"{provider_name}/tool-context",
            local_tools=tools,
            tools=["EchoWithContext"],
        )
        result = await agent.ask("tool-context-chat", "Use the tool.")

        assert '"text": "hello"' in result
        assert '"chat_id": "tool-context-chat"' in result
        assert '"turn_id": "' in result
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_harness_passes_tool_config_to_agent_tools(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"test_tool_config_provider_{suffix}"
    tools = ToolRegistry("test tools")

    @tools(
        name="EchoWithConfig",
        description="Echo the user text plus injected tool configuration.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    def echo_with_config(text: str, tool_config: dict) -> dict:
        return {"text": text, "tool_config": tool_config}

    @ep_provider(name=provider_name)
    async def tool_config_provider(messages, model=None, **kwargs):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if tool_messages:
            return LLMResponse(content=tool_messages[-1]["content"])
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_echo_with_config",
                    name="EchoWithConfig",
                    arguments={"text": "from model", "tool_config": {"mode": "from-model"}},
                )
            ],
        )

    try:
        bos_dir = tmp_path / ".bos"
        bos_dir.mkdir()
        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
            tools={"EchoWithConfig": {"mode": "strict", "timeout_seconds": 15}},
        ) as harness:
            agent = harness.create_agent(
                agent_cfg={
                    "model": f"{provider_name}/tool-config",
                    "tools": ["EchoWithConfig"],
                }
            )
            agent._local_tools.register(tools.get("EchoWithConfig"))
            result = await agent.ask("tool-config-chat", "Use the tool.")

        assert '"text": "from model"' in result
        assert '"mode": "strict"' in result
        assert '"timeout_seconds": 15' in result
        assert "from-model" not in result
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
                            "message": "Summarize BOS subagent orchestration in one line.",
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
            system_prompt="Delegate focused work to the researcher when useful.",
        )
        ReactAgent.register(
            name=researcher_name,
            description="Researcher",
            model=f"{provider_name}/researcher",
            tools=[],
            system_prompt="Return concise delegated research findings.",
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
            result = await manager.ask("parent-chat", "Explain the orchestration pattern.")

            chats = await harness.message_store.list_chats()

        assert researcher_name in listed_agents
        assert "Researcher" in listed_agents
        assert result == "Manager synthesized: Researcher says BOS delegates to named specialists via AskSubagent."
        assert "parent-chat" in chats
        child_chats = [chat for chat in chats if chat != "parent-chat"]
        assert len(child_chats) == 1
        assert child_chats[0].startswith("parent-chat~researcher")
    finally:
        ep_provider._extensions.pop(provider_name, None)
        ep_agent._extensions.pop(manager_name, None)
        ep_agent._extensions.pop(researcher_name, None)
