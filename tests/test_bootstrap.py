import sys

from bos.config.workspace import Workspace
from bos.core import ExtensionPoint


def test_workspace_bootstrap_loads_extensions_bundle(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        '[platform]\nextensions = ["bos.exts"]\n',
        encoding="utf-8",
    )

    previous_extensions = sys.modules.pop("bos.exts", None)

    try:
        Workspace.from_discovery(tmp_path).bootstrap_platform()
        assert "bos.exts" in sys.modules
    finally:
        sys.modules.pop("bos.exts", None)
        if previous_extensions is not None:
            sys.modules["bos.exts"] = previous_extensions


def test_exts_defaults_merge_resolves_any_registered_name(tmp_path):
    point = ExtensionPoint(name="custom_point_test", description="test point")

    @point(name="thing", defaults={"a": 1})
    def thing(**kwargs):
        return kwargs

    assert ExtensionPoint.lookup("custom_point_test") is point

    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        "[platform]\nextensions = []\n\n[exts.custom_point_test.thing]\nb = 2\n",
        encoding="utf-8",
    )
    Workspace.from_discovery(tmp_path).bootstrap_platform()
    assert point.get("thing").defaults == {"a": 1, "b": 2}
