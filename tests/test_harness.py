import logging
import uuid

import pytest
from conftest import CloseTrackingConsolidator, InMemMemoryExtension, InMemMessageStore, MessageOnlyConsolidator

from bos.config.workspace import Workspace
from bos.core import AgentHarness, LLMResponse, Message, ToolCallRequest, bootstrap_platform, ep_agent, ep_provider
from bos.core.agent import ChainReactInterceptor, ReactAgent
from bos.core.contract import SkillMeta
from bos.core.defaults.agent_spec import bos_maxims as default_maxims
from bos.core.defaults.skills_loader import FileSystemSkillsLoader
from bos.core.history import HistoryProjection
from bos.core.registry import ToolRegistry


def create_test_agent(**kwargs):
    kwargs.setdefault("message_store", InMemMessageStore())
    kwargs.setdefault("memory", InMemMemoryExtension())
    kwargs.setdefault("consolidator", MessageOnlyConsolidator())
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

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path, consolidator=MessageOnlyConsolidator()) as harness:
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

        async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path, consolidator=MessageOnlyConsolidator()) as harness:
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

        async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path, consolidator=MessageOnlyConsolidator()) as harness:
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
        consolidator=MessageOnlyConsolidator(),
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

    assert "## youtube-searcher" in skills_prompt
    assert "youtube-searcher-display-name" not in skills_prompt
    assert "Search YouTube." in skills_prompt
    assert str(skill_file) not in skills_prompt
    assert skill_metas["youtube-searcher"].name == "youtube-searcher"
    assert skill_metas["youtube-searcher"].description == "Search YouTube."
    assert load_result == skill_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_memories_render_with_shared_prompt_section_format():
    agent = create_test_agent(
        memory=InMemMemoryExtension(user="Prefers concise answers."),
        maxims={"user": default_maxims["user"]},
    )

    maxims_prompt = await agent._prompt_section_maxims()

    assert "<active_maxims>" in maxims_prompt
    assert "your knowledge about the user" in maxims_prompt
    assert "Prefers concise answers." in maxims_prompt


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

        assert "## Tool049" in tools_prompt
        assert "Tool050" not in tools_prompt
        assert "## skill_049" in skills_prompt
        assert "skill_050" not in skills_prompt
        assert f"## {subagent_names[49]}" in subagents_prompt
        assert subagent_names[50] not in subagents_prompt
        assert "first 50 tools" in caplog.text
        assert "first 50 skills" in caplog.text
        assert "first 50 subagents" in caplog.text
    finally:
        for name in subagent_names:
            ep_agent._extensions.pop(name, None)


