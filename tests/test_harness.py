import logging
import uuid

import pytest

from bos.core import AgentHarness, LLMResponse, ToolCallRequest, ep_agent, ep_provider
from bos.core.agent import ChainReactInterceptor, ReactAgent
from bos.core.contract import SkillMeta
from bos.core.defaults import FileSystemSkillsLoader, InMemMemoryExtension, InMemMessageStore, NaiveConsolidator
from bos.core.registry import ToolRegistry
from bos.extensions.mailboxes import jsonl_mailbox  # noqa: F401


def create_test_agent(**kwargs):
    kwargs.setdefault("message_store", InMemMessageStore())
    kwargs.setdefault("memory", InMemMemoryExtension())
    kwargs.setdefault("consolidator", NaiveConsolidator())
    kwargs.setdefault("skills_loader", FileSystemSkillsLoader())
    kwargs.setdefault("interceptor", ChainReactInterceptor())
    return ReactAgent(**kwargs)


def test_react_agent_local_tools_describe_ask_subagent(caplog):
    local_tools = ToolRegistry("test tools")

    with caplog.at_level(logging.WARNING):
        agent = create_test_agent(local_tools=local_tools)

    assert local_tools.get("AskSubagent") is not None
    ask_subagent = agent._local_tools.get("AskSubagent")
    assert ask_subagent.description.lstrip().startswith("Delegate a task to a named subagent and return its response.")
    assert not any("Tool AskSubagent is missing description" in record.message for record in caplog.records)

    schema = agent._local_tools.to_openai_schema()["AskSubagent"]
    assert schema["function"]["description"] == ask_subagent.description
    properties = schema["function"]["parameters"]["properties"]
    assert set(properties) == {"role", "message"}
    assert schema["function"]["parameters"]["required"] == ["role", "message"]
    assert agent._local_tools.get("ListAgents") is None
    assert agent._local_tools.get("SearchSkills") is None



