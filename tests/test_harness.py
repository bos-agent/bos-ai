import asyncio
import logging
import uuid

import pytest
from conftest import (
    CloseTrackingConsolidator,
    InMemChatStore,
    InMemMemoryExtension,
    MessageOnlyConsolidator,
    create_test_agent,
)

import bos.extensions.tools.filesystem  # noqa: F401  — registers ep_tool entries
import bos.extensions.tools.knowledge  # noqa: F401
import bos.extensions.tools.system  # noqa: F401
from bos.config.default_agent_spec import default_agent_spec
from bos.config.workspace import Workspace
from bos.core import AgentHarness, AgentRegistry, LLMResponse, Message, ToolCallRequest, ep_provider
from bos.core.contract import ep_consolidator, ep_tool
from bos.core.registry import ToolRegistry
from bos.plugins.memory import MemoryAgentPlugin
from bos.plugins.skills import SkillMeta, SkillsAgentPlugin, SkillsHarnessPlugin
from bos.plugins.subagent import SubagentAgentPlugin, SubagentHarnessPlugin
from bos.plugins.task import TaskAgentPlugin


class _MockSubagentRuntime:
    async def ask(self, role, message, *, parent) -> str:
        return "mock response"


def test_react_agent_local_tools_describe_ask_subagent(caplog):
    local_tools = ToolRegistry("_test_tools")
    role = f"ask_subagent_tool_{uuid.uuid4().hex}"

    try:
        AgentRegistry.register(name=role, description="Available subagent", tools=[])
        subagent = SubagentAgentPlugin(_MockSubagentRuntime(), enabled=None, disabled=[])

        with caplog.at_level(logging.WARNING):
            agent = create_test_agent(local_tools=local_tools, plugins=[subagent])
    finally:
        AgentRegistry._registry.pop(role, None)

    assert local_tools.get("AskSubagent") is not None
    ask_subagent = agent._local_tools.get("AskSubagent")
    assert ask_subagent.description.lstrip().startswith("Delegate a task to a named subagent and return its response.")
    assert not any("Tool AskSubagent is missing description" in record.message for record in caplog.records)

    schema = agent._local_tools.to_openai_schema()["AskSubagent"]
    assert schema["function"]["description"] == ask_subagent.description
    properties = schema["function"]["parameters"]["properties"]
    assert set(properties) == {"role", "task"}
    assert schema["function"]["parameters"]["required"] == ["role", "task"]
    assert agent._local_tools.metadata_for("AskSubagent")["parallel_safe"] is True
    assert agent._local_tools.get("ListAgents") is None
    assert agent._local_tools.get("SearchSkills") is None


@pytest.mark.asyncio
async def test_subagent_plugin_hides_prompt_and_tool_when_no_subagents():
    snapshot = dict(AgentRegistry._registry)
    AgentRegistry._registry.clear()
    try:
        AgentRegistry.register(name="_default", description="Default agent", tools=[])
        local_tools = ToolRegistry("_test_tools")
        subagent = SubagentAgentPlugin(_MockSubagentRuntime(), enabled=None, disabled=[])

        agent = create_test_agent(local_tools=local_tools, plugins=[subagent])

        assert agent._local_tools.get("AskSubagent") is None
        assert await subagent.get_system_prompt_section(None) is None
        assert "AskSubagent" not in await agent._prompt_section_tools()
        assert "<subagent_workflow>" not in await agent._build_system_prompt()
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(snapshot)


@pytest.mark.asyncio
async def test_subagent_plugin_string_star_matches_list_star():
    role = f"string_star_subagent_{uuid.uuid4().hex}"
    provider = SubagentHarnessPlugin()
    provider._runtime = _MockSubagentRuntime()

    try:
        AgentRegistry.register(name=role, description="String star subagent", tools=[])
        provider.validate_config({"enabled": "*"})
        plugin = provider.bind({"enabled": "*"})
        agent = create_test_agent(plugins=[plugin])

        assert agent._local_tools.get("AskSubagent") is not None
        prompt_section = await plugin.get_system_prompt_section(None)
        assert prompt_section is not None
        assert "<subagent_workflow>" in prompt_section
        assert f'<agent role="{role}">String star subagent</agent>' in prompt_section
    finally:
        AgentRegistry._registry.pop(role, None)


@pytest.mark.asyncio
async def test_harness_binds_subagent_plugin_bindings_from_validated_config(tmp_path):
    role = f"configured_subagent_{uuid.uuid4().hex}"
    snapshot = dict(AgentRegistry._registry)
    AgentRegistry._registry.clear()
    try:
        ws = Workspace(
            tmp_path,
            tmp_path / ".bos",
            {
                "agent": {
                    "defaults": {
                        "plugin-bindings": {
                            "SubagentPlugin": {
                                "enabled": ["*"],
                            }
                        }
                    }
                },
                "agents": {
                    role: {
                        "system_prompt": "You are a configured helper.",
                    }
                },
            },
        )
        ws.bos_dir.mkdir(parents=True, exist_ok=True)
        ws.bootstrap_platform()

        async with ws.harness() as harness:
            agent = await harness.create_agent("_default")
            prompt = await agent._build_system_prompt()

        assert agent._local_tools.get("AskSubagent") is not None
        assert "<subagent_workflow>" in prompt
        assert f'<agent role="{role}"></agent>' in prompt
    finally:
        AgentRegistry._registry.clear()
        AgentRegistry._registry.update(snapshot)


