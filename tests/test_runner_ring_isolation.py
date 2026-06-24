"""Guard: ``bos.runner`` is a focused process supervisor — it imports only inward (BEP 13 §3.4).

``bos.runner`` launches and supervises the gateway process (start/stop, PID/lock
files, background spawn). It sits outward of `config` and inward of `cli`: it
imports `bos.core`/`bos.gateway`/`bos.config`, and **`cli` imports it**, never the
reverse. Its ring discipline:

1. **No outward / sideways imports.** Nothing under `bos/runner/` may import
   ``bos.cli`` (the outermost entrypoint), ``bos.extensions`` (adapters), or
   ``bos.exts`` (composition root). It is a supervisor, not the composition root.
2. **No inner-ring private reaches.** It imports ``bos.core``/``bos.gateway``/
   ``bos.config`` through their published API, never an underscore-prefixed private
   module (BEP 13 §1.6 rule 4).

Static (AST) check, resolving relative imports to absolute.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
RUNNER_DIR = SRC / "bos" / "runner"

FORBIDDEN_OUTER = ("bos.cli", "bos.extensions", "bos.exts")
INNER_RINGS = ("bos.core", "bos.gateway", "bos.config")


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


def _is_private_inner_reach(module: str) -> bool:
    if not any(module == ring or module.startswith(ring + ".") for ring in INNER_RINGS):
        return False
    return any(seg.startswith("_") and not seg.startswith("__") for seg in module.split("."))


def test_runner_ring_has_no_outward_imports() -> None:
    offenders: list[str] = []
    for py in sorted(RUNNER_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if any(module == ring or module.startswith(ring + ".") for ring in FORBIDDEN_OUTER):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.runner is a process supervisor and must not import bos.cli, bos.extensions, or bos.exts "
        "(BEP 13 §3.4):\n  " + "\n  ".join(offenders)
    )


def test_runner_ring_uses_published_api_only() -> None:
    offenders: list[str] = []
    for py in sorted(RUNNER_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if _is_private_inner_reach(module):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.runner must import bos.core/bos.gateway/bos.config through their published API, "
        "not an underscore-prefixed private module (BEP 13 §1.6 rule 4):\n  " + "\n  ".join(offenders)
    )
