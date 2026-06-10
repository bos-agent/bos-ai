"""``boscli debug prompt/messages`` — dev-only debugging commands (requires BOS_DEV=1)."""

from __future__ import annotations

import asyncio
import json

import click


@click.group(name="debug")
def debug():
    """Dev-only debugging commands (BOS_DEV=1 required)."""


def _resolve_debug_agent(ctx, agent_kind: str | None, workspace_dir: str | None):
    """Build the workspace and resolve the agent kind/config like the gateway would."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    ws = _build_workspace_for_ask(ctx, workspace_dir)
    ws.resolve_agents()
    ws.bootstrap_platform()
    agent_cfg = None
    if agent_kind:
        selected = agent_kind
    elif not ws.config.runtime or not ws.config.runtime.actors:
        selected = ws.get_main_agent_kind()
    else:
        actors = ws.resolve_gateway_actors()
        default_actor = ws.resolve_default_actor()
        actor = actors[default_actor]
        selected = actor.agent
        agent_cfg = dict(actor.agent_overrides)
        agent_cfg["agent_name"] = actor.name
        agent_cfg["history_attribution"] = len(actors) > 1
    return ws, selected, agent_cfg


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
    ws, selected, agent_cfg = _resolve_debug_agent(ctx, agent_kind, workspace_dir)

    async def _run() -> str:
        async with ws.harness() as harness:
            agent = await harness.create_agent(selected, agent_cfg=agent_cfg)
            return await agent._build_system_prompt()

    click.echo(asyncio.run(_run()))


@debug.command()
@click.argument("chat_id")
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
@click.option(
    "--system",
    "include_system",
    is_flag=True,
    default=False,
    help="Include the system prompt message.",
)
@click.pass_context
def messages(ctx, chat_id: str, agent_kind: str | None, workspace_dir: str | None, include_system: bool):
    """Print the projected LLM messages for a chat (as the next turn would see them)."""
    ws, selected, agent_cfg = _resolve_debug_agent(ctx, agent_kind, workspace_dir)

    async def _run() -> list[dict]:
        async with ws.harness() as harness:
            agent = await harness.create_agent(selected, agent_cfg=agent_cfg)
            history = await agent._load_and_compact_history(chat_id, budget_model=None)
            if include_system:
                system = await agent._build_system_prompt()
                return [{"role": "system", "content": system}] + history
            return history

    click.echo(json.dumps(asyncio.run(_run()), indent=2, default=str))