@pytest.mark.asyncio
async def test_harness_create_agent_defaults_to_no_capabilities(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(
        bos_dir=bos_dir,
        workspace=tmp_path,
    ) as harness:
        agent = await harness.create_agent()

        assert agent._tools == []
        assert agent._get_tool_defs() == []


@pytest.mark.asyncio
async def test_agent_history_attribution_is_disabled_by_default():
    store = InMemChatStore()
    await store.commit_turn(
        "shared",
        [
            Message(
                llm_message={"role": "assistant", "content": "main says hi"},
                metadata={"agent_name": "main"},
            ),
        ],
        turn_id="turn-1",
    )
    agent = create_test_agent(agent_name="libai", chat_store=store)

    history = await agent._load_and_compact_history("shared", budget_model=None)

    assert history == [{"role": "assistant", "content": "main says hi"}]


@pytest.mark.asyncio
async def test_agent_history_formats_agent_attribution_when_enabled():
    store = InMemChatStore()
    await store.commit_turn(
        "shared",
        [
            Message(
                llm_message={"role": "user", "content": "hello"},
                metadata={"target_agent": "libai", "target_display": "Li Bai"},
            ),
            Message(
                llm_message={"role": "assistant", "content": "a poem"},
                metadata={"agent_name": "libai", "agent_display": "Li Bai"},
            ),
            Message(
                llm_message={"role": "assistant", "content": "main says hi"},
                metadata={"agent_name": "main"},
            ),
        ],
        turn_id="turn-1",
    )
    agent = create_test_agent(agent_name="libai", chat_store=store, history_attribution=True)

    history = await agent._load_and_compact_history("shared", budget_model=None)

    assert history[0]["content"] == "[user -> Li Bai]\nhello"
    assert history[1]["content"] == "[assistant: Li Bai]\na poem"
    assert history[1]["role"] == "assistant"
    assert history[2]["role"] == "user"
    assert history[2]["content"] == "[assistant main said]\nmain says hi"


@pytest.mark.asyncio
async def test_agent_history_attribution_reads_legacy_actor_metadata():
    store = InMemChatStore()
    await store.commit_turn(
        "shared",
        [
            Message(
                llm_message={"role": "assistant", "content": "legacy"},
                metadata={"actor": "main"},
            ),
        ],
        turn_id="turn-1",
    )
    agent = create_test_agent(agent_name="libai", chat_store=store, history_attribution=True)

    history = await agent._load_and_compact_history("shared", budget_model=None)

    assert history == [{"role": "user", "content": "[assistant main said]\nlegacy"}]


@pytest.mark.asyncio
async def test_agent_history_renders_workdir_even_without_actor_attribution():
    store = InMemChatStore()
    await store.commit_turn(
        "chat",
        [
            Message(
                llm_message={"role": "user", "content": "hello"},
                metadata={"target_agent": "main", "workdir": "/home/user/proj"},
            ),
            Message(
                llm_message={"role": "assistant", "content": "hi"},
                metadata={"agent_name": "main"},
            ),
        ],
        turn_id="turn-1",
    )
    agent = create_test_agent(agent_name="main", chat_store=store)

    history = await agent._load_and_compact_history("chat", budget_model=None)

    # workdir renders; actor labels stay suppressed when attribution is off
    assert history[0]["content"] == "[workdir: /home/user/proj]\nhello"
    assert history[1] == {"role": "assistant", "content": "hi"}


@pytest.mark.asyncio
async def test_agent_history_combines_target_and_workdir_labels():
    store = InMemChatStore()
    await store.commit_turn(
        "chat",
        [
            Message(
                llm_message={"role": "user", "content": "hello"},
                metadata={"target_agent": "libai", "target_display": "Li Bai", "workdir": "/home/user/proj"},
            ),
        ],
        turn_id="turn-1",
    )
    agent = create_test_agent(agent_name="libai", chat_store=store, history_attribution=True)

    history = await agent._load_and_compact_history("chat", budget_model=None)

    assert history[0]["content"] == "[user -> Li Bai | workdir: /home/user/proj]\nhello"


@pytest.mark.asyncio
async def test_agent_history_renders_workdir_only_when_it_changes():
    store = InMemChatStore()
    await store.commit_turn(
        "chat",
        [
            Message(llm_message={"role": "user", "content": "one"}, metadata={"workdir": "/proj/a"}),
            Message(llm_message={"role": "assistant", "content": "ok"}, metadata={"agent_name": "main"}),
        ],
        turn_id="turn-1",
    )
    await store.commit_turn(
        "chat",
        [
            Message(llm_message={"role": "user", "content": "two"}, metadata={"workdir": "/proj/a"}),
            Message(llm_message={"role": "assistant", "content": "ok"}, metadata={"agent_name": "main"}),
        ],
        turn_id="turn-2",
    )
    await store.commit_turn(
        "chat",
        [
            Message(llm_message={"role": "user", "content": "three"}, metadata={"workdir": "/proj/b"}),
            Message(llm_message={"role": "assistant", "content": "ok"}, metadata={"agent_name": "main"}),
        ],
        turn_id="turn-3",
    )
    agent = create_test_agent(agent_name="main", chat_store=store)

    history = await agent._load_and_compact_history("chat", budget_model=None)

    # first occurrence renders, the unchanged repeat is suppressed, the change renders
    assert history[0]["content"] == "[workdir: /proj/a]\none"
    assert history[2]["content"] == "two"
    assert history[4]["content"] == "[workdir: /proj/b]\nthree"


def test_current_turn_user_message_projection_renders_workdir():
    agent = create_test_agent(agent_name="main")
    message = Message(
        llm_message={"role": "user", "content": "hello"},
        metadata={"target_agent": "main", "workdir": "/home/user/proj"},
    )

    projected = agent._project_current_message(message)

    assert projected["content"] == "[workdir: /home/user/proj]\nhello"
    # stored message stays raw; the label is derived at projection time only
    assert message.llm_message["content"] == "hello"


def test_current_turn_projection_leaves_assistant_and_plain_messages_untouched():
    agent = create_test_agent(agent_name="main", history_attribution=True)

    assistant = Message(llm_message={"role": "assistant", "content": "hi"}, metadata={"agent_name": "main"})
    plain_user = Message(llm_message={"role": "user", "content": "hi"})

    assert agent._project_current_message(assistant) == assistant.llm_message
    assert agent._project_current_message(plain_user) == plain_user.llm_message


def test_agent_system_info_includes_workspace():
    agent = create_test_agent(workspace="/srv/agent-workspace")

    section = agent._prompt_section_system_info()

    assert "<workspace>/srv/agent-workspace</workspace>" in section


def test_agent_system_info_omits_workspace_when_unset():
    agent = create_test_agent()

    section = agent._prompt_section_system_info()

    assert "<workspace>" not in section


@pytest.mark.asyncio
async def test_registered_agent_defaults_to_no_capabilities(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    agent_name = f"locked_{uuid.uuid4().hex}"

    try:
        AgentRegistry.register(name=agent_name, description="Locked", system_prompt="Stay locked down.")

        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
        ) as harness:
            agent = await harness.create_agent(agent_name)

            assert agent._tools == []
            assert agent._get_tool_defs() == []
    finally:
        AgentRegistry._registry.pop(agent_name, None)


@pytest.mark.asyncio
async def test_registered_agent_star_capabilities_enable_all(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    agent_name = f"open_{uuid.uuid4().hex}"

    try:
        AgentRegistry.register(
            name=agent_name,
            description="Open",
            system_prompt="Use everything.",
            tools=None,
        )

        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
        ) as harness:
            agent = await harness.create_agent(agent_name)
            tool_names = {tool_def["function"]["name"] for tool_def in agent._get_tool_defs()}

            assert agent._tools is None
            assert "ListAgents" not in tool_names
            assert "SearchSkills" not in tool_names
    finally:
        AgentRegistry._registry.pop(agent_name, None)


def test_registered_agent_rejects_unknown_capability_string():
    agent_name = f"bad_caps_{uuid.uuid4().hex}"

    with pytest.raises(TypeError, match="tools must be a list or None"):
        AgentRegistry.register(name=agent_name, tools="all")


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


def test_default_system_prompt_uses_compact_xml_contract():
    prompt = default_agent_spec["system_prompt"]

    for tag in (
        "role",
        "behavior",
        "workflow",
        "edit_discipline",
        "verification",
        "communication",
        "final_response",
        "tool_discipline",
    ):
        assert f"<{tag}>" in prompt
        assert f"</{tag}>" in prompt

    assert "Always explain your reasoning before calling a tool" not in prompt
    assert "track progress\n  with the task tools" not in prompt
    assert "reserve subagents" not in prompt
    assert "Do not narrate hidden reasoning" in prompt
    assert "Verify meaningful code changes" in prompt
    assert "Do not claim a command, test, or check passed unless it was actually run" in prompt
    assert "never overwrite or discard changes you did not make" in prompt
    assert "State verification that was run and whether it passed" in prompt


def test_default_tools_usage_covers_core_agent_tools():
    expected_tools = {
        "Bash",
        "ReadFile",
        "WriteFile",
        "EditFile",
        "GlobSearch",
        "GrepSearch",
        "WebSearch",
        "WebFetch",
    }
    tools_usage = ep_tool.describe_usage()
    assert expected_tools <= tools_usage.keys()
    assert all(tools_usage[name].strip() for name in expected_tools)


def test_default_tools_usage_references_actual_bos_names_only():
    rendered = "\n".join(ep_tool.describe_usage().values())

    assert "TodoWrite" not in rendered
    assert "TodoRead" not in rendered
    assert "NotebookEdit" not in rendered
    assert "ReadFile" in rendered
    assert "EditFile" in rendered


def test_default_tools_usage_stays_compact():
    assert sum(len(text.split()) for text in ep_tool.describe_usage().values()) < 1800


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

    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    loader = FileSystemSkillsLoader(skill_dirs=[skills_dir])
    skills_plugin = SkillsAgentPlugin(loader, allow=None, exclude=[])

    agent = create_test_agent(
        system_prompt="You are a helpful assistant.",
        tools=["LoadSkill"],
        plugins=[skills_plugin],
    )
    skills_prompt = await skills_plugin.get_system_prompt_section(None)
    skill_metas = await loader.search_skills("YouTube")
    load_result = await agent._local_tools.invoke("LoadSkill", {"name": "youtube-searcher"})

    assert "<skills_workflow>" in skills_prompt
    assert "Use the exact name attribute from available_skills as the LoadSkill name." in skills_prompt
    assert '<skill name="youtube-searcher">Search YouTube.</skill>' in skills_prompt
    assert "youtube-searcher-display-name" not in skills_prompt
    assert "Search YouTube." in skills_prompt
    assert str(skill_file) not in skills_prompt
    assert skill_metas["youtube-searcher"].name == "youtube-searcher"
    assert skill_metas["youtube-searcher"].description == "Search YouTube."
    assert load_result == skill_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_builtin_python_skill_discovered_and_loadable(tmp_path):
    """The packaged builtin skills resolve via the __builtin__ sentinel."""
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    loader = FileSystemSkillsLoader(skill_dirs=["__builtin__"], bos_dir=tmp_path)
    metas = await loader.search_skills()

    assert "python" in metas
    assert "uv" in metas["python"].description
    body = await loader.load_skill("python")
    assert "/// script" in body
    assert "uv run" in body


@pytest.mark.asyncio
async def test_builtin_skill_creator_discovered_and_loadable(tmp_path):
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    loader = FileSystemSkillsLoader(skill_dirs=["__builtin__"], bos_dir=tmp_path)
    metas = await loader.search_skills()

    assert "skill-creator" in metas
    # the `>` block-scalar description folds to a single line for the system prompt
    assert "\n" not in metas["skill-creator"].description
    assert "not triggering" in metas["skill-creator"].description
    body = await loader.load_skill("skill-creator")
    assert "directory name is the skill's identity" in body
    assert "`LoadSkill` returns only the SKILL.md text" in body


@pytest.mark.asyncio
async def test_test_skill_tool_runs_isolated_agent_and_reports_trigger(tmp_path):
    from bos.core import LLMClient
    from bos.core.contract import ToolContext
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader
    from bos.plugins.skills.plugin import _SkillTestRuntime

    skills_dir = tmp_path / "skills"
    suffix = uuid.uuid4().hex
    provider_name = f"test_skill_tool_provider_{suffix}"
    parent_kind = f"skill_test_parent_{suffix}"

    @ep_provider(name=provider_name)
    async def skill_test_provider(messages, model=None, **kwargs):
        if any(m.get("role") == "tool" for m in messages):
            return LLMResponse(content="ahoy, captain!")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="LoadSkill", arguments={"name": "greeter"})],
            finish_reason="tool_calls",
        )

    def make_loader():
        return FileSystemSkillsLoader(skill_dirs=[skills_dir])

    AgentRegistry.register(name=parent_kind, description="parent", tools=[], model=f"{provider_name}/x")
    try:
        runtime = _SkillTestRuntime(
            llm=LLMClient(),
            consolidator=MessageOnlyConsolidator(),
            workspace=str(tmp_path),
            loader_factory=make_loader,
            test_tools="*",
        )
        plugin = SkillsAgentPlugin(make_loader(), allow=None, exclude=[], test_runtime=runtime)
        registry = ToolRegistry(f"_test_skill_tools_{suffix}")
        plugin.register_tools(registry)
        assert registry.get("TestSkill") is not None

        context = ToolContext(agent_name=parent_kind, chat_id="chat", turn_id="turn")
        missing = await registry.invoke("TestSkill", {"name": "greeter", "task": "say hi", "context": context})
        assert "not found" in missing

        # The skill is written *after* the plugin's own loader cached its scan —
        # TestSkill must still see it via its per-run fresh loader.
        _write_skill(skills_dir, "greeter", "Greet users warmly.", "Always greet with 'ahoy'.")
        result = await registry.invoke("TestSkill", {"name": "greeter", "task": "say hi", "context": context})
    finally:
        AgentRegistry._registry.pop(parent_kind, None)
        ep_provider._extensions.pop(provider_name, None)

    assert "Skill under test: greeter" in result
    assert "Triggered (test agent loaded it from the description alone): yes" in result
    assert "ahoy, captain!" in result


