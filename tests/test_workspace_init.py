import pytest

from bos.config.workspace import Workspace, WorkspaceResolutionError, initialize_workspace


def test_workspace_load_does_not_create_fallback_bos_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    fallback_bos_dir = tmp_path / "fallback-bos"
    workspace.mkdir()
    monkeypatch.setenv("BOS_DIR", str(fallback_bos_dir))

    ws = Workspace(workspace)

    assert ws.bos_dir == fallback_bos_dir.resolve()
    assert ws.config == {}
    assert not fallback_bos_dir.exists()


def test_workspace_load_requires_discovered_bos_dir_or_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    with pytest.raises(WorkspaceResolutionError, match="No BOS workspace found"):
        Workspace(workspace)


def test_workspace_load_rejects_conflicting_discovered_bos_dir_and_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    discovered_bos_dir = workspace / ".bos"
    fallback_bos_dir = tmp_path / "fallback-bos"
    workspace.mkdir()
    discovered_bos_dir.mkdir()
    monkeypatch.setenv("BOS_DIR", str(fallback_bos_dir))

    with pytest.raises(WorkspaceResolutionError, match="Ambiguous BOS config"):
        Workspace(workspace)


def test_workspace_load_allows_matching_discovered_bos_dir_and_bos_dir_env(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    discovered_bos_dir = workspace / ".bos"
    workspace.mkdir()
    discovered_bos_dir.mkdir()
    monkeypatch.setenv("BOS_DIR", str(discovered_bos_dir))

    ws = Workspace(workspace)

    assert ws.bos_dir == discovered_bos_dir.resolve()
    assert ws.config == {}


def test_initialize_workspace_targets_workspace_local_bos_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    fallback_bos_dir = tmp_path / "fallback-bos"
    workspace.mkdir()
    monkeypatch.setenv("BOS_DIR", str(fallback_bos_dir))

    bos_dir = initialize_workspace(workspace)
    monkeypatch.setenv("BOS_DIR", str(bos_dir))
    ws = Workspace(workspace)

    assert bos_dir == (workspace / ".bos").resolve()
    assert (bos_dir / "config.toml").exists()
    assert ws.bos_dir == bos_dir
    assert not fallback_bos_dir.exists()
