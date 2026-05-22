"""Integration tests for boscli ask --interactive wiring (no Textual launch)."""

import asyncio

import pytest

from bos.cli.local_client import LocalClient
from bos.config.workspace import Workspace, WorkspaceResolutionError, resolve_config_source
from bos.core import _get_bos_home


def test_resolve_config_source_builtin_preset():
    """resolve_config_source with a preset name resolves to the built-in preset."""
    config_path, bos_dir, config = resolve_config_source("default")
    assert config_path.name == "default.toml"
    assert config_path.exists()
    assert isinstance(config, dict)


def test_resolve_config_source_unknown_name_raises():
    """resolve_config_source with an unknown name raises WorkspaceResolutionError."""
    with pytest.raises(WorkspaceResolutionError, match="Unknown config source"):
        resolve_config_source("nonexistent-config-zzz")


@pytest.mark.asyncio
async def test_interactive_wiring_harness_to_client(tmp_path):
    """Verify harness + ActorActor + LocalClient wiring without Textual."""
    from bos.core.actor import AgentActor
    from bos.core.chat_state import ChatState

    # Write a minimal config
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "test-agent"

[platform]
extensions = ["bos.exts"]

[[platform.agents]]
name = "test-agent"
system_prompt = "Test"
tools = []
skills = []
subagents = []

[harness.consolidator]
model = "test/consolidator"
""".strip(),
        encoding="utf-8",
    )

    ws = Workspace.from_discovery(str(tmp_path))
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = await harness.create_agent("test-agent")
        actor_mbox = harness.mail_route.bind("agent@main")
        client_mbox = harness.mail_route.bind("client@local")

        chat_state = ChatState(ws.bos_dir)
        actor = AgentActor(agent, actor_mbox, chat_state=chat_state)
        client = LocalClient(
            client_id="local:test",
            client_mbox=client_mbox,
            chat_state=chat_state,
        )

        actor_task = asyncio.create_task(actor.run())

        await client.connect()
        chat_id = client.chat_id
        assert chat_id is not None

        # Client can send and receive
        await client.send("hello", chat_id=chat_id)

        # Let the actor process it briefly (in real flow this would return via LLM)
        await asyncio.sleep(0.1)

        actor_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await actor_task

        await client.aclose()


def test_build_workspace_for_ask_falls_back_to_default_preset(tmp_path, monkeypatch):
    """When no workspace is found and no -c given, ask falls back to the 'default' preset."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    # Ensure no workspace discovery succeeds
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    ctx = type("Ctx", (), {"obj": {}})

    ws = _build_workspace_for_ask(ctx)
    assert ws.bos_dir == _get_bos_home() / "agents" / "default"
    assert isinstance(ws.config, dict)
    assert ws.config_file.name == "default.toml"


def test_build_workspace_for_ask_uses_discovery_when_workspace_exists(tmp_path, monkeypatch):
    """When a workspace config is discoverable, ask uses it (no fallback)."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        '[main]\nagent = "discovered-agent"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    ctx = type("Ctx", (), {"obj": {}})
    ws = _build_workspace_for_ask(ctx)

    assert ws.get_main_agent_name() == "discovered-agent"
    assert ws.bos_dir == bos_dir.resolve()


def test_build_workspace_for_ask_workspace_override(tmp_path, monkeypatch):
    """-w/--workspace overrides the workspace directory."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    override_dir = tmp_path / "override"
    override_dir.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    ctx = type("Ctx", (), {"obj": {}})
    ws = _build_workspace_for_ask(ctx, workspace_override=str(override_dir))

    assert ws.workspace == override_dir.resolve()