def test_test_skill_tool_absent_without_runtime_so_test_agents_cannot_recurse(tmp_path):
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    # The ephemeral agent's plugin is constructed without a test runtime, so a
    # skill test run never exposes TestSkill to the agent under test.
    plugin = SkillsAgentPlugin(FileSystemSkillsLoader(skill_dirs=[tmp_path]), allow=None, exclude=[])
    registry = ToolRegistry(f"_no_test_skill_tools_{uuid.uuid4().hex}")
    plugin.register_tools(registry)
    assert registry.get("TestSkill") is None
    assert registry.get("LoadSkill") is not None


def _write_skill(skills_dir, name, description, body):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


class _FakeSkillsEntryPoint:
    def __init__(self, name, target=None, error=None):
        self.name = name
        self._target = target
        self._error = error

    def load(self):
        if self._error is not None:
            raise self._error
        return self._target


def _fake_entry_points(monkeypatch, *eps):
    from bos.plugins.skills import fs_skill_loader

    def entry_points(group):
        assert group == "bos.skills"
        return list(eps)

    monkeypatch.setattr(fs_skill_loader, "entry_points", entry_points)


@pytest.mark.asyncio
async def test_packages_contribute_skill_dirs_via_entry_points(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    pkg_dir = tmp_path / "pkg_skills"
    _write_skill(pkg_dir, "weather", "Weather skill.", "Contributed weather body.")
    pkg = SimpleNamespace(__path__=[str(pkg_dir)])
    _fake_entry_points(monkeypatch, _FakeSkillsEntryPoint("weather", target=pkg))

    loader = FileSystemSkillsLoader(skill_dirs=["__builtin__"], bos_dir=tmp_path)
    metas = await loader.search_skills()

    assert "weather" in metas
    assert "python" in metas  # bos builtins still included
    body = await loader.load_skill("weather")
    assert "Contributed weather body." in body


@pytest.mark.asyncio
async def test_workspace_skills_override_contributed_skills(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    pkg_dir = tmp_path / "pkg_skills"
    _write_skill(pkg_dir, "alpha", "Alpha skill.", "Contributed alpha body.")
    _write_skill(tmp_path / "skills", "alpha", "Alpha skill.", "Workspace alpha body.")
    pkg = SimpleNamespace(__path__=[str(pkg_dir)])
    _fake_entry_points(monkeypatch, _FakeSkillsEntryPoint("alpha_pkg", target=pkg))

    loader = FileSystemSkillsLoader(skill_dirs=["__builtin__", "skills"], bos_dir=tmp_path)
    body = await loader.load_skill("alpha")
    assert "Workspace alpha body." in body


@pytest.mark.asyncio
async def test_broken_skills_entry_point_is_skipped(tmp_path, monkeypatch):
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    _fake_entry_points(
        monkeypatch,
        _FakeSkillsEntryPoint("broken", error=RuntimeError("boom")),
        _FakeSkillsEntryPoint("not_a_package", target=object()),
    )

    loader = FileSystemSkillsLoader(skill_dirs=["__builtin__"], bos_dir=tmp_path)
    metas = await loader.search_skills()
    assert "python" in metas  # discovery survives broken or non-package entry points


@pytest.mark.asyncio
async def test_skills_preload_renders_full_body_and_omits_from_available(tmp_path):
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "alpha", "Alpha skill.", "Full alpha instructions body.")
    _write_skill(skills_dir, "beta", "Beta skill.", "Full beta instructions body.")

    loader = FileSystemSkillsLoader(skill_dirs=[skills_dir])
    plugin = SkillsAgentPlugin(loader, allow=None, exclude=[], preload=["alpha"])

    prompt = await plugin.get_system_prompt_section(None)

    assert '<skill_instructions name="alpha">' in prompt
    assert "Full alpha instructions body." in prompt
    assert "do not call LoadSkill for them" in prompt
    assert '<skill name="alpha">' not in prompt  # not duplicated as metadata
    assert '<skill name="beta">Beta skill.</skill>' in prompt
    assert "Full beta instructions body." not in prompt


@pytest.mark.asyncio
async def test_skills_preload_unknown_name_is_ignored(tmp_path):
    from bos.plugins.skills.fs_skill_loader import FileSystemSkillsLoader

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "alpha", "Alpha skill.", "Full alpha instructions body.")

    loader = FileSystemSkillsLoader(skill_dirs=[skills_dir])
    plugin = SkillsAgentPlugin(loader, allow=None, exclude=[], preload=["missing"])

    prompt = await plugin.get_system_prompt_section(None)

    assert "<skill_instructions" not in prompt
    assert '<skill name="alpha">Alpha skill.</skill>' in prompt


