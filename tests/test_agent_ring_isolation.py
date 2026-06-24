"""Guard: ``bos.core.agent`` is the absolute innermost ring (BEP 13 §1.2).

It must import **only** the stdlib and its own package-internal modules — not
``bos.core.contract``/``harness``/``actor``, not any third-party package. This
keeps the agent core a standalone library that can be lifted out to build other
agent applications; outer rings depend on the agent core, never the reverse.

Static (AST) check rather than runtime import, because importing the submodule
would pull in ``bos.core.__init__`` (the public API surface) and defeat the
isolation it is meant to verify.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "src" / "bos" / "core" / "agent"

_ALLOWED_TOP = set(sys.stdlib_module_names) | {"__future__"}


def _offending_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import inside the package — allowed
            module = node.module or ""
        elif isinstance(node, ast.Import):
            module = node.names[0].name
        else:
            continue
        top = module.split(".")[0]
        if top in _ALLOWED_TOP or module.startswith("bos.core.agent"):
            continue
        offenders.append(f"{path.name}:{node.lineno}: imports {module!r}")
    return offenders


def test_agent_ring_imports_only_stdlib_and_itself() -> None:
    offenders: list[str] = []
    for py in sorted(AGENT_DIR.rglob("*.py")):
        offenders.extend(_offending_imports(py))
    assert not offenders, (
        "bos.core.agent must depend only on stdlib + itself (BEP 13 §1.2):\n  " + "\n  ".join(offenders)
    )
