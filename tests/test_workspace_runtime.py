from bos.config.workspace import Workspace


def test_runtime_config_defaults_to_process(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text("", encoding="utf-8")

    ws = Workspace(tmp_path)
    runtime = ws.get_runtime_config()

    assert runtime.kind == "process"
    assert runtime.workspace_dir == "/workspace"
    assert runtime.bos_dir == "/workspace/.bos"


def test_runtime_config_mounts_external_bos_dir_outside_workspace(tmp_path):
    repo = tmp_path / "repo"
    workspace = repo / "app"
    bos_dir = repo / ".bos"
    workspace.mkdir(parents=True)
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text("", encoding="utf-8")

    ws = Workspace(workspace)
    runtime = ws.get_runtime_config()

    assert runtime.kind == "process"
    assert runtime.bos_dir == "/bos"


def test_resolve_platform_envfile_from_bos_dir(tmp_path):
    bos_dir = tmp_path / ".bos"
    env_dir = tmp_path / "env"
    bos_dir.mkdir()
    env_dir.mkdir()
    env_file = env_dir / "agent.env"
    env_file.write_text("BOT_TOKEN=test\n", encoding="utf-8")
    (bos_dir / "config.toml").write_text('[platform]\nenvfile = "../env/agent.env"\n', encoding="utf-8")

    ws = Workspace(tmp_path)

    assert ws.resolve_platform_envfile() == env_file.resolve()
