import logging
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from importlib.metadata import entry_points
from pathlib import Path

from bos.core import _read_text

from .plugin import SkillMeta, pep_skills_loader

logger = logging.getLogger(__name__)


def _contributed_skill_dirs() -> list[Path]:
    """Collect skill dirs shipped by packages via the ``bos.skills`` entry-point group.

    Each entry point names a package whose directory contains skills (each
    skill is a subdirectory with ``SKILL.md``)::

        [project.entry-points."bos.skills"]
        weather = "bos_weather_tools.skills"
    """
    dirs: list[Path] = []
    for ep in entry_points(group="bos.skills"):
        try:
            target = ep.load()
        except Exception:
            logger.warning("Failed to load bos.skills entry point %r; skipping.", ep.name, exc_info=True)
            continue
        if hasattr(target, "__path__"):
            dirs.extend(Path(p).resolve() for p in target.__path__)
        elif getattr(target, "__file__", None):
            dirs.append(Path(target.__file__).parent.resolve())
        else:
            logger.warning("bos.skills entry point %r does not resolve to a package; skipping.", ep.name)
    return dirs


def _parse_frontmatter_fields(frontmatter: str) -> dict[str, str]:
    """Parse the simple YAML-style front matter fields used by skill files."""

    def strip_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    def normalize_block(block: list[str], style: str) -> str:
        lines = [line[2:] if line.startswith("  ") else line.lstrip() for line in block]
        if style == ">":
            return " ".join(line.strip() for line in lines if line.strip())
        return "\n".join(lines).strip()

    fields: dict[str, str] = {}
    lines = frontmatter.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            i += 1
            continue

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        block_style = value[:1] if value in {">", "|", ">-", "|-", ">+", "|+"} else ""
        if block_style:
            block: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line and not next_line.startswith((" ", "\t")) and ":" in next_line:
                    break
                block.append(next_line)
                i += 1
            fields[key] = normalize_block(block, block_style)
            continue

        fields[key] = strip_quotes(value)
        i += 1

    return fields


@pep_skills_loader(name="_default")
class FileSystemSkillsLoader:
    def __init__(self, skill_dirs: Iterable[Path | str] | None = None, bos_dir: str | Path | None = None) -> None:
        if skill_dirs is None:
            skill_dirs = ["__builtin__", "skills"]
        self._base_dir = Path(bos_dir or ".")
        self._raw_dirs = [str(d) for d in skill_dirs]
        self._resolved_dirs: list[Path] | None = None
        self._skill_metas: dict[str, SkillMeta] = {}
        self._skill_metas_refreshed_at = datetime(2000, 1, 1)

    async def _get_skill_dirs(self) -> list[Path]:
        """Resolve configured dirs once, expanding `__builtin__` in place.

        The sentinel expands to the packaged bos.skills dirs followed by the
        ``bos.skills`` entry-point contributions, so later (workspace) dirs
        win on skill-name clashes.
        """
        if self._resolved_dirs is None:
            dirs: list[Path] = []
            for raw in self._raw_dirs:
                if raw == "__builtin__":
                    import bos.skills

                    dirs.extend(Path(p).resolve() for p in bos.skills.__path__)
                    dirs.extend(_contributed_skill_dirs())
                else:
                    dirs.append((self._base_dir / Path(raw).expanduser()).resolve())
            self._resolved_dirs = dirs
        return self._resolved_dirs

    async def load_skill(self, name: str) -> str:
        skill_files = await self._list_skill_files()
        return _read_text(skill_files[name])

    async def search_skills(self, query: str | None = None) -> dict[str, SkillMeta]:
        skill_metas = await self._get_skill_metas()
        query = query and query.lower()
        return (
            skill_metas
            if not query
            else {
                name: sm
                for name, sm in skill_metas.items()
                if query in name.lower() or query in sm.name.lower() or query in sm.description.lower()
            }
        )

    async def _get_skill_metas(self) -> dict[str, SkillMeta]:
        now = datetime.now()
        if now - self._skill_metas_refreshed_at > timedelta(minutes=5):
            self._skill_metas = await self._load_skill_metas()
            self._skill_metas_refreshed_at = now
        return self._skill_metas

    async def _list_skill_files(self) -> dict[str, Path]:
        skill_files = {}
        for d in await self._get_skill_dirs():
            if (d / "SKILL.md").exists():
                skill_files[d.name] = d / "SKILL.md"
            if d.is_dir():
                for c in d.iterdir():
                    if c.is_dir() and (c / "SKILL.md").exists():
                        skill_files[c.name] = c / "SKILL.md"
        return skill_files

    async def _load_skill_metas(self) -> dict[str, SkillMeta]:
        skill_files = await self._list_skill_files()
        skill_metas = {}
        for skill_name, path in skill_files.items():
            content = path.read_text(encoding="utf-8")
            description = ""
            if frontmatter := re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL):
                metadata = _parse_frontmatter_fields(frontmatter.group(1))
                description = metadata.get("description") or metadata.get("summary") or ""
            if not description:
                for line in (line.strip() for line in content.splitlines() if line.strip()):
                    if len(description) > 250:
                        break
                    description += line + " "
            skill_metas[skill_name] = SkillMeta(
                location=str(path),
                name=skill_name,
                description=description,
            )
        return skill_metas
