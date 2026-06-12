"""Project scaffolding engine for ``boscli project`` (BEP 9).

Archetypes are template directories under ``templates/``; shared fragments live
in ``templates/_shared/``. Rendering uses :class:`string.Template` only — the
scaffold must stay deterministic and dependency-free.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from bos.config import WorkspaceResolutionError, validate_config
from bos.config.workspace import _find_discovered_config

_TEMPLATES_DIR = Path(__file__).parent / "templates"

ARCHETYPES: tuple[str, ...] = ("assistant", "team", "service", "telegram-bot")


def render_template(relpath: str, context: dict[str, str] | None = None) -> str:
    """Render a packaged template with ``string.Template`` substitution."""
    raw = (_TEMPLATES_DIR / relpath).read_text(encoding="utf-8")
    return Template(raw).substitute(context or {})


def toml_multiline_text(value: str) -> str:
    """Sanitize free text for embedding inside a TOML multi-line basic string."""
    return value.replace("\\", "\\\\").replace('"""', "'''").strip()


def toml_string(value: str) -> str:
    """Sanitize free text for embedding inside a single-line TOML basic string."""
    cleaned = " ".join(value.split())
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def frontmatter_text(value: str) -> str:
    """Sanitize free text for a single-line frontmatter value."""
    return " ".join(value.split())


@dataclass
class ScaffoldResult:
    workspace: Path
    bos_dir: Path
    config_file: Path
    written: list[Path] = field(default_factory=list)


def scaffold_workspace(
    workspace: str | Path,
    archetype: str,
    context: dict[str, str],
    *,
    dotbos: bool = False,
    env_content: str = "",
    agent_files: dict[str, str] | None = None,
) -> ScaffoldResult:
    """Render *archetype* into *workspace* and return the written layout.

    The rendered config is validated against the BEP 6 schema *before* any
    file is written — a scaffold that fails validation is a template bug, not
    a user error. On a later I/O failure, already-written files are removed.
    """
    if archetype not in ARCHETYPES:
        raise ValueError(f"Unknown archetype {archetype!r}. Available: {', '.join(ARCHETYPES)}")

    workspace = Path(workspace).expanduser().resolve()
    existing = _find_discovered_config(workspace)
    if existing is not None:
        raise WorkspaceResolutionError(
            f"Workspace already initialized: found {existing}. Use `boscli project add` to extend it."
        )

    config_text = render_template(f"{archetype}/bos.toml.tmpl", context)
    validate_config(tomllib.loads(config_text))

    if dotbos:
        bos_dir = workspace / ".bos"
        config_file = bos_dir / "config.toml"
    else:
        bos_dir = workspace
        config_file = workspace / "bos.toml"

    result = ScaffoldResult(workspace=workspace, bos_dir=bos_dir, config_file=config_file)

    def write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result.written.append(path)

    try:
        write(config_file, config_text)
        write(workspace / "README.md", render_template("_shared/README.md.tmpl", context))
        write(bos_dir / ".env", env_content)
        if not (workspace / ".gitignore").exists():
            write(workspace / ".gitignore", render_template("_shared/gitignore.tmpl"))
        write(bos_dir / "extensions" / "project_tools.py", render_template("_shared/project_tools.py.tmpl"))
        write(bos_dir / "skills" / "example-skill" / "SKILL.md", render_template("_shared/skill.md.tmpl"))
        agents_dir = bos_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in (agent_files or {}).items():
            write(agents_dir / filename, content)
    except Exception:
        for path in result.written:
            path.unlink(missing_ok=True)
        raise

    return result
