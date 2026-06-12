"""Tests for workspace discovery and initialization with BEP6 config."""
import pytest

from bos.config.workspace import Workspace, WorkspaceResolutionError, initialize_workspace


def test_workspace_load_does_not_create_fallback_bos_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    fallback_bos_dir = tmp_path / "fallback-bos"
    workspace.mkdir()
    monkeypatch.setenv("BOS_CONFIG", str(fallback_bos_dir / "config.toml"))

    ws = Workspace.from_discovery(workspace)

    assert ws.bos_dir == fallback_bos_dir.resolve()
    # Empty config validates to RootConfig with all fields default
    assert ws.config.platform is None
    assert ws.config.runtime is None
    assert not fallback_bos_dir.exists()


def test_workspace_load_requires_discovered_bos_dir_or_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)

    with pytest.raises(WorkspaceResolutionError, match="No BOS workspace found"):
        Workspace.from_discovery(workspace)


def test_workspace_load_rejects_conflicting_discovered_bos_dir_and_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    discovered_bos_dir = workspace / ".bos"
    fallback_bos_dir = tmp_path / "fallback-bos"
    workspace.mkdir()
    discovered_bos_dir.mkdir()
    (discovered_bos_dir / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("BOS_CONFIG", str(fallback_bos_dir / "config.toml"))

    with pytest.raises(WorkspaceResolutionError, match="Ambiguous BOS config"):
        Workspace.from_discovery(workspace)


def test_workspace_load_allows_matching_discovered_bos_dir_and_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    discovered_bos_dir = workspace / ".bos"
    workspace.mkdir()
    discovered_bos_dir.mkdir()
    monkeypatch.setenv("BOS_CONFIG", str(discovered_bos_dir / "config.toml"))

    ws = Workspace.from_discovery(workspace)

    assert ws.bos_dir == discovered_bos_dir.resolve()
    assert ws.config.platform is None


def test_initialize_workspace_creates_dot_bos_layout_by_default(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)

    bos_dir = initialize_workspace(workspace)
    ws = Workspace.from_discovery(workspace)

    assert bos_dir == (workspace / ".bos").resolve()
    assert (bos_dir / "config.toml").exists()
    assert not (workspace / "bos.toml").exists()
    assert ws.bos_dir == bos_dir


def test_initialize_workspace_flat_creates_bos_toml(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)

    bos_dir = initialize_workspace(workspace, dotbos=False)

    assert bos_dir == workspace.resolve()
    assert (workspace / "bos.toml").exists()
    assert not (workspace / ".bos").exists()


def test_initialize_workspace_rejects_already_initialized(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("BOS_CONFIG", raising=False)

    with pytest.raises(WorkspaceResolutionError, match="already initialized"):
        initialize_workspace(workspace)


# --- bos.toml discovery ---


def test_workspace_loads_from_bos_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "bos.toml").write_text(
        '[platform]\nextensions = ["bos.exts"]\n', encoding="utf-8"
    )

    ws = Workspace.from_discovery(workspace)

    assert ws.bos_dir == workspace.resolve()
    assert ws.config.platform is not None
    assert ws.config.platform.extensions == ["bos.exts"]


def test_workspace_bos_toml_sets_bos_dir_equal_to_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    workspace = tmp_path / "project"
    subdir = workspace / "sub"
    workspace.mkdir()
    subdir.mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")

    ws = Workspace.from_discovery(subdir)

    assert ws.bos_dir == workspace.resolve()


def test_workspace_rejects_both_dotbos_and_bos_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".bos").mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")

    with pytest.raises(WorkspaceResolutionError, match="found both .bos/ and bos.toml"):
        Workspace.from_discovery(workspace)


def test_workspace_bos_toml_allows_matching_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("BOS_CONFIG", str(workspace / "bos.toml"))

    ws = Workspace.from_discovery(workspace)

    assert ws.bos_dir == workspace.resolve()


def test_workspace_bos_toml_rejects_conflicting_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    other_dir = tmp_path / "other"
    workspace.mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("BOS_CONFIG", str(other_dir / "bos.toml"))

    with pytest.raises(WorkspaceResolutionError, match="Ambiguous BOS config"):
        Workspace.from_discovery(workspace)
