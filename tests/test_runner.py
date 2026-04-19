from bos.config.workspace import Workspace


def test_resolved_http_channel_binds_all_interfaces_in_docker(tmp_path):
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
host = "127.0.0.1"
port = 8080
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channel = Workspace(tmp_path).resolve_channels(runtime_kind="docker")[0]

    assert channel.options["host"] == "0.0.0.0"


def test_non_http_channel_config_is_unchanged(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.channels]]
name = "TelegramChannel"
bind_address = "channel@telegram"
target_address = "agent@main"
token = "x"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channel = Workspace(tmp_path).resolve_channels(runtime_kind="docker")[0]

    assert channel.options == {"token": "x"}
