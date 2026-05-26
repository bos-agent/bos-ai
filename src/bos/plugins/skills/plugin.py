"""SkillsHarnessPlugin and SkillsAgentPlugin."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
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

ep_skills_loader = ExtensionPoint(
    description="Skills loader implementations (FileSystemSkillsLoader, etc.)."
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


@ep_plugin(name="SkillsPlugin")
class SkillsHarnessPlugin:
    @property
    def name(self) -> str:
        return "SkillsPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {"skill_dirs": ["skills"], "allow": "*", "exclude": [], "loader": "_default"}

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

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        cache_key = _loader_cache_key(config)
        loader_name, skill_dirs = cache_key
        if cache_key not in self._loaders:
            loader_ext = ep_skills_loader.get(loader_name)
            if loader_ext is None:
                raise ValueError(f"SkillsPlugin: unknown loader {loader_name!r}")
            self._loaders[cache_key] = loader_ext.fn(
                bos_dir=self._services.bos_dir,
                skill_dirs=list(skill_dirs),
            )
        loader = self._loaders[cache_key]
        allow = config.get("allow")
        exclude = config.get("exclude", [])
        if isinstance(allow, str) and allow == "*":
            allow = None
        return SkillsAgentPlugin(loader, allow, exclude)

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
    def __init__(self, loader: Any, allow: list[str] | str | None, exclude: list[str]) -> None:
        self._loader = loader
        self._allow = allow
        self._exclude = exclude

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

    async def get_system_prompt_section(self, context: TurnContext | None) -> str | None:
        import logging

        logger = logging.getLogger(__name__)
        available = await self._loader.search_skills()
        available = _pick_collection(available, self._allow, self._exclude)
        sections = [_SKILLS_PROMPT_SECTION]
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