def test_skills_plugin_default_config_ships_builtins_and_validates_preload():
    plugin = SkillsHarnessPlugin()
    cfg = plugin.default_config()

    assert cfg["skill_dirs"] == ["__builtin__", "skills"]
    assert cfg["preload"] == []
    plugin.validate_config({"preload": ["python"]})
    with pytest.raises(TypeError, match="preload"):
        plugin.validate_config({"preload": "python"})
    with pytest.raises(TypeError, match="preload"):
        plugin.validate_config({"preload": [1]})


@pytest.mark.asyncio
async def test_memories_render_with_shared_prompt_section_format():
    store = InMemMemoryExtension()
    await store.set_maxim("user", "Prefers concise answers.")
    plugin = MemoryAgentPlugin(store, {"user"})
    create_test_agent(plugins=[plugin])
    section = await plugin.get_system_prompt_section(None)
    assert "<memory_workflow>" in section
    assert "Use Recall" in section
    assert "<active_maxims>" in section
    assert '<maxim name="user" scope="your knowledge about the user' in section
    assert "Prefers concise answers." in section


@pytest.mark.asyncio
async def test_memory_workflow_renders_without_active_maxims():
    plugin = MemoryAgentPlugin(InMemMemoryExtension(), {"user"})
    create_test_agent(plugins=[plugin])
    section = await plugin.get_system_prompt_section(None)
    assert "<memory_workflow>" in section
    assert "<active_maxims>" in section
    assert '<maxim name="user" scope="your knowledge about the user' in section
    assert "(empty)" not in section
    assert "Use Remember" in section


