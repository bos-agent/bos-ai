from click.testing import CliRunner

from bos.cli.entry import cli


def test_cli_init_creates_bos_toml_by_default(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0
    assert f"Initialized BOS workspace at {workspace}" in result.output
    assert (workspace / "bos.toml").exists()
    assert not (workspace / ".bos").exists()


def test_cli_init_dotbos_creates_dot_bos_layout(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["init", "--dotbos"])

    assert result.exit_code == 0
    assert f"Initialized BOS workspace at {workspace / '.bos'}" in result.output
    assert (workspace / ".bos" / "config.toml").exists()
    assert not (workspace / "bos.toml").exists()


def test_cli_init_rejects_already_initialized_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "bos.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code != 0
    assert "already initialized" in result.output


def test_cli_init_rejects_already_initialized_dotbos(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    bos_dir = workspace / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code != 0
    assert "already initialized" in result.output


def test_cli_init_git_creates_repo_and_gitignore(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["init", "--git"])

    assert result.exit_code == 0
    assert (workspace / ".git").is_dir()
    gitignore = workspace / ".gitignore"
    assert gitignore.exists()
    assert gitignore.read_text(encoding="utf-8") == ".env\nrun\n"


def test_cli_status_without_workspace_uses_default_preset(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(cli, ["gateway", "status"])

    assert result.exit_code == 0
    assert "Gateway is not running." in result.output
    assert (tmp_path / "home" / "presets" / "default").is_dir()
