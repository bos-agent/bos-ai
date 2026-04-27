from click.testing import CliRunner

from bos.cli.entry import cli


def test_cli_init_targets_workspace_local_bos_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    fallback_bos_dir = tmp_path / "fallback-bos"
    workspace.mkdir()
    monkeypatch.setenv("BOS_DIR", str(fallback_bos_dir))

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init"])

    assert result.exit_code == 0
    assert f"Initialized BOS workspace at {workspace / '.bos'}" in result.output
    assert (workspace / ".bos" / "config.toml").exists()
    assert not fallback_bos_dir.exists()


def test_cli_status_fails_cleanly_without_workspace_or_bos_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "status"])

    assert result.exit_code == 2
    assert "No BOS workspace found" in result.output
