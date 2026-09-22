"""bos.exts degrades when a built-in adapter's optional dependency is absent (BEP 16 §3.4)."""

import importlib
import logging
import sys

from conftest import BlockImport

from bos.exts import _optional

_KNOWLEDGE = "bos.extensions.tools.knowledge"


class TestOptionalExtension:
    def test_missing_dependency_warns_naming_the_extra_and_does_not_raise(self, caplog, monkeypatch):
        monkeypatch.setattr(sys, "meta_path", [BlockImport("ddgs"), *sys.meta_path])
        for cached in (_KNOWLEDGE, "ddgs", "ddgs.exceptions"):
            monkeypatch.delitem(sys.modules, cached, raising=False)

        with caplog.at_level(logging.WARNING, logger="bos.exts"):
            _optional(_KNOWLEDGE, extra="search")

        assert _KNOWLEDGE in caplog.text
        assert "ddgs" in caplog.text
        assert "bos-ai[search]" in caplog.text
        assert _KNOWLEDGE not in sys.modules

    def test_present_dependency_loads_the_module_without_a_missing_dep_warning(self, caplog):
        sys.modules.pop(_KNOWLEDGE, None)

        with caplog.at_level(logging.WARNING, logger="bos.exts"):
            _optional(_KNOWLEDGE, extra="search")

        assert _KNOWLEDGE in sys.modules
        # Asserting on this warning's own text, not on global log silence:
        # re-importing the module re-runs its @ep_tool decorators, which the
        # registry logs as an overwrite on the "bos" logger.
        assert "not loaded" not in caplog.text
        assert "bos-ai[" not in caplog.text

    def test_importing_bos_exts_still_registers_builtin_adapters(self):
        importlib.import_module("bos.exts")
        from bos.core.contract import ep_chat_store

        assert ep_chat_store.has("InMemChatStore")


def test_no_module_imports_aiohttp() -> None:
    """aiohttp left the repository (BEP 17 §2.1.4). One HTTP stack, server and
    client — a second one reintroduced here is 11 MB and two sets of semantics
    for the same job."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for py in sorted([*(root / "src").rglob("*.py"), *(root / "tests").rglob("*.py")]):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            else:
                continue
            if module == "aiohttp" or module.startswith("aiohttp."):
                offenders.append(f"{py.relative_to(root)}:{node.lineno}")

    assert not offenders, "aiohttp is gone; these still import it:\n  " + "\n  ".join(offenders)