@pytest.mark.asyncio
async def test_harness_create_agent_defaults_to_no_capabilities(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        agent = harness.create_agent()

        assert agent._tools == []
        assert agent._skills == []
        assert agent._maxims == {}
        assert agent._subagents == []
        assert agent._get_tool_defs() == []


@pytest.mark.asyncio
async def test_registered_agent_defaults_to_no_capabilities(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    agent_name = f"locked_{uuid.uuid4().hex}"

    try:
        ReactAgent.register(name=agent_name, description="Locked", system_prompt="Stay locked down.")

        async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
            agent = harness.create_agent(agent_name)

            assert agent._tools == []
            assert agent._skills == []
            assert agent._maxims == {}
            assert agent._subagents == []
            assert agent._get_tool_defs() == []
    finally:
        ep_agent._extensions.pop(agent_name, None)


@pytest.mark.asyncio
async def test_registered_agent_star_capabilities_enable_all(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    agent_name = f"open_{uuid.uuid4().hex}"

    try:
        ReactAgent.register(
            name=agent_name,
            description="Open",
            system_prompt="Use everything.",
            tools="*",
            skills="*",
            maxims="*",
            subagents="*",
        )

        async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
            agent = harness.create_agent(agent_name)
            tool_names = {tool_def["function"]["name"] for tool_def in agent._get_tool_defs()}

            assert agent._tools is None
            assert agent._skills is None
            assert agent._maxims.keys() == {"user", "soul", "identity", "rules"}
            assert agent._subagents is None
            assert {"AskSubagent", "LoadSkill", "Remember"} <= tool_names
            assert "ListAgents" not in tool_names
            assert "SearchSkills" not in tool_names
            assert "UnloadSkill" not in tool_names
    finally:
        ep_agent._extensions.pop(agent_name, None)


def test_registered_agent_rejects_unknown_capability_string():
    agent_name = f"bad_caps_{uuid.uuid4().hex}"

    with pytest.raises(TypeError, match="tools must be a list, '\\*', or None"):
        ReactAgent.register(name=agent_name, tools="all")


@pytest.mark.asyncio
async def test_react_agent_returns_placeholder_for_empty_model_response():
    suffix = uuid.uuid4().hex
    provider_name = f"test_empty_response_provider_{suffix}"

    @ep_provider(name=provider_name)
    async def empty_provider(messages, model=None, **kwargs):
        return LLMResponse(content=None)

    try:
        agent = create_test_agent(model=f"{provider_name}/empty")
        result = await agent.ask("empty-response-chat", "Say something.")

        assert result == "(empty model response)"
    finally:
        ep_provider._extensions.pop(provider_name, None)


def test_react_agent_rejects_dict_system_prompt():
    with pytest.raises(TypeError, match="system_prompt must be a string or None"):
        create_test_agent(system_prompt={"_default": "nope"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_skills_render_metadata_in_prompt_and_load_full_skill_body(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "youtube-searcher"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """
---
name: youtube-searcher-display-name
description: Search YouTube.
---
Use this skill to search YouTube.
""".lstrip(),
        encoding="utf-8",
    )

    async with AgentHarness(
        bos_dir=bos_dir,
        workspace=tmp_path,
        skills_loader={"skill_dirs": [skills_dir]},
    ) as harness:
        agent = harness.create_agent(
            agent_cfg={
                "system_prompt": "You are a helpful assistant.",
                "tools": ["LoadSkill"],
                "skills": None,
            }
        )
        skills_prompt = await agent._prompt_section_skills()
        skill_metas = await harness.skills_loader.search_skills("YouTube")
        load_result = await agent._local_tools.invoke_async("LoadSkill", {"name": "youtube-searcher"})

    assert "* **youtube-searcher**" in skills_prompt
    assert "* **youtube-searcher-display-name**" not in skills_prompt
    assert "```\nSearch YouTube.\n```" in skills_prompt
    assert str(skill_file) not in skills_prompt
    assert skill_metas["youtube-searcher"].name == "youtube-searcher"
    assert skill_metas["youtube-searcher"].description == "Search YouTube."
    assert load_result == skill_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_memories_render_with_shared_prompt_section_format():
    agent = create_test_agent(memory=InMemMemoryExtension(), maxims={"user": "Prefers concise answers."})

    maxims_prompt = await agent._prompt_section_maxims()

    assert maxims_prompt == "--- MAXIMS ---\n\n* **user** (your knowledge about the user — preferences, background, projects, style)\n```\nPrefers concise answers.\n```\n\n"


@pytest.mark.asyncio
async def test_prompt_sections_render_first_50_items_and_warn(caplog):
    local_tools = ToolRegistry("many test tools")
    tool_names = [f"Tool{i:03}" for i in range(51)]

    for i, tool_name in enumerate(tool_names):
        local_tools(
            name=tool_name,
            description=f"Tool description {i:03}",
            parameters={"type": "object", "properties": {}, "required": []},
        )(lambda: "ok")

    class StaticSkillsLoader:
        async def load_skill(self, name: str) -> str:
            return name

        async def search_skills(self, query: str | None = None) -> dict[str, SkillMeta]:
            return {
                f"skill_{i:03}": SkillMeta(
                    location=f"/skills/skill_{i:03}/SKILL.md",
                    description=f"Skill description {i:03}",
                )
                for i in range(51)
            }

    subagent_names = [f"prompt_cap_agent_{uuid.uuid4().hex}_{i:03}" for i in range(51)]
    try:
        for i, name in enumerate(subagent_names):
            ReactAgent.register(name=name, description=f"Subagent description {i:03}", tools=[])

        agent = create_test_agent(
            local_tools=local_tools,
            tools=tool_names,
            skills_loader=StaticSkillsLoader(),
            subagents=subagent_names,
        )

        with caplog.at_level(logging.WARNING):
            tools_prompt = await agent._prompt_section_tools()
            skills_prompt = await agent._prompt_section_skills()
            subagents_prompt = await agent._prompt_section_subagents()

        assert "* **Tool049**" in tools_prompt
        assert "* **Tool050**" not in tools_prompt
        assert "* **skill_049**" in skills_prompt
        assert "* **skill_050**" not in skills_prompt
        assert f"* **{subagent_names[49]}**" in subagents_prompt
        assert f"* **{subagent_names[50]}**" not in subagents_prompt
        assert "first 50 tools" in caplog.text
        assert "first 50 skills" in caplog.text
        assert "first 50 subagents" in caplog.text
    finally:
        for name in subagent_names:
            ep_agent._extensions.pop(name, None)


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
        agent = create_test_agent(model=f"{provider_name}/plain", system_prompt="Reply plainly.")
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
        agent = create_test_agent(
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
                            "role": researcher_name,
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
            tools=["AskSubagent"],
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
            subagent_defaults={
                "task_template": "--- Sub-agent Instructions ---\n{task}",
            },
        ) as harness:
            manager = harness.create_agent(manager_name)
            subagents_prompt = await manager._prompt_section_subagents()
            result = await manager.ask("parent-chat", "Explain the orchestration pattern.")

            chats = await harness.message_store.list_chats()

        assert researcher_name in subagents_prompt
        assert "Researcher" in subagents_prompt
        assert result == "Manager synthesized: Researcher says BOS delegates to named specialists via AskSubagent."
        assert "parent-chat" in chats
        child_chats = [chat for chat in chats if chat != "parent-chat"]
        assert len(child_chats) == 1
        assert child_chats[0].startswith("parent-chat~researcher")
    finally:
        ep_provider._extensions.pop(provider_name, None)
        ep_agent._extensions.pop(manager_name, None)
        ep_agent._extensions.pop(researcher_name, None)


@pytest.mark.asyncio
async def test_ask_subagent_rejects_disallowed_registered_agent(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"test_subagent_filter_provider_{suffix}"
    manager_name = f"manager_{suffix}"
    allowed_name = f"allowed_{suffix}"
    blocked_name = f"blocked_{suffix}"

    @ep_provider(name=provider_name)
    async def scripted_provider(messages, model=None, **kwargs):
        if model == "manager":
            tool_messages = [message for message in messages if message.get("role") == "tool"]
            if tool_messages:
                return LLMResponse(content=tool_messages[-1]["content"])
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_ask_subagent",
                        name="AskSubagent",
                        arguments={"role": blocked_name, "message": "Should be rejected."},
                    )
                ],
            )
        return LLMResponse(content="blocked response")

    try:
        ReactAgent.register(
            name=manager_name,
            description="Manager",
            model=f"{provider_name}/manager",
            tools=["AskSubagent"],
            subagents=[allowed_name],
        )
        ReactAgent.register(
            name=allowed_name,
            description="Allowed",
            model=f"{provider_name}/allowed",
            tools=[],
        )
        ReactAgent.register(
            name=blocked_name,
            description="Blocked",
            model=f"{provider_name}/blocked",
            tools=[],
        )

        bos_dir = tmp_path / ".bos"
        bos_dir.mkdir()
        async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
            manager = harness.create_agent(manager_name)
            result = await manager.ask("parent-chat", "Try the blocked subagent.")
            chats = await harness.message_store.list_chats()

        assert result == f"Error: Agent '{blocked_name}' is not an allowed subagent."
        assert list(chats) == ["parent-chat"]
    finally:
        ep_provider._extensions.pop(provider_name, None)
        ep_agent._extensions.pop(manager_name, None)
        ep_agent._extensions.pop(allowed_name, None)
        ep_agent._extensions.pop(blocked_name, None)
