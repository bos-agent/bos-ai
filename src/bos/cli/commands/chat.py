"""``td chat`` — Codex/Claude-Code-style TUI for agent interaction.

Uses Textual for a full-screen terminal UI and AgentActor + InMemMailbox
so the event loop is never blocked by the LLM.  A TUI interceptor feeds
real-time step info (thinking, tool calls, results) into the conversation log.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import click

from bos.core import (
    ReactAgent,
    Workspace,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  Oneshot mode (no TUI)
# ═══════════════════════════════════════════════════════════════


_ANSI_DIM = "\x1b[90m"
_ANSI_RESET = "\x1b[0m"
_ANSI_CLEAR_LINE = "\x1b[2K"


def _dim_text(text: str) -> str:
    return f"{_ANSI_DIM}{text}{_ANSI_RESET}"


def _truncate(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 3]}..."


async def _status_spinner(stop: asyncio.Event, model: str, preview: str) -> None:
    frames = ("|", "/", "-", "\\")
    idx = 0
    short = _truncate(preview, limit=60)
    while not stop.is_set():
        print(f"\r{_ANSI_CLEAR_LINE}{_dim_text(f'{frames[idx]} {model}: {short}')}", end="", flush=True)
        idx = (idx + 1) % len(frames)
        await asyncio.sleep(0.12)


async def _oneshot(agent: ReactAgent, message: str) -> int:
    """Send a single message, print the response, and exit (no TUI)."""
    try:
        stop = asyncio.Event()
        spinner = asyncio.create_task(_status_spinner(stop, agent._model, message))
        text = await agent.ask(uuid.uuid4().hex, message)
        stop.set()
        await spinner
        print(f"\r{_ANSI_CLEAR_LINE}", end="")
        print(text or "(no content returned)")
    except Exception as exc:
        stop.set()
        await spinner
        print(f"\r{_ANSI_CLEAR_LINE}", end="")
        logger.exception("Request failed")
        print(f"Error: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


# ═══════════════════════════════════════════════════════════════
#  Click command
# ═══════════════════════════════════════════════════════════════


@click.command()
@click.option("--model", "-m", help="Specify the LLM model to use.", default=None)
@click.option("--message", "-M", help="Send a single message and exit (oneshot mode).", default=None)
@click.option("--agent", "-a", help="Specify the agent name to use.", default=None)
@click.pass_context
def chat(ctx, model: str | None, message: str | None, agent: str | None):
    """Start an interactive chat with the AI agent."""

    workspace = ctx.obj.get("WORKSPACE", ".")
    ws = Workspace(workspace)
    ws.bootstrap_platform()

    agent_name = agent or ws.get_setting("cli.chat.agent")
    agent_cfg = {"model": model} if model else None

    if message:
        # Oneshot mode — no TUI
        async def _run_oneshot():
            async with ws.harness() as harness:
                return await _oneshot(harness.create_agent(agent_name, agent_cfg), message)

        try:
            raise SystemExit(asyncio.run(_run_oneshot()))
        except KeyboardInterrupt:
            pass
        return

    # Interactive TUI mode
    ws.enable_interceptors(["tui_interceptor"])
    from bos.cli.tui_app import run_chat_tui

    async def _run_tui():
        async with ws.harness() as harness:
            await run_chat_tui(harness.create_agent(agent_name, agent_cfg), harness.mailbox)

    asyncio.run(_run_tui())