@pytest.mark.asyncio
async def test_task_plugin_renders_workflow_prompt_section():
    plugin = TaskAgentPlugin()
    create_test_agent(plugins=[plugin])
    section = await plugin.get_system_prompt_section(None)
    assert "<task_workflow>" in section
    assert "Use TaskCreate" in section
    assert "Only mark a task completed" in section


@pytest.mark.asyncio
async def test_plugin_prompt_sections_render_inside_system_prompt():
    store = InMemMemoryExtension()
    await store.set_maxim("user", "Prefers concise answers.")

    # Register a dummy agent so subagent section renders

    AgentRegistry.register("test-subagent", description="A test subagent.")

    class StaticSkillsLoader:
        async def load_skill(self, name: str) -> str:
            return name

        async def search_skills(self, query: str | None = None) -> dict[str, SkillMeta]:
            return {"code-review": SkillMeta(location="/skills/code-review/SKILL.md", description="Review code.")}

    agent = create_test_agent(
        plugins=[
            MemoryAgentPlugin(store, {"user"}),
            SkillsAgentPlugin(StaticSkillsLoader(), allow=None, exclude=[]),
            TaskAgentPlugin(),
            SubagentAgentPlugin(_MockSubagentRuntime(), enabled=None, disabled=[]),
        ]
    )
    prompt = await agent._build_system_prompt()
    system_end = prompt.index("</system_prompt>")

    assert prompt.index("<memory_workflow>") < system_end
    assert prompt.index("<active_maxims>") < system_end
    assert prompt.index("<skills_workflow>") < system_end
    assert prompt.index("<available_skills>") < system_end
    assert prompt.index("<task_workflow>") < system_end
    assert prompt.index("<subagent_workflow>") < system_end
    assert prompt.index("<available_tools>") > system_end

    AgentRegistry._registry.pop("test-subagent", None)


@pytest.mark.asyncio
async def test_plugins_prompt_overrides_plugin_section():
    class DummyPlugin:
        @property
        def name(self) -> str:
            return "DummyPlugin"

        def register_tools(self, registry) -> None:
            pass

        async def get_system_prompt_section(self, context) -> str | None:
            return "Original dummy prompt section"

        def get_interceptors(self):
            return []

    agent = create_test_agent(
        plugins=[DummyPlugin()],
        plugins_prompt={"DummyPlugin": "Overridden dummy prompt section"},
    )
    prompt = await agent._build_system_prompt()
    assert "Overridden dummy prompt section" in prompt
    assert "Original dummy prompt section" not in prompt


