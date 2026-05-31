import sys

from bos.config.workspace import Workspace


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
