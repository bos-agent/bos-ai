from pathlib import Path

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


def test_resolve_actors_defaults_to_single_coordinator(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text("[main]\nagent = \"research\"\n", encoding="utf-8")

    actors = Workspace(tmp_path).resolve_actors()

    assert [(actor.name, actor.agent, actor.address, actor.role) for actor in actors] == [
        ("main", "research", "agent@main", "coordinator"),
    ]


def test_resolve_actors_loads_configured_workers(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.actors]]
name = "researcher"
agent = "researcher"
description = "Research worker"
capabilities = ["research"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    actors = Workspace(tmp_path).resolve_actors()

    assert [(actor.name, actor.agent, actor.address, actor.role) for actor in actors] == [
        ("main", "main", "agent@main", "coordinator"),
        ("researcher", "researcher", "agent@researcher", "worker"),
    ]
    assert actors[1].description == "Research worker"


def test_resolve_actors_requires_configured_main_actor_without_main_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[[main.actors]]
name = "main"
agent = "orchestrator"
capabilities = ["triage"]

[[main.actors]]
name = "researcher"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    actors = Workspace(tmp_path).resolve_actors()

    assert [(actor.name, actor.agent, actor.address, actor.role) for actor in actors] == [
        ("main", "orchestrator", "agent@main", "coordinator"),
        ("researcher", "researcher", "agent@researcher", "worker"),
    ]
    assert actors[0].capabilities == ["triage"]


def test_resolve_actors_rejects_duplicate_actor_address(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.actors]]
name = "other"
address = "agent@main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate actor address"):
        Workspace(tmp_path).resolve_actors()


def test_resolve_actors_rejects_role_config(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.actors]]
role = "coordinator"
name = "backup"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="role is derived"):
        Workspace(tmp_path).resolve_actors()


def test_resolve_actors_rejects_missing_main_actor_without_main_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[[main.actors]]
name = "researcher"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly one actor named 'main'"):
        Workspace(tmp_path).resolve_actors()


def test_resolve_actors_rejects_missing_actor_topology_without_main_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="main.agent is required"):
        Workspace(tmp_path).resolve_actors()


def test_resolve_actors_rejects_configured_main_actor_with_main_agent(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.actors]]
name = "main"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not include 'main'"):
        Workspace(tmp_path).resolve_actors()


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
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
port = 8080
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channels = Workspace(tmp_path).resolve_channels()

    assert [(channel.name, channel.bind_address, channel.target_address) for channel in channels] == [
        ("HttpChannel", "channel@http", "agent@main"),
    ]
    assert channels[0].options["bos_dir"] == str(bos_dir)
    assert channels[0].options["workspace_dir"] == str(tmp_path.resolve())
    assert channels[0].options["bind_address"] == "channel@http"
    assert channels[0].options["port"] == 8080


def test_resolve_channels_passes_bos_dir_to_direct_channels(tmp_path):
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

    channels = Workspace(tmp_path).resolve_channels()

    assert channels[0].options["bos_dir"] == str(bos_dir)
    assert channels[0].options["workspace_dir"] == str(tmp_path.resolve())
    assert channels[0].options["bind_address"] == "channel@telegram"


def test_resolve_channels_rejects_broadcast_channel(tmp_path):
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
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="BroadcastChannel is no longer supported"):
        Workspace(tmp_path).resolve_channels()


def test_resolve_channels_rejects_channel_to_channel_topology(tmp_path):
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

    with pytest.raises(ValueError, match="channel-to-channel routing is no longer supported"):
        Workspace(tmp_path).resolve_channels()


def test_template_documents_manual_gemini_image_verification_and_phase_one_limits():
    template_path = Path(__file__).resolve().parents[1] / "src" / "bos" / "config" / "template.toml"
    template = template_path.read_text(encoding="utf-8")

    assert "manually verified for image understanding" in template
    assert "Automated phase-1 multimodal regression coverage still targets the `_default` provider" in template
    assert "PDF/file inputs remain unsupported in phase 1" in template


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


def test_resolve_channels_accepts_known_worker_actor_target(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[[main.actors]]
name = "main"

[[main.actors]]
name = "researcher"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@researcher"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channels = Workspace(tmp_path).resolve_channels()

    assert channels[0].target_address == "agent@researcher"


def test_resolve_channels_rejects_unknown_actor_target_with_multi_actor_config(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[[main.actors]]
name = "main"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@missing"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown actor address"):
        Workspace(tmp_path).resolve_channels()
