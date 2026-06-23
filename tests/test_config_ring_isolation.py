"""Guard: ``bos.config`` is a consumer of the harness — it imports only inward (BEP 13 §3.2).

``bos.config`` (workspace loading, the TOML schema, the default agent spec) sits
one ring *outward* of the assembly ring: it imports ``bos.core`` (``AgentHarness``,
``ep_agent``, contract types, published helpers); ``bos.core`` imports nothing from
``bos.config``. Its ring discipline is that every source dependency points inward:

1. **No outward imports.** Nothing under ``bos/config/`` may import an outer ring —
   ``bos.gateway`` / ``bos.cli`` / ``bos.runner`` (process entrypoints),
   ``bos.extensions`` (adapters), or ``bos.exts`` (composition root). (Config *names*
   ``"bos.exts"`` / ``"./extensions"`` as string defaults for the extension loader;
   those are data, not imports, so this AST check does not flag them.)
2. **No inner-ring private reaches.** It imports the assembly ring and foundations
   through their published API, never an underscore-prefixed private module such as
   ``bos.core._utils`` or ``bos.core.agent._content`` (BEP 13 §1.6 rule 4). The
   ``_``-prefixed *helpers* it needs are re-exported from ``bos.core`` itself.

Static (AST) check, resolving relative imports to absolute so an escaping
``from ..gateway import`` or ``from bos.core._utils import`` is caught, not skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
CONFIG_DIR = SRC / "bos" / "config"

FORBIDDEN_OUTER = ("bos.gateway", "bos.cli", "bos.runner", "bos.extensions", "bos.exts")


def _resolve(path: Path, level: int, module: str) -> str:
    pkg = path.relative_to(SRC).with_suffix("").parts[:-1]  # containing package
    base = pkg[: len(pkg) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve(path, node.level, node.module or "") if node.level else (node.module or "")
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        else:
            continue
        found.append((node.lineno, module))
    return found


def _is_private_core_reach(module: str) -> bool:
    # A reach into an inner ring's privates: a bos.core-rooted import whose path has
    # an underscore-prefixed segment (e.g. bos.core._utils, bos.core.agent._content).
    if not (module == "bos.core" or module.startswith("bos.core")):
        return False
    return any(seg.startswith("_") and not seg.startswith("__") for seg in module.split("."))


def test_config_ring_has_no_outward_imports() -> None:
    offenders: list[str] = []
    for py in sorted(CONFIG_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if any(module == ring or module.startswith(ring + ".") for ring in FORBIDDEN_OUTER):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.config is a consumer of the harness and must not import any outer ring "
        "(gateway/cli/runner/extensions/exts) (BEP 13 §3.2):\n  " + "\n  ".join(offenders)
    )


def test_config_ring_uses_published_api_only() -> None:
    offenders: list[str] = []
    for py in sorted(CONFIG_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if _is_private_core_reach(module):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.config must import bos.core (and the foundations) through their published API, "
        "not an underscore-prefixed private module (BEP 13 §1.6 rule 4):\n  " + "\n  ".join(offenders)
    )
