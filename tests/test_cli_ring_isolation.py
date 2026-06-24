"""Guard: ``bos.cli`` is the outermost ring — a leaf composition root (BEP 13 §3.4).

``bos.cli`` is the user-facing entrypoint and composition root: it loads a
``Workspace`` (via ``bos.config``), opens the harness, builds a
``GatewayRuntimeConfig``, drives the runner, and — as the composition root —
wires concrete adapters/plugins (it may import ``bos.exts``/``bos.extensions``/
``bos.plugins``). Two invariants define its place in the ring hierarchy:

1. **Nothing depends on it (it is a true leaf).** No module anywhere under
   ``bos/`` outside ``bos/cli/`` may import ``bos.cli`` — the entrypoint is the
   outermost ring, so an inward ring importing it would invert the hierarchy.
2. **No inner-ring private reaches.** It imports the inner rings
   (``bos.core``/``bos.gateway``/``bos.config``/``bos.runner``) through their
   published API, never an underscore-prefixed private module (BEP 13 §1.6 rule 4).

Static (AST) check, resolving relative imports to absolute.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
BOS = SRC / "bos"
CLI_DIR = BOS / "cli"

INNER_RINGS = ("bos.core", "bos.gateway", "bos.config", "bos.runner")


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


def test_nothing_imports_cli() -> None:
    offenders: list[str] = []
    for py in sorted(BOS.rglob("*.py")):
        if CLI_DIR in py.parents:
            continue  # cli importing itself is fine
        for lineno, module in _imports(py):
            if module == "bos.cli" or module.startswith("bos.cli."):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.cli is the outermost ring (a leaf entrypoint); no inner ring may import it (BEP 13 §3.4):\n  "
        + "\n  ".join(offenders)
    )


def test_cli_ring_uses_published_api_only() -> None:
    offenders: list[str] = []
    for py in sorted(CLI_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if _is_private_inner_reach(module):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.cli must import the inner rings through their published API, "
        "not an underscore-prefixed private module (BEP 13 §1.6 rule 4):\n  " + "\n  ".join(offenders)
    )
