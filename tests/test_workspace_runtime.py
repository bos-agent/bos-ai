import pytest

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


def test_main_agent_address_is_stable_even_when_selecting_different_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text("[main]\nagent = \"research\"\n", encoding="utf-8")

    ws = Workspace(tmp_path)

    assert ws.get_main_agent_name() == "research"
    assert ws.get_main_agent_address() == "agent@main"


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


def test_resolve_channels_uses_explicit_bind_and_target_addresses(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.channels]]
name = "BroadcastChannel"
bind_address = "channel@group"
target_address = "agent@main"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "channel@group"
port = 8080
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channels = Workspace(tmp_path).resolve_channels()

    assert [(channel.name, channel.bind_address, channel.target_address) for channel in channels] == [
        ("BroadcastChannel", "channel@group", "agent@main"),
        ("HttpChannel", "channel@http", "channel@group"),
    ]
    assert channels[1].options["port"] == 8080


def test_resolve_channels_rejects_broadcast_to_broadcast_topology(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.channels]]
name = "BroadcastChannel"
bind_address = "channel@group-a"
target_address = "channel@group-b"

[[main.channels]]
name = "BroadcastChannel"
bind_address = "channel@group-b"
target_address = "agent@main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="BroadcastChannel.*must target an actor address"):
        Workspace(tmp_path).resolve_channels()


def test_resolve_channels_rejects_leaf_targeting_non_broadcast_channel(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"

[[main.channels]]
name = "TelegramChannel"
bind_address = "channel@telegram"
target_address = "channel@http"
token = "x"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must target an actor or BroadcastChannel"):
        Workspace(tmp_path).resolve_channels()


def test_resolve_channels_accepts_agent_main_for_non_default_selected_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "research"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channels = Workspace(tmp_path).resolve_channels()

    assert channels[0].target_address == "agent@main"
