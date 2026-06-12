"""SkillsHarnessPlugin and SkillsAgentPlugin."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from bos.core._utils import _allowed, _pick_collection, _xml_attr
from bos.core.contract import (
    AgentPlugin,
    PluginServices,
    TurnInterceptor,
    ep_plugin,
)
from bos.core.registry import ExtensionPoint, ToolRegistry

# Plugin-defined extension points use the `pep_` prefix (plugin extension
# point) to distinguish them from the core `ep_` points in bos.core.contract.
pep_skills_loader = ExtensionPoint(
    name="pep_skills_loader",
    description="Skills loader implementations (FileSystemSkillsLoader, etc.).",
)


@dataclass
class SkillMeta:
    location: str
    name: str = ""
    description: str = ""


def _normalize_skill_dirs(skill_dirs: Any) -> tuple[str, ...]:
    if not isinstance(skill_dirs, list):
        raise TypeError("SkillsPlugin: 'skill_dirs' must be a list")
    normalized: list[str] = []
    for skill_dir in skill_dirs:
        if not isinstance(skill_dir, (str, os.PathLike)):
            raise TypeError("SkillsPlugin: 'skill_dirs' entries must be strings or paths")
        normalized.append(str(skill_dir))
    return tuple(normalized)


def _loader_cache_key(config: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    return str(config.get("loader", "_default")), _normalize_skill_dirs(config.get("skill_dirs", ["skills"]))


if TYPE_CHECKING:
    from bos.core.agent import TurnContext
    from bos.core.contract import ToolContext


@dataclass(frozen=True)
class _SkillTestRuntime:
    """Harness services captured at bind time so TestSkill can build throwaway agents."""

    llm: Any
    consolidator: Any
    workspace: str
    loader_factory: Callable[[], Any]
    test_tools: Any = "*"  # "*" (all tools) or an explicit tool-name list


class _RecordingLoader:
    """Skills-loader wrapper that records load_skill calls (TestSkill's triggering signal)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.loaded: list[str] = []

    async def load_skill(self, name: str) -> str:
        self.loaded.append(name)
        return await self._inner.load_skill(name)

    async def search_skills(self, query: str | None = None) -> dict[str, SkillMeta]:
        return await self._inner.search_skills(query)


@ep_plugin(name="SkillsPlugin")
class SkillsHarnessPlugin:
    @property
    def name(self) -> str:
        return "SkillsPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {
            "skill_dirs": ["__builtin__", "skills"],
            "allow": "*",
            "exclude": [],
            "loader": "_default",
            "preload": [],
            "test_tools": "*",
        }

    async def setup(self, services: PluginServices) -> None:
        self._services = services
        self._loaders: dict[tuple[str, tuple[str, ...]], Any] = {}

    def validate_config(self, config: Mapping[str, Any]) -> None:
        _normalize_skill_dirs(config.get("skill_dirs", []))
        allow = config.get("allow")
        if allow is not None and not isinstance(allow, (str, list)):
            raise TypeError("SkillsPlugin: 'allow' must be a string, list, or None")
        exclude = config.get("exclude")
        if exclude is not None and not isinstance(exclude, list):
            raise TypeError("SkillsPlugin: 'exclude' must be a list or None")
        preload = config.get("preload")
        if preload is not None and (
            not isinstance(preload, list) or not all(isinstance(p, str) for p in preload)
        ):
            raise TypeError("SkillsPlugin: 'preload' must be a list of skill names")
        test_tools = config.get("test_tools")
        if test_tools is not None and test_tools != "*":
            if not isinstance(test_tools, list) or not all(isinstance(t, str) for t in test_tools):
                raise TypeError('SkillsPlugin: \'test_tools\' must be "*" or a list of tool names')

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        cache_key = _loader_cache_key(config)
        loader_name, skill_dirs = cache_key
        loader_ext = pep_skills_loader.get(loader_name)
        if loader_ext is None:
            raise ValueError(f"SkillsPlugin: unknown loader {loader_name!r}")
        if cache_key not in self._loaders:
            self._loaders[cache_key] = loader_ext.fn(
                bos_dir=self._services.bos_dir,
                skill_dirs=list(skill_dirs),
            )
        loader = self._loaders[cache_key]
        allow = config.get("allow")
        exclude = config.get("exclude", [])
        if isinstance(allow, str) and allow == "*":
            allow = None
        services = self._services
        test_runtime = _SkillTestRuntime(
            llm=services.llm,
            consolidator=services.consolidator,
            workspace=str(services.workspace),
            # A fresh loader per test run so a just-written skill is visible
            # immediately, bypassing the shared loader's metadata cache.
            loader_factory=lambda: loader_ext.fn(bos_dir=services.bos_dir, skill_dirs=list(skill_dirs)),
            test_tools=config.get("test_tools", "*"),
        )
        return SkillsAgentPlugin(
            loader, allow, exclude, preload=config.get("preload", []), test_runtime=test_runtime
        )

    async def teardown(self) -> None:
        from bos.core._utils import _aclose

        for loader in self._loaders.values():
            await _aclose(loader)
        self._loaders.clear()


