from click.testing import CliRunner

from bos.cli.entry import cli


def test_cli_init_creates_bos_toml_by_default(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init"])

    assert result.exit_code == 0
    assert f"Initialized BOS workspace at {workspace}" in result.output
    assert (workspace / "bos.toml").exists()
    assert not (workspace / ".bos").exists()


def test_cli_init_dotbos_creates_dot_bos_layout(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init", "--dotbos"])

    assert result.exit_code == 0
    assert f"Initialized BOS workspace at {workspace / '.bos'}" in result.output
    assert (workspace / ".bos" / "config.toml").exists()
    assert not (workspace / "bos.toml").exists()


def test_cli_init_rejects_already_initialized_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init"])

    assert result.exit_code != 0
    assert "already initialized" in result.output


def test_cli_init_rejects_already_initialized_dotbos(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".bos").mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init"])

    assert result.exit_code != 0
    assert "already initialized" in result.output


def test_cli_init_git_creates_repo_and_gitignore(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "init", "--git"])

    assert result.exit_code == 0
    assert (workspace / ".git").is_dir()
    gitignore = workspace / ".gitignore"
    assert gitignore.exists()
    assert gitignore.read_text(encoding="utf-8") == ".env\nrun\n"


def test_cli_status_fails_cleanly_without_workspace_or_bos_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_DIR", raising=False)

    result = CliRunner().invoke(cli, ["--workspace", str(workspace), "status"])

    assert result.exit_code == 2
    assert "No BOS workspace found" in result.output
