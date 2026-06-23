"""Guard: ``bos.gateway`` imports only inward (BEP 13 §3.3).

The gateway is *policy* — the actor/channel runtime that drives the agent. It
depends inward on ``bos.core`` (foundations + assembly ring) and owns the shape
of the configuration it consumes (``GatewayRuntimeConfig`` and the ``Resolved*``
value objects in ``gateway/config.py``). The configuration *loader* —
``bos.config`` — is an outer ring that imports those shapes and produces them;
the gateway never imports it. The composition root (``runner``/``cli``) builds a
``GatewayRuntimeConfig`` and injects it via ``Gateway(runtime=...)``.

Ring discipline (now guard-enforced):

1. **No outward imports.** Nothing under ``bos/gateway/`` may import
   ``bos.config`` (the loader), ``bos.runner``/``bos.cli`` (process entrypoints),
   ``bos.extensions`` (adapters; injected via ``ep_*``), or ``bos.exts``.
2. **No inner-ring private reaches.** It imports ``bos.core`` (and the
   foundations) through their published API, never an underscore-prefixed
   private module such as ``bos.core._utils`` (BEP 13 §1.6 rule 4).

Static (AST) check, resolving relative imports to absolute so an escaping
``from bos.config import`` or ``from bos.core._utils import`` is caught.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
GATEWAY_DIR = SRC / "bos" / "gateway"

FORBIDDEN_OUTER = ("bos.config", "bos.runner", "bos.cli", "bos.extensions", "bos.exts")


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
    if not module.startswith("bos.core"):
        return False
    return any(seg.startswith("_") and not seg.startswith("__") for seg in module.split("."))


def test_gateway_ring_has_no_outward_imports() -> None:
    offenders: list[str] = []
    for py in sorted(GATEWAY_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if any(module == ring or module.startswith(ring + ".") for ring in FORBIDDEN_OUTER):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.gateway is policy and must not import the config loader or any outer ring "
        "(config/runner/cli/extensions/exts) — its runtime config is injected (BEP 13 §3.3):\n  "
        + "\n  ".join(offenders)
    )


def test_gateway_ring_uses_published_api_only() -> None:
    offenders: list[str] = []
    for py in sorted(GATEWAY_DIR.rglob("*.py")):
        for lineno, module in _imports(py):
            if _is_private_core_reach(module):
                offenders.append(f"{py.relative_to(SRC)}:{lineno}: imports {module!r}")
    assert not offenders, (
        "bos.gateway must import bos.core (and the foundations) through their published API, "
        "not an underscore-prefixed private module (BEP 13 §1.6 rule 4):\n  " + "\n  ".join(offenders)
    )