@pytest.mark.asyncio
async def test_prompt_sections_render_first_50_items_and_warn(caplog):
    local_tools = ToolRegistry("_many_test_tools")
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
        # Clean up leaked registrations from other tests
        AgentRegistry._registry.pop("test-agent", None)
        for i, name in enumerate(subagent_names):
            AgentRegistry.register(name=name, description=f"Subagent description {i:03}", tools=[])

        skills_plugin = SkillsAgentPlugin(StaticSkillsLoader(), allow=None, exclude=[])
        subagent_plugin = SubagentAgentPlugin(_MockSubagentRuntime(), enabled=None, disabled=[])

        agent = create_test_agent(
            local_tools=local_tools,
            tools=tool_names,
            plugins=[skills_plugin, subagent_plugin],
        )

        with caplog.at_level(logging.WARNING):
            tools_prompt = await agent._prompt_section_tools()
            skills_prompt = await skills_plugin.get_system_prompt_section(None) or ""
            subagents_prompt = await subagent_plugin.get_system_prompt_section(None) or ""

        assert '<tool name="Tool049">\nTool description 049\n</tool>' in tools_prompt
        assert "Tool050" not in tools_prompt
        assert '<skill name="skill_049">Skill description 049</skill>' in skills_prompt
        assert "skill_050" not in skills_prompt
        assert "<skills_workflow>" in skills_prompt
        assert "<subagent_workflow>" in subagents_prompt
        assert f'<agent role="{subagent_names[49]}">Subagent description 049</agent>' in subagents_prompt
        assert subagent_names[50] not in subagents_prompt
        assert "first 50 tools" in caplog.text
        assert "first 50 subagents" in caplog.text
    finally:
        for name in subagent_names:
            AgentRegistry._registry.pop(name, None)


@pytest.mark.asyncio
async def test_tools_usage_overrides_tool_description_in_prompt():
    local_tools = ToolRegistry("_test_tools")

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
    local_tools = ToolRegistry("_test_tools")

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
    local_tools = ToolRegistry("_test_tools")

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
    tools = ToolRegistry("_test_tools")

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
async def test_parallel_safe_tool_calls_run_concurrently_in_result_order():
    suffix = uuid.uuid4().hex
    provider_name = f"test_parallel_safe_tools_{suffix}"
    tools = ToolRegistry("_parallel_test_tools")
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    @tools(
        name="SafeFirst",
        description="First parallel-safe test tool.",
        parallel_safe=True,
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def safe_first() -> str:
        first_started.set()
        try:
            await asyncio.wait_for(second_started.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            return "first:sequential"
        await asyncio.sleep(0.05)
        return "first:parallel"

    @tools(
        name="SafeSecond",
        description="Second parallel-safe test tool.",
        parallel_safe=True,
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def safe_second() -> str:
        second_started.set()
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        return "second:parallel"

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if tool_messages:
            return LLMResponse(
                content="|".join(message["content"] for message in tool_messages),
                finish_reason="stop",
            )
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="call_first", name="SafeFirst", arguments={}),
                ToolCallRequest(id="call_second", name="SafeSecond", arguments={}),
            ],
            finish_reason="tool_calls",
        )

    try:
        agent = create_test_agent(
            model=f"{provider_name}/parallel",
            local_tools=tools,
            tools=["SafeFirst", "SafeSecond"],
        )
        result = await agent.ask("parallel-safe-chat", "Use the safe tools.")
    finally:
        ep_provider._extensions.pop(provider_name, None)

    assert result == "first:parallel|second:parallel"


