"""Guard: ``bos.core`` is the assembly ring — it imports only inward (BEP 13 §3.1).

The assembly ring (harness, contract, registry, events, llm, ``defaults`` — and
the shared ``_``-prefixed helpers) wires the two foundations into a runnable
agent. Unlike the foundations it is **not** third-party-free; what defines it as
a ring is that every source dependency points *inward*:

1. **No outward imports.** Nothing under ``bos/core/`` may import an outer ring —
   ``bos.gateway`` / ``bos.cli`` / ``bos.runner`` (process entrypoints),
   ``bos.extensions`` (adapter implementations, injected via ``ep_*``),
   ``bos.config`` (a *consumer* of the harness — it imports ``bos.core``, never
   the reverse), or ``bos.exts`` (the composition root). Adapters are injected
   inward at runtime; the ring never names them at import time.
2. **No foundation-private reaches.** The assembly ring imports the foundations
   through their package API (``bos.core.agent`` / ``bos.core.actor``), never a
   private submodule path like ``bos.core.agent._utils`` (BEP 13 §1.6 rule 4).

The two foundation subpackages (``core/agent``, ``core/actor``) are excluded —
they carry their own stricter zero-dependency guards. Static (AST) check,
resolving relative imports to absolute so an escaping ``from ..gateway import``
or ``from .agent._utils import`` is caught, not skipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
CORE_DIR = SRC / "bos" / "core"
FOUNDATIONS = (CORE_DIR / "agent", CORE_DIR / "actor")

# Outer rings the assembly ring must never import (rule 1).
FORBIDDEN_OUTER = ("bos.gateway", "bos.cli", "bos.runner", "bos.extensions", "bos.config", "bos.exts")
# Foundation package roots; deeper paths are private leaves the ring must not reach (rule 4).
FOUNDATION_PKGS = ("bos.core.agent", "bos.core.actor")


def _resolve(path: Path, level: int, module: str) -> str:
    pkg = path.relative_to(SRC).with_suffix("").parts[:-1]  # containing package
    base = pkg[: len(pkg) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def _assembly_files() -> list[Path]:
    return [
        py
        for py in sorted(CORE_DIR.rglob("*.py"))
        if not any(found in py.parents for found in FOUNDATIONS)
    ]


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


def test_assembly_ring_has_no_outward_imports() -> None:
    offenders: list[str] = []
    for py in _assembly_files():
        for lineno, module in _imports(py):
            if any(module == ring or module.startswith(ring + ".") for ring in FORBIDDEN_OUTER):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.core is the assembly ring and must not import any outer ring "
        "(gateway/cli/runner/extensions/config/exts) — adapters are injected inward (BEP 13 §3.1):\n  "
        + "\n  ".join(offenders)
    )


def test_assembly_ring_uses_foundation_package_api_only() -> None:
    offenders: list[str] = []
    for py in _assembly_files():
        for lineno, module in _imports(py):
            if any(module.startswith(pkg + ".") for pkg in FOUNDATION_PKGS):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.core must import the foundations through their package API "
        "(bos.core.agent / bos.core.actor), not a private submodule (BEP 13 §1.6 rule 4):\n  "
        + "\n  ".join(offenders)
    )
