"""SkillsPlugin — skill discovery, loading, and prompt section."""

from . import fs_skill_loader  # noqa: E402  registers FileSystemSkillsLoader
from .plugin import SkillMeta, SkillsAgentPlugin, SkillsHarnessPlugin, pep_skills_loader  # noqa: E402

__all__ = [
    "SkillMeta",
    "SkillsAgentPlugin",
    "SkillsHarnessPlugin",
    "fs_skill_loader",
    "pep_skills_loader",
]