@pytest.mark.asyncio
async def test_react_agent_persists_sanitized_abort_on_cancellation():
    store = InMemChatStore()

    class SlowLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, messages, **kwargs):
            self.started.set()
            await asyncio.Event().wait()

    llm = SlowLLM()
    agent = create_test_agent(chat_store=store, llm=llm)

    task = asyncio.create_task(agent.ask("cancel-chat", "Please do a long task."))
    await asyncio.wait_for(llm.started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    messages = store._messages["cancel-chat"]
    assert [message.llm_message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0].llm_message["content"] == "Please do a long task."
    assert "turn aborted before completion" in messages[1].llm_message["content"]
    assert messages[1].metadata["turn_status"] == "aborted"
    assert messages[1].metadata["abort_reason"] == "cancelled"


@pytest.mark.asyncio
async def test_react_agent_abort_persistence_drops_incomplete_tool_call_state():
    store = InMemChatStore()
    tools = ToolRegistry("_test_tools")
    tool_started = asyncio.Event()

    @tools(
        name="SlowTool",
        description="Wait until cancelled.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def slow_tool() -> str:
        tool_started.set()
        await asyncio.Event().wait()
        return "unreachable"

    class ToolCallingLLM:
        async def complete(self, messages, **kwargs):
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_slow", name="SlowTool", arguments={})],
                finish_reason="tool_calls",
            )

    agent = create_test_agent(
        chat_store=store,
        llm=ToolCallingLLM(),
        local_tools=tools,
        tools=["SlowTool"],
    )

    task = asyncio.create_task(agent.ask("cancel-tool-chat", "Use the slow tool."))
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    messages = store._messages["cancel-tool-chat"]
    assert [message.llm_message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0].llm_message["content"] == "Use the slow tool."
    assert "turn aborted before completion" in messages[1].llm_message["content"]
    assert all("tool_calls" not in message.llm_message for message in messages)


@pytest.mark.asyncio
async def test_react_agent_cancellation_after_final_response_persists_answer():
    store = InMemChatStore()

    class CancelOnFinalResponse:
        async def intercept(self, stage, context):
            if stage == "final_response":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

    class FinalLLM:
        async def complete(self, messages, **kwargs):
            return LLMResponse(content="done", finish_reason="stop")

    agent = create_test_agent(
        chat_store=store,
        llm=FinalLLM(),
        interceptor=CancelOnFinalResponse(),
    )

    with pytest.raises(asyncio.CancelledError):
        await agent.ask("cancel-after-final-chat", "Finish then cancel.")

    messages = store._messages["cancel-after-final-chat"]
    assert [message.llm_message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1].llm_message["content"] == "done"
    assert "turn aborted before completion" not in messages[1].llm_message["content"]


@pytest.mark.asyncio
async def test_harness_passes_tool_config_to_agent_tools(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"test_tool_config_provider_{suffix}"
    tools = ToolRegistry("_test_tools")

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
        ) as harness:
            agent = await harness.create_agent(
                agent_cfg={
                    "model": f"{provider_name}/tool-config",
                    "tools": ["EchoWithConfig"],
                }
            )
            agent._local_tools.register(tools.get("EchoWithConfig"))
            result = await agent.ask("tool-config-chat", "Use the tool.")

        assert '"text": "from model"' in result
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_harness_consolidator_model_precedence(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    monkeypatch.setenv("BOS_CONSOLIDATOR_MODEL", "env/consolidator")
    monkeypatch.setenv("BOS_MODEL", "env/base")
    async with AgentHarness(
        bos_dir=bos_dir,
        workspace=tmp_path,
    ) as harness:
        # Consolidator model comes from env var via EP defaults
        assert harness.consolidator is not None

    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL")
    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        assert harness.consolidator is not None


@pytest.mark.asyncio
async def test_harness_uses_bos_consolidator_model_before_bos_model(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    monkeypatch.setenv("BOS_CONSOLIDATOR_MODEL", "env/consolidator")
    monkeypatch.setenv("BOS_MODEL", "env/base")

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        assert harness.consolidator is not None


@pytest.mark.asyncio
async def test_harness_allows_no_consolidator_model(tmp_path, monkeypatch):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL", raising=False)
    monkeypatch.delenv("BOS_MODEL", raising=False)

    async with AgentHarness(bos_dir=bos_dir, workspace=tmp_path) as harness:
        assert harness.consolidator is not None


def test_bootstrap_platform_does_not_require_consolidator_model(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONSOLIDATOR_MODEL", raising=False)
    monkeypatch.delenv("BOS_MODEL", raising=False)

    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir(parents=True, exist_ok=True)
    ws = Workspace(tmp_path, bos_dir, {"runtime": {"location": "process", "actors": {"main": {"agent": "_default"}}}})
    ws.bootstrap_platform()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_harness_closes_custom_consolidator(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    consolidator = CloseTrackingConsolidator()

    from bos.core.registry import Extension

    ep_consolidator.register(
        Extension(
            name="_close_test",
            fn=lambda model=None, llm=None, **kw: consolidator,
        )
    )

    try:
        async with AgentHarness(
            bos_dir=bos_dir,
            workspace=tmp_path,
            consolidator="_close_test",
        ):
            pass
    finally:
        ep_consolidator._extensions.pop("_close_test", None)

    assert consolidator.closed is True


@pytest.mark.asyncio
async def test_agent_history_budget_uses_resolved_turn_model(monkeypatch):
    suffix = uuid.uuid4().hex
    provider_name = f"test_budget_model_provider_{suffix}"
    seen_budget_models: list[str | None] = []

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, **kwargs):
        return LLMResponse(content="ok")

    try:
        agent = create_test_agent(model=f"{provider_name}/base")
        original_get_context = agent._chat_store.get_context

        async def tracking_get_context(chat_id, *, tokenizer_model=None, filter_mode=None):
            seen_budget_models.append(tokenizer_model)
            return await original_get_context(chat_id, tokenizer_model=tokenizer_model, filter_mode=filter_mode)

        monkeypatch.setattr(agent._chat_store, "get_context", tracking_get_context)
        result = await agent.ask("budget-model-chat", "Hello.", llm_args={"model": f"{provider_name}/override"})

        assert result == "ok"
        assert seen_budget_models == [f"{provider_name}/override"]
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_agent_auto_compaction_passes_message_objects(monkeypatch):
    from bos.core.contract import ContextResult

    store = InMemChatStore()
    consolidator = MessageOnlyConsolidator()
    await store.commit_turn(
        "compact-chat",
        [Message(llm_message={"role": "user", "content": "large history"})],
        turn_id="seed-turn",
    )

    real_get_context = store.get_context
    call_count = [0]

    async def fake_get_context(chat_id, *, tokenizer_model=None, filter_mode=None):
        call_count[0] += 1
        if call_count[0] <= 2:
            # First two calls: high token count triggers compaction
            result = await real_get_context(chat_id, tokenizer_model=tokenizer_model, filter_mode=filter_mode)
            return ContextResult(
                messages=result.messages,
                source_messages=result.source_messages,
                estimated_tokens=999,
                tokenizer_model=result.tokenizer_model,
                estimation_source=result.estimation_source,
                filter_mode=result.filter_mode,
                summary_applied=result.summary_applied,
                summary_message_count_excluded=result.summary_message_count_excluded,
                latest_summary=result.latest_summary,
            )
        return await real_get_context(chat_id, tokenizer_model=tokenizer_model, filter_mode=filter_mode)

    monkeypatch.setattr(store, "get_context", fake_get_context)

    import asyncio

    compaction_locks: dict[str, asyncio.Lock] = {}

    def get_compaction_lock(chat_id: str) -> asyncio.Lock:
        if chat_id not in compaction_locks:
            compaction_locks[chat_id] = asyncio.Lock()
        return compaction_locks[chat_id]

    agent = create_test_agent(
        chat_store=store,
        consolidator=consolidator,
        max_tokens=1,
        chat_compaction_lock=get_compaction_lock,
    )

    history = await agent._load_and_compact_history("compact-chat", budget_model="test/model")

    assert consolidator.calls
    assert all(isinstance(message, Message) for message in consolidator.calls[0][0])
    assert any(message.is_summary for message in store._messages["compact-chat"])
    assert history[-1]["content"].startswith("Chat summary:")


@pytest.mark.asyncio
async def test_agent_emits_task_state_events(monkeypatch):
    """After TaskCreate/TaskUpdate calls, the event sink receives task_state events."""
    suffix = uuid.uuid4().hex
    provider_name = f"test_task_events_{suffix}"

    call_count = [0]
    captured_task_id: list[str] = []

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, tools=None, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1",
                        name="TaskCreate",
                        arguments={
                            "tasks": [
                                {"subject": "Task 1", "description": "First task"},
                                {"subject": "Task 2", "description": "Second task"},
                            ]
                        },
                    ),
                ],
                finish_reason="tool_calls",
            )
        elif call_count[0] == 2:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc3",
                        name="TaskUpdate",
                        arguments={"updates": [{"taskId": captured_task_id[0], "status": "in_progress"}]},
                    ),
                ],
                finish_reason="tool_calls",
            )
        else:
            return LLMResponse(content="All done.", finish_reason="stop")

    events: list = []

    class CaptureSink:
        async def emit(self, event):
            events.append(event)
            if event.event_type == "task" and event.detail == "task_state":
                tasks = event.metadata.get("tasks", [])
                if tasks and not captured_task_id:
                    captured_task_id.append(tasks[0]["id"])

    try:
        agent = create_test_agent(model=f"{provider_name}/model", plugins=[TaskAgentPlugin()])
        await agent.ask(
            chat_id="task-events-chat",
            content="Create two tasks then update one.",
            event_sink=CaptureSink(),
        )
    finally:
        ep_provider._extensions.pop(provider_name, None)

    task_events = [e for e in events if e.event_type == "task" and e.detail == "task_state"]
    assert len(task_events) >= 2, f"Expected >= 2 task_state events (bulk create + update), got {len(task_events)}"

    # After the bulk TaskCreate: both tasks present and pending
    tasks1 = task_events[0].metadata.get("tasks", [])
    assert [t["subject"] for t in tasks1] == ["Task 1", "Task 2"]
    assert all(t["status"] == "pending" for t in tasks1)

    # After TaskUpdate: first task now in_progress
    tasks2 = task_events[1].metadata.get("tasks", [])
    assert tasks2[0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_cache_hint_offsets_for_ephemeral_messages():
    suffix = uuid.uuid4().hex
    provider_name = f"test_cache_ephemeral_{suffix}"
    calls: list[dict] = []

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, tools=None, **kwargs):
        calls.append({
            "messages": messages,
            "cache_control_injection_points": kwargs.get("cache_control_injection_points"),
        })
        if len(calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="tc1", name="Echo", arguments={})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done", finish_reason="stop")

    class EphemeralInterceptor:
        async def intercept(self, stage, context):
            if stage == "before_llm":
                context.ephemeral = [{"role": "user", "content": "ephemeral note"}]

    async def echo() -> str:
        return "tool result"

    try:
        agent = create_test_agent(
            model=f"{provider_name}/model",
            interceptor=EphemeralInterceptor(),
            tools=["Echo"],
        )
        agent._local_tools(
            name="Echo",
            description="Echo test tool.",
            parameters={"type": "object", "properties": {}, "required": []},
        )(echo)

        await agent.ask("cache-ephemeral-chat", "run tool")

        assert len(calls) == 2
        second_messages = calls[1]["messages"]
        assert [message["role"] for message in second_messages] == [
            "system",
            "user",
            "assistant",
            "tool",
            "user",
        ]
        assert second_messages[-1]["content"] == "ephemeral note"
        assert "_ephemeral_key" not in second_messages[-1]
        assert calls[1]["cache_control_injection_points"] == [
            {"location": "message", "role": "system"},
            {"location": "message", "index": -4},
        ]
    finally:
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_task_plugin_injects_current_tasks_as_ephemeral_user_context():
    suffix = uuid.uuid4().hex
    provider_name = f"test_task_ephemeral_{suffix}"
    calls: list[dict] = []

    @ep_provider(name=provider_name)
    async def provider(messages, model=None, tools=None, **kwargs):
        calls.append({
            "messages": messages,
            "cache_control_injection_points": kwargs.get("cache_control_injection_points"),
        })
        if len(calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_task_create",
                        name="TaskCreate",
                        arguments={
                            "tasks": [
                                {
                                    "subject": "Implement <feature>",
                                    "description": "Update the & parser.",
                                }
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done", finish_reason="stop")

    try:
        agent = create_test_agent(
            model=f"{provider_name}/model",
            plugins=[TaskAgentPlugin()],
            tools=["TaskCreate"],
        )

        await agent.ask("task-ephemeral-chat", "Track the work.")

        assert len(calls) == 2
        first_messages = calls[0]["messages"]
        second_messages = calls[1]["messages"]
        assert "<current_tasks>" not in first_messages[0]["content"]
        assert [message["role"] for message in second_messages] == [
            "system",
            "user",
            "assistant",
            "tool",
            "user",
        ]
        task_context = second_messages[-1]["content"]
        assert task_context.startswith("<current_tasks>")
        assert "_ephemeral_key" not in second_messages[-1]
        assert '<task id="1" status="pending" blocked_by="" blocks="">' in task_context
        assert "<subject>Implement &lt;feature&gt;</subject>" in task_context
        assert "<description>Update the &amp; parser.</description>" in task_context
        assert calls[1]["cache_control_injection_points"] == [
            {"location": "message", "role": "system"},
            {"location": "message", "index": -4},
        ]
    finally:
        ep_provider._extensions.pop(provider_name, None)