@pytest.mark.asyncio
async def test_tools_usage_overrides_tool_description_in_prompt():
    local_tools = ToolRegistry("test tools")

    @local_tools(
        name="FetchURL",
        description="Fetch content from a URL.",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    async def fetch_url(url: str) -> str:
        return "ok"

    @local_tools(
        name="RunBash",
        description="Run a bash command.",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
    )
    async def run_bash(cmd: str) -> str:
        return "ok"

    agent = create_test_agent(
        local_tools=local_tools,
        tools=["FetchURL", "RunBash"],
        tools_usage={"FetchURL": "Custom fetch usage for this agent."},
    )

    prompt = await agent._prompt_section_tools()

    assert "Custom fetch usage for this agent." in prompt
    assert "Fetch content from a URL." not in prompt
    assert "Run a bash command." in prompt


@pytest.mark.asyncio
async def test_tools_usage_default_none_keeps_original_descriptions():
    local_tools = ToolRegistry("test tools")

    @local_tools(
        name="FetchURL",
        description="Fetch content from a URL.",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    async def fetch_url(url: str) -> str:
        return "ok"

    agent = create_test_agent(local_tools=local_tools, tools=["FetchURL"])

    prompt = await agent._prompt_section_tools()

    assert "Fetch content from a URL." in prompt


@pytest.mark.asyncio
async def test_tools_usage_empty_dict_keeps_all_original_descriptions():
    local_tools = ToolRegistry("test tools")

    @local_tools(
        name="FetchURL",
        description="Fetch content from a URL.",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    async def fetch_url(url: str) -> str:
        return "ok"

    agent = create_test_agent(local_tools=local_tools, tools=["FetchURL"], tools_usage={})
    prompt = await agent._prompt_section_tools()

    assert "Fetch content from a URL." in prompt


@pytest.mark.asyncio
async def test_tools_usage_flows_through_create_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path, consolidator=MessageOnlyConsolidator()) as harness:
        agent = harness.create_agent(
            agent_cfg={
                "system_prompt": "You are a helpful assistant.",
                "tools": ["LoadSkill"],
                "tools_usage": {"LoadSkill": "Custom usage for LoadSkill."},
            }
        )
        prompt = await agent._prompt_section_tools()

    assert "Custom usage for LoadSkill." in prompt
    assert "Load a skill" not in prompt


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
            consolidator=MessageOnlyConsolidator(),
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
            consolidator=MessageOnlyConsolidator(),
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
        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
            consolidator=MessageOnlyConsolidator(),
        ) as harness:
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


@pytest.mark.asyncio
async def test_harness_consolidator_model_precedence(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    monkeypatch.setenv("BOS_CONSOLIDATOR_MODEL", "env/consolidator")
    monkeypatch.setenv("BOS_MODEL", "env/base")
    async with AgentHarness(
        bos_dir=bos_dir,
        workspace=tmp_path,
        consolidator={"model": "explicit/consolidator"},
    ) as harness:
        assert harness.consolidator._model == "explicit/consolidator"

    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL")
    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        assert harness.consolidator._model is None


@pytest.mark.asyncio
async def test_harness_uses_bos_consolidator_model_before_bos_model(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    monkeypatch.setenv("BOS_CONSOLIDATOR_MODEL", "env/consolidator")
    monkeypatch.setenv("BOS_MODEL", "env/base")

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        assert harness.consolidator._model == "env/consolidator"


@pytest.mark.asyncio
async def test_harness_allows_no_consolidator_model(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL", raising=False)
    monkeypatch.delenv("BOS_MODEL", raising=False)

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        assert harness.consolidator._model is None


def test_bootstrap_platform_does_not_require_consolidator_model(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL", raising=False)
    monkeypatch.delenv("BOS_MODEL", raising=False)

    bootstrap_platform(tmp_path / ".bos")


@pytest.mark.asyncio
async def test_platform_agent_defaults_model_is_not_consolidator_fallback(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[platform.agent_defaults]
model = "agent/default"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL", raising=False)
    monkeypatch.delenv("BOS_MODEL", raising=False)

    workspace = Workspace.from_discovery(tmp_path)
    async with workspace.harness() as harness:
        assert harness.consolidator._model is None


@pytest.mark.asyncio
async def test_harness_closes_custom_consolidator(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    consolidator = CloseTrackingConsolidator()

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path, consolidator=consolidator):
        pass

    assert consolidator.closed is True


@pytest.mark.asyncio
async def test_agent_history_budget_uses_resolved_turn_model(monkeypatch):
    suffix = uuid.uuid4().hex
    provider_name = f"test_budget_model_provider_{suffix}"
    seen_budget_models: list[str | None] = []

    def fake_estimate(messages, *, budget_model):
        seen_budget_models.append(budget_model)
        return HistoryProjection(messages=[], estimated_tokens=0, model=budget_model, source="fallback")

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content="ok")

    monkeypatch.setattr("bos.core.agent.estimate_message_history_tokens", fake_estimate)
    try:
        agent = create_test_agent(model=f"{provider_name}/base")
        result = await agent.ask("budget-model-chat", "Hello.", llm_args={"model": f"{provider_name}/override"})

        assert result == "ok"
        assert seen_budget_models == [f"{provider_name}/override"]
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_agent_auto_compaction_passes_message_objects(monkeypatch):
    store = InMemMessageStore()
    consolidator = MessageOnlyConsolidator()
    await store.save_messages(
        "compact-chat",
        [Message(llm_message={"role": "user", "content": "large history"})],
    )

    def fake_estimate(messages, *, budget_model):
        projected = [message.llm_message for message in messages]
        return HistoryProjection(messages=projected, estimated_tokens=999, model=budget_model, source="fallback")

    monkeypatch.setattr("bos.core.agent.estimate_message_history_tokens", fake_estimate)
    agent = create_test_agent(message_store=store, consolidator=consolidator, max_tokens=1)

    history = await agent._get_chat_history("compact-chat", budget_model="test/model")

    assert consolidator.calls
    assert all(isinstance(message, Message) for message in consolidator.calls[0][0])
    assert any(message.is_summary for message in store._messages["compact-chat"])
    assert history[-1]["content"].startswith("Chat summary:")