_SKILLS_TOOL_USAGE = {
    "LoadSkill": """Load an allowed skill's full instructions.

Use progressive disclosure: available_skills gives only compact metadata; LoadSkill returns the
full task-specific instructions. Use when a skill clearly matches the user's request or the user
explicitly names it. After loading a skill, follow its instructions before continuing with the task.

Guidelines:
- Do not invent skill names.
- Load only relevant skills.
- Load the full skill before relying on skill-specific procedures, scripts, templates, or assets.
- Treat skill instructions as task-specific operating guidance alongside repository instructions.""",
    "TestSkill": """Run a task against an isolated throwaway agent with exactly one skill enabled.

Use while creating or improving a skill, to check two things at once: whether the skill triggers
(the test agent decides to load it from the description alone) and whether following its body
produces the right result. The run is fully isolated — in-memory chat state, a fresh scan of the
skill directories (a just-written skill is visible immediately), no change to project config,
and no interaction with the running gateway or real chat history.

Guidelines:
- Phrase task as a realistic user prompt; do not name the skill in it, or the triggering check
  proves nothing.
- Run 2-3 differently phrased tasks before judging the skill.
- A 'not triggered' result means the description needs work; a wrong response means the body does.
- The test agent sees only the skill under test, so this cannot detect a description that loses
  to a competing skill — do a final end-to-end check through the real agent for that.""",
}

_SKILLS_PROMPT_SECTION = """<skills_workflow>
Use skills as progressively disclosed task playbooks.

- The available_skills list contains compact metadata only; it is not the full instruction body.
- Use LoadSkill when a listed skill clearly matches the user's request, the user names a skill, or specialized
  procedures/templates/assets would reduce risk or improve quality.
- Load only relevant skills; do not load skills speculatively for unrelated work.
- Do not invent skill names. Use the exact name attribute from available_skills as the LoadSkill name.
- After loading a skill, follow its instructions together with user, repository, and system guidance.
- If no listed skill applies, continue with ordinary tools and reasoning.
</skills_workflow>"""


