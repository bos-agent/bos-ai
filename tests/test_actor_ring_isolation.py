"""Guard: ``bos.core.actor`` is a zero-dependency system foundation (BEP 13 §2).

A peer of the agent ring: it imports the stdlib and itself **only** — not
``bos.protocol``, not ``bos.core.agent``, not the harness/outer rings. The
dependency points the other way (``bos.protocol`` re-exports ``Envelope``/
``MessageType`` *from* this ring), so the actor foundation can be lifted out to
build other long-lived component systems with no agent/conversation baggage.

Static (AST) check, resolving relative imports to absolute so an escaping
``from ..agent import`` or ``from ..contract import`` is caught, not skipped.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
ACTOR_DIR = SRC / "bos" / "core" / "actor"

_ALLOWED_TOP = set(sys.stdlib_module_names) | {"__future__"}


def _resolve(path: Path, level: int, module: str) -> str:
    pkg = path.relative_to(SRC).with_suffix("").parts[:-1]  # containing package
    base = pkg[: len(pkg) - (level - 1)]
    return ".".join([*base, module]) if module else ".".join(base)


def _offending_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve(path, node.level, node.module or "") if node.level else (node.module or "")
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        else:
            continue
        if module.startswith("bos.core.actor") or module.split(".")[0] in _ALLOWED_TOP:
            continue
        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: imports {module!r}")
    return offenders


def test_actor_ring_imports_only_stdlib_and_itself() -> None:
    offenders: list[str] = []
    for py in sorted(ACTOR_DIR.rglob("*.py")):
        offenders.extend(_offending_imports(py))
    assert not offenders, (
        "bos.core.actor is the system foundation and must depend only on stdlib + itself "
        "(BEP 13 §2):\n  " + "\n  ".join(offenders)
    )
