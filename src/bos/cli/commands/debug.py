"""``boscli debug prompt`` — dev-only debugging commands (requires BOS_DEV=1)."""

from __future__ import annotations

import asyncio

import click


@click.group(name="debug")
def debug():
    """Dev-only debugging commands (BOS_DEV=1 required)."""


@debug.command()
@click.option(
    "--agent",
    "agent_kind",
    default=None,
    help="Agent kind (defaults to the configured main agent).",
)
@click.option(
    "-w",
    "--workspace",
    "workspace_dir",
    default=None,
    help="Override the workspace directory.",
)
@click.pass_context
def prompt(ctx, agent_kind: str | None, workspace_dir: str | None):
    """Print the full system prompt for an agent."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    ws = _build_workspace_for_ask(ctx, workspace_dir)
    ws.bootstrap_platform()
    selected = agent_kind or ws.get_main_agent_kind()

    async def _run() -> str:
        async with ws.harness() as harness:
            agent = await harness.create_agent(selected)
            return await agent._build_system_prompt()

    click.echo(asyncio.run(_run()))
