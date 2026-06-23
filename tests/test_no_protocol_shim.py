"""Guard: the ``bos.protocol`` migration shim is retired (BEP 13 Track A).

The shim re-exported foundation wire types for legacy ``from bos.protocol
import …`` call sites and was always slated for deletion. Now that every
consumer imports the owning foundation directly (``bos.core.agent`` /
``bos.core.actor``) — and the content helpers / WS-takeover constants have
moved to their real homes — the package is gone. This guard fails CI if either
the package or an import of it is reintroduced.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "bos"
TESTS = Path(__file__).resolve().parent


def test_protocol_package_does_not_exist() -> None:
    assert not (SRC / "protocol").exists(), "bos.protocol was deleted in BEP 13 Track A — do not reintroduce it"
    assert importlib.util.find_spec("bos.protocol") is None, "bos.protocol must not be importable"


def test_no_source_or_test_imports_bos_protocol() -> None:
    offenders: list[str] = []
    for py in sorted([*SRC.rglob("*.py"), *TESTS.rglob("*.py")]):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            else:
                continue
            if module == "bos.protocol" or module.startswith("bos.protocol."):
                offenders.append(f"{py.name}:{node.lineno}: imports {module!r}")
    assert not offenders, (
        "bos.protocol is retired (BEP 13 Track A); import the owning foundation directly:\n  "
        + "\n  ".join(offenders)
    )
