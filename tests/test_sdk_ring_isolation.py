"""Guard: ``bos.sdk`` is the embedding SDK — it imports only inward (BEP 18 §3.9).

``bos.sdk`` (``open_harness``, the bootstrap sequence) is the embedding contract:
process entrypoints and embedders call into it, and it calls into ``bos.core``
(``AgentHarness`` and friends) and ``bos.config`` (``Workspace``); neither imports
it back. It is therefore a ring *outward* of both — just inside the process
entrypoints. Its ring discipline is that every source dependency points inward:

1. **No outward imports.** Nothing under ``bos/sdk/`` may import ``bos.gateway`` /
   ``bos.runner`` / ``bos.cli`` (process entrypoints/hosts), ``bos.extensions``
   (adapters), or ``bos.exts`` (composition root). Importing ``bos.core`` and
   ``bos.config`` is the legal inward direction.
2. **No inner-ring private reaches.** It imports ``bos.core`` and ``bos.config``
   through their published API, never an underscore-prefixed private module such
   as ``bos.core._utils`` or ``bos.core.agent._content`` (BEP 13 §1.6 rule 4).
   The ``_``-prefixed *helpers* it needs are re-exported from ``bos.core`` itself.

Static (AST) check, resolving relative imports to absolute so an escaping
``from ..runner import`` or ``from bos.core._utils import`` is caught, not skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
SDK_DIR = SRC / "bos" / "sdk"

FORBIDDEN_OUTER = ("bos.gateway", "bos.runner", "bos.cli", "bos.extensions", "bos.exts")
INNER_RINGS = ("bos.core", "bos.config")


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
    # A reach into an inner ring's privates: a bos.core/bos.config-rooted import
    # whose path has an underscore-prefixed segment (e.g. bos.core._utils).
    if not any(module == ring or module.startswith(ring + ".") for ring in INNER_RINGS):
        return False
    return any(seg.startswith("_") and not seg.startswith("__") for seg in module.split("."))


def test_sdk_ring_has_no_outward_imports() -> None:
    offenders: list[str] = []
    for py in sorted(SDK_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if any(module == ring or module.startswith(ring + ".") for ring in FORBIDDEN_OUTER):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.sdk is the embedding SDK and must not import a process entrypoint, host or adapter ring "
        "(gateway/runner/cli/extensions/exts) (BEP 18 §3.9):\n  " + "\n  ".join(offenders)
    )


def test_sdk_ring_uses_published_api_only() -> None:
    offenders: list[str] = []
    for py in sorted(SDK_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if _is_private_inner_reach(module):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.sdk must import bos.core / bos.config through their published API, "
        "not an underscore-prefixed private module (BEP 13 §1.6 rule 4):\n  " + "\n  ".join(offenders)
    )