class SkillsAgentPlugin:
    def __init__(
        self,
        loader: Any,
        allow: list[str] | str | None,
        exclude: list[str],
        preload: Sequence[str] = (),
        test_runtime: _SkillTestRuntime | None = None,
    ) -> None:
        self._loader = loader
        self._allow = allow
        self._exclude = exclude
        self._preload = tuple(preload)
        self._test_runtime = test_runtime

    @property
    def name(self) -> str:
        return "SkillsPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        allow = self._allow
        exclude = self._exclude
        loader = self._loader

        @registry(
            name="LoadSkill",
            description="Read an allowed skill's full instructions and return them as the tool result.",
            usage=_SKILLS_TOOL_USAGE["LoadSkill"],
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill name"}},
                "required": ["name"],
            },
        )
        async def load_skill(name: str) -> str:
            if not _allowed(name, allow, exclude):
                raise ValueError(f"Skill '{name}' is not allowed.")
            try:
                return await loader.load_skill(name)
            except Exception as ex:
                return f"(Failed to load skill '{name}': {ex}.)"

        if self._test_runtime is not None:
            self._register_test_tool(registry, self._test_runtime)

    def _register_test_tool(self, registry: ToolRegistry, runtime: _SkillTestRuntime) -> None:
        @registry(
            name="TestSkill",
            description=(
                "Test a skill in isolation: run a task on a throwaway agent that has only "
                "that skill enabled, and report whether it triggered and what it produced."
            ),
            usage=_SKILLS_TOOL_USAGE["TestSkill"],
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name (its directory name)."},
                    "task": {
                        "type": "string",
                        "description": "Realistic user prompt to run; must not name the skill.",
                    },
                },
                "required": ["name", "task"],
            },
        )
        async def test_skill(name: str, task: str, context: ToolContext | None = None) -> str:
            import uuid

            from bos.core import AgentRegistry
            from bos.core.agent import Agent
            from bos.extensions.chat_stores.in_memory import InMemChatStore

            loader = _RecordingLoader(runtime.loader_factory())
            try:
                metas = await loader.search_skills()
            except Exception as ex:
                return f"(Skill discovery failed: {ex}.)"
            if name not in metas:
                listing = ", ".join(sorted(metas)) or "none"
                return f"(Skill '{name}' not found. Discovered skills: {listing}.)"

            # Inherit the calling agent's model when it is resolvable from the
            # registry; otherwise fall back to the LLM client default (BOS_MODEL).
            model = AgentRegistry.get_defaults(context.agent_name).get("model") if context else None

            test_tools = runtime.test_tools
            tools = None if test_tools == "*" else sorted(set(test_tools) | {"LoadSkill"})

            # Conceptually a subagent run, but built directly: the harness
            # subagent path pins the shared chat store and cached skills
            # loader, and gives no signal on whether LoadSkill was called.
            test_agent = Agent(
                kind="_skill_test",
                agent_name=f"skill-test:{name}",
                system_prompt=(
                    "You are a general-purpose assistant. Handle the user's task as you "
                    "normally would, using the tools and skills available to you."
                ),
                chat_store=InMemChatStore(),
                consolidator=runtime.consolidator,
                llm=runtime.llm,
                model=model,
                tools=tools,
                plugins=[SkillsAgentPlugin(loader, allow=[name], exclude=[])],
                max_iterations=30,
                workspace=runtime.workspace,
            )
            try:
                response = await test_agent.ask(f"skill-test-{uuid.uuid4().hex}", task)
            except Exception as ex:
                return f"(Skill test run failed: {ex}.)"

            triggered = name in loader.loaded
            verdict = "yes" if triggered else "NO — the description did not convince the agent to load it"
            return (
                f"Skill under test: {name}\n"
                f"Triggered (test agent loaded it from the description alone): {verdict}\n"
                f"--- test agent response ---\n{response}"
            )

    async def get_system_prompt_section(self, context: TurnContext | None) -> str | None:
        import logging

        logger = logging.getLogger(__name__)
        available = await self._loader.search_skills()
        # Copy: _pick_collection may return the loader's cached dict unfiltered.
        available = dict(_pick_collection(available, self._allow, self._exclude))
        sections = [_SKILLS_PROMPT_SECTION]

        preloaded: list[str] = []
        for name in self._preload:
            if name not in available:
                continue
            try:
                body = await self._loader.load_skill(name)
            except Exception:
                logger.warning(
                    "Failed to preload skill %r; leaving it in available_skills.", name, exc_info=True
                )
                continue
            available.pop(name)
            preloaded.append(f'<skill_instructions name="{_xml_attr(name)}">\n{body.strip()}\n</skill_instructions>')
        if preloaded:
            preloaded.append(
                "(The skill_instructions above are already fully loaded; do not call LoadSkill for them.)"
            )
            sections.append("\n\n".join(preloaded))

        if not available:
            return "\n\n".join(sections)
        try:
            limit = int(os.environ.get("BOS_CAPABILITY_LIMIT", 50))
        except Exception:
            limit = 50
        if len(available) > limit:
            logger.warning(
                "Rendering only the first %d skills in the system prompt; %d are available.",
                limit,
                len(available),
            )
            available = dict(list(available.items())[:limit])
        section = "<available_skills>\n"
        section += "\n".join(
            f'<skill name="{_xml_attr(name)}">{escape(meta.description or "")}</skill>'
            for name, meta in available.items()
        )
        section += "\n</available_skills>"
        sections.append(section)
        return "\n\n".join(sections)

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        return []
