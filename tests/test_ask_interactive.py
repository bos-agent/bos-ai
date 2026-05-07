"""Integration tests for bos ask --interactive wiring (no Textual launch)."""

import asyncio

import pytest

from bos.cli.commands.agent import _resolve_whom
from bos.cli.local_client import LocalClient
from bos.config.workspace import Workspace


def test_resolve_whom_builtin_preset():
    """--whom with a name resolves to the built-in preset."""
    result = _resolve_whom("default")
    assert result.name == "default.toml"
    assert result.exists()


def test_resolve_whom_unknown_name_raises():
    """--whom with an unknown name raises UsageError."""
    import click

    with pytest.raises(click.UsageError, match="Unknown config"):
        _resolve_whom("nonexistent-config-zzz")


@pytest.mark.asyncio
async def test_interactive_wiring_harness_to_client(tmp_path):
    """Verify harness + ActorActor + LocalClient wiring without Textual."""
    from bos.core.actor import AgentActor
    from bos.core.chat_state import ChatState
    from bos.named_actors.registry import ActorRegistry

    # Write a minimal config
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "test-agent"

[platform]
extensions = ["bos.extensions.all"]

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

    ws = Workspace(str(tmp_path))
    ws.bootstrap_platform()

    async with ws.harness() as harness:
        agent = harness.create_agent("test-agent")
        actor_mbox = harness.mail_route.bind("agent@main")
        client_mbox = harness.mail_route.bind("client@local")

        registry = ActorRegistry()
        registry.register("main", actor_mbox, is_default=True)

        chat_state = ChatState(ws.bos_dir)
        actor = AgentActor(agent, actor_mbox, chat_state=chat_state)
        client = LocalClient(
            client_id="local:test",
            client_mbox=client_mbox,
            registry=registry,
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
