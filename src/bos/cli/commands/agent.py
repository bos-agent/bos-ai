"""``bos start/stop/status/restart/task/tui`` — agent process lifecycle commands."""

from __future__ import annotations

import asyncio
import getpass
import os
import re
import signal
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from bos.config import Workspace, WorkspaceResolutionError
from bos.protocol import TurnEvent


def _get_ws_and_rd(ctx):
    workspace = ctx.obj.get("WORKSPACE", ".")
    try:
        ws = Workspace(workspace)
    except WorkspaceResolutionError as exc:
        raise click.UsageError(str(exc)) from exc
    from bos.runner.proc import RunDir

    rd = RunDir(ws.bos_dir)
    return ws, rd


def _resolve_whom(whom: str) -> Path:
    """Resolve --whom value to a config file path.

    If it looks like a file path (contains / or ends with .toml), treat as
    a direct file path. Otherwise look up <name>.toml in the built-in presets.
    """
    from pathlib import Path

    if "/" in whom or "\\" in whom or whom.endswith(".toml"):
        p = Path(whom).expanduser()
        if not p.exists():
            raise click.UsageError(f"Config file not found: {p}")
        return p.resolve()

    presets_dir = Path(__file__).resolve().parent.parent.parent / "config" / "presets"
    preset = presets_dir / f"{whom}.toml"
    if not preset.exists():
        available = sorted(p.stem for p in presets_dir.glob("*.toml")) if presets_dir.exists() else []
        raise click.UsageError(
            f"Unknown config {whom!r}. Available presets: {', '.join(available) or 'none'}"
        )
    return preset


async def _build_agent_system_prompt(ws: Workspace, agent_name: str | None = None) -> str:
    ws.bootstrap_platform()
    selected_agent = agent_name or ws.get_main_agent_name()
    async with ws.harness() as harness:
        agent = harness.create_agent(selected_agent)
        return await agent._build_system_prompt()


async def _connect_tui_client(client) -> None:
    try:
        await client.connect()
        return
    except Exception as exc:
        if getattr(exc, "status", None) != 409:
            raise

    takeover = click.confirm(
        "Another interactive TUI client is already connected. Disconnect it and take over?",
        default=False,
    )
    if not takeover:
        raise click.Abort()
    await client.connect(takeover=True)


def _default_tui_client_id() -> str:
    try:
        username = getpass.getuser()
    except Exception:
        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    safe = re.sub(r"[^a-z0-9_.-]+", "-", username.strip().lower()).strip("-")
    return f"tui:{safe or 'local'}"


def _turn_event_label(event: TurnEvent) -> str:
    if event.parent_agent_name and event.agent_name and event.agent_name != event.parent_agent_name:
        return f"{event.parent_agent_name} -> {event.agent_name}"
    return event.agent_name or "agent"


def _preview(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


class _TaskProgressDisplay:
    """Compact live renderer for oneshot task turn events."""

    def __init__(self, *, max_rows: int = 5) -> None:
        self._console = Console(stderr=True)
        self._enabled = self._console.is_terminal
        self._live: Live | None = None
        self._rows: deque[tuple[str, str]] = deque(maxlen=max_rows)

    def __enter__(self) -> "_TaskProgressDisplay":
        if self._enabled:
            self._append("dim", "starting task…")
            self._live = Live(
                self._render(),
                console=self._console,
                refresh_per_second=8,
                transient=True,
                vertical_overflow="crop",
            )
            self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    async def emit(self, event: TurnEvent) -> None:
        if not self._enabled:
            return
        style, message = self._format_event(event)
        if not message:
            return
        self._append(style, message)
        if self._live is not None:
            self._live.update(self._render())

    def _append(self, style: str, message: str) -> None:
        self._rows.append((style, _preview(message, 160)))

    def _render(self) -> Panel:
        body = Text()
        for idx, (style, message) in enumerate(self._rows):
            if idx:
                body.append("\n")
            body.append(message, style=style)
        return Panel(body, title="bos ask", border_style="cyan", padding=(0, 1))

    def _format_event(self, event: TurnEvent) -> tuple[str, str]:
        label = _turn_event_label(event)

        if event.event_type == "turn" and event.phase == "start":
            return "dim", f"▶ {label} started"

        if event.event_type == "llm" and event.detail == "thinking":
            return "italic dim", f"🤔 {label} thinking…"

        if event.event_type == "llm" and event.detail == "tool_calls":
            calls = []
            for tc in event.tool_calls or []:
                args = tc.get("arguments") or {}
                args_str = ", ".join(f"{key}={value!r}" for key, value in args.items())
                calls.append(f"{tc.get('name', '?')}({args_str})")
            return "cyan", f"⚡ {label}: " + ("; ".join(calls) if calls else "tool call")

        if event.event_type == "tool" and event.detail == "tool_call":
            return "cyan", f"⚙ {label}: {event.tool_name or '?'} running…"

        if event.event_type == "tool" and event.detail == "tool_result":
            preview = _preview(event.content, 80)
            suffix = f" → {preview}" if preview else ""
            return "green", f"↳ {label}: {event.tool_name or '?'} done{suffix}"

        if event.event_type == "response" and event.detail == "final":
            return "bold green", "✓ final response ready"

        if event.detail == "max_iteration":
            return "yellow", f"⚠ {label} max iterations reached"

        if event.detail == "error":
            return "red", f"⚠ {label} error: {event.content or 'unknown error'}"

        return "", ""


async def _run_interactive(
    ws: "Workspace",
    agent_name: str,
    agent_cfg: dict | None,
    *,
    initial_message: str | None = None,
) -> None:
    """Run the agent in interactive mode with an in-process TUI."""
    import asyncio
    import getpass
    import os
    import re

    from bos.cli.local_client import LocalClient
    from bos.core.actor import AgentActor
    from bos.core.chat_state import ChatState
    from bos.named_actors.registry import ActorRegistry

    async with ws.harness() as harness:
        agent = harness.create_agent(agent_name, agent_cfg=agent_cfg)
        actor_mbox = harness.mail_route.bind("agent@main")
        client_mbox = harness.mail_route.bind("client@local")

        registry = ActorRegistry()
        registry.register("main", actor_mbox, is_default=True)

        try:
            username = getpass.getuser()
        except Exception:
            username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        safe = re.sub(r"[^a-z0-9_.-]+", "-", username.strip().lower()).strip("-")
        client_id = f"local:{safe or 'local'}"

        chat_state = ChatState(ws.bos_dir)
        actor = AgentActor(agent, actor_mbox, chat_state=chat_state)
        client = LocalClient(
            client_id=client_id,
            client_mbox=client_mbox,
            registry=registry,
            chat_state=chat_state,
        )

        await client.connect()

        from bos.cli.tui_app import run_chat_tui

        actor_task = asyncio.create_task(actor.run())

        # If an initial message is provided, send it first
        if initial_message:
            await client.send(initial_message, chat_id=client.chat_id)

        try:
            await run_chat_tui(client)
        finally:
            actor_task.cancel()
            try:
                await actor_task
            except asyncio.CancelledError:
                pass
            await client.aclose()


# ── bos prompt ────────────────────────────────────────────────


@click.command()
@click.option(
    "--agent",
    "agent_name",
    default=None,
    help="Agent name to show the prompt for. Use '0' for the default agent with all tools/skills.",
)
@click.pass_context
def prompt(ctx, agent_name: str | None):
    """Print the built system prompt for an agent.

    By default, shows the prompt for the configured main agent.
    Use --agent <name> to show the prompt for a specific agent.
    Use --agent 0 to show the default agent prompt with all available
    tools and skills.
    """
    ws, _ = _get_ws_and_rd(ctx)
    try:
        rendered_prompt = asyncio.run(
            _build_agent_system_prompt(
                ws,
                agent_name == "0" and "_default" or agent_name,
            )
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    click.echo(rendered_prompt, nl=False)


# ── bos ask ──────────────────────────────────────────────────


@click.command()
@click.argument("message", required=False)
@click.option(
    "--agent",
    "agent_name",
    default=None,
    help="Agent name to use (defaults to the configured main agent).",
)
@click.option(
    "--model",
    default=None,
    help="Override the model for this task.",
)
@click.option(
    "--stdin",
    "use_stdin",
    is_flag=True,
    default=False,
    help="Read task content from stdin (appended after MESSAGE if both given).",
)
@click.option(
    "--max-iterations",
    "max_iterations",
    type=int,
    default=None,
    help="Override the maximum number of ReAct iterations.",
)
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    default=False,
    help="Start an interactive chat session (in-process TUI, no background daemon).",
)
@click.option(
    "--whom",
    default=None,
    help="Path to a bos.toml config file, or name of a built-in preset.",
)
@click.option(
    "--inmem",
    is_flag=True,
    default=False,
    help="Replace mail route, message store, and memory with in-memory variants.",
)
@click.pass_context
def ask(
    ctx,
    message: str | None,
    agent_name: str | None,
    model: str | None,
    use_stdin: bool,
    max_iterations: int | None,
    interactive: bool,
    whom: str | None,
    inmem: bool,
):
    """Run a oneshot agent task or start an interactive chat session.

    \b
    Examples:
        bos ask "refactor the auth module"
        bos ask -i
        bos ask -i "write tests for utils.py"
        bos ask -i --whom default
        cat spec.md | bos ask --stdin
    """
    if use_stdin and not sys.stdin.isatty():
        stdin_content = sys.stdin.read()
        message = ((message or "") + "\n" + stdin_content).strip() if message else stdin_content.strip()

    if not interactive and not message:
        raise click.UsageError("Provide a task message, use --stdin, or use -i for interactive mode.")

    config_source = _resolve_whom(whom) if whom else None
    ws = Workspace(ctx.obj.get("WORKSPACE", "."), config_source=config_source)
    if inmem:
        ws.config.setdefault("harness", {})
        ws.config["harness"]["mail_route"] = {"name": "InMemMailRoute"}
        ws.config["harness"]["message_store"] = {"name": "InMemMessageStore"}
        ws.config["harness"]["memory"] = {"name": "InMemMemoryExtension"}
    ws.bootstrap_platform()
    selected_agent = agent_name or ws.get_main_agent_name()

    llm_args: dict = {}
    if model:
        llm_args["model"] = model

    if interactive:
        agent_cfg = {"max_iterations": max_iterations} if max_iterations is not None else None
        try:
            asyncio.run(_run_interactive(ws, selected_agent, agent_cfg, initial_message=message))
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        return

    # One-shot mode (unchanged from current logic)
    async def _run(event_sink: _TaskProgressDisplay | None = None) -> str:
        agent_cfg = {"max_iterations": max_iterations} if max_iterations is not None else None
        async with ws.harness() as harness:
            agent = harness.create_agent(selected_agent, agent_cfg=agent_cfg)
            return await agent.ask(
                uuid.uuid4().hex,
                message,
                llm_args=llm_args or None,
                event_sink=event_sink,
            )

    try:
        with _TaskProgressDisplay() as progress:
            result = asyncio.run(_run(progress))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    click.echo(result)


# ── bos start ─────────────────────────────────────────────────


@click.command()
@click.option("--foreground", "-f", is_flag=True, default=False, help="Run in the foreground (don't daemonize).")
@click.option("--docker", is_flag=True, default=False, help="Run the agent inside a Docker container.")
@click.pass_context
def start(ctx, foreground: bool, docker: bool):
    """Start the agent actor and channel server."""
    ws, rd = _get_ws_and_rd(ctx)
    ws.bootstrap_platform()

    from bos.named_actors.runner import start_named_actors
    from bos.runner.proc import is_running, read_state, run_docker_foreground, start_background, start_docker

    if is_running(rd):
        state = read_state(rd)
        identifier = state.get("container_id") if state.get("runtime") == "docker" else state.get("pid")
        click.echo(f"Agent is already running ({state.get('runtime', 'process')} {identifier}).", err=True)
        raise SystemExit(1)

    runtime = ws.get_runtime_config(force_kind="docker" if docker else None)

    if runtime.kind not in {"process", "docker"}:
        raise click.UsageError(f"Unsupported runtime kind: {runtime.kind!r}")

    if runtime.kind == "docker":
        if foreground:
            click.echo("Starting agent in Docker foreground…")
            raise SystemExit(run_docker_foreground(ws, runtime))

        container_id = start_docker(ws, rd, runtime)
        click.echo(f"Agent starting in Docker ({container_id[:12]})…")
    elif foreground:
        click.echo("Starting agent in foreground…")
        asyncio.run(start_named_actors(ws))
        return
    else:
        argv = [sys.executable, "-m", "bos.runner._main", "--workspace", str(ws.workspace)]
        pid = start_background(argv, rd)
        click.echo(f"Agent starting (PID {pid})…")

    state = read_state(rd)
    pid = state.get("pid")
    container_id = state.get("container_id")

    # Poll agent.state until channels are registered (up to 10s)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.3)
        state = read_state(rd)
        channels = state.get("channels", [])
        if channels:
            for ch in channels:
                if ch.get("name") == "HttpChannel":
                    host = ch.get("host", "127.0.0.1")
                    port = ch.get("port")
                    if state.get("runtime") == "docker" and host == "0.0.0.0":
                        host = "127.0.0.1"
                    ident = container_id[:12] if container_id else pid
                    click.echo(f"Agent started ({state.get('runtime', 'process')} {ident}) · ws://{host}:{port}/ws")
                    return
            ident = container_id[:12] if container_id else pid
            click.echo(f"Agent started ({state.get('runtime', 'process')} {ident})")
            return

    ident = container_id[:12] if container_id else pid
    click.echo(f"Agent started ({runtime.kind} {ident}) — channel info not yet available (check bos status)")


# ── bos stop ──────────────────────────────────────────────────


@click.command()
@click.pass_context
def stop(ctx):
    """Stop the running agent."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state, stop_agent

    if not is_running(rd):
        click.echo("No agent is running.", err=True)
        raise SystemExit(1)

    state = read_state(rd)
    runtime = state.get("runtime", "process")
    ident = state.get("container_id", "?")[:12] if runtime == "docker" else state.get("pid", "?")
    click.echo(f"Stopping agent ({runtime} {ident})…")

    stop_agent(rd, signal.SIGTERM)

    # Wait up to 5s for clean exit
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if not is_running(rd):
            break
    else:
        click.echo("Agent did not exit cleanly — sending SIGKILL")
        try:
            stop_agent(rd, signal.SIGKILL)
        except Exception:
            pass

    # Clean up state files if process left them behind
    rd.pid_file.unlink(missing_ok=True)
    rd.state_file.unlink(missing_ok=True)
    click.echo("Agent stopped.")


# ── bos status ────────────────────────────────────────────────


@click.command()
@click.pass_context
def status(ctx):
    """Show agent running status."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state

    state = read_state(rd)
    running = is_running(rd)

    if not state and not running:
        click.echo("Agent is not running.")
        return

    status_str = click.style("● running", fg="green") if running else click.style("○ stopped", fg="red")
    runtime = state.get("runtime", "process")
    pid = state.get("pid", "—")
    container_id = state.get("container_id", "—")
    started = state.get("started_at", "—")
    last_active = state.get("last_active", "—")

    # Uptime
    uptime_str = "—"
    try:
        from datetime import datetime

        started_dt = datetime.fromisoformat(started)
        now = datetime.now(started_dt.tzinfo) if started_dt.tzinfo else datetime.now()
        uptime = now - started_dt
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    except Exception:
        pass

    click.echo(f"Status:      {status_str}")
    click.echo(f"Runtime:     {runtime}")
    click.echo(f"PID:         {pid}")
    if runtime == "docker":
        click.echo(f"Container:   {container_id}")
    click.echo(f"Started:     {started}")
    click.echo(f"Last active: {last_active}")
    click.echo(f"Uptime:      {uptime_str}")

    for ch in state.get("channels", []):
        name = ch.get("name", "?")
        host = ch.get("host", "?")
        if runtime == "docker" and host == "0.0.0.0":
            host = "127.0.0.1"
        port = ch.get("port", "?")
        addr = ch.get("address", "?")
        click.echo(f"Channel:     {name} @ {addr} → ws://{host}:{port}/ws")


# ── bos restart ───────────────────────────────────────────────


@click.command()
@click.pass_context
def restart(ctx):
    """Restart the agent (stop then start)."""
    # Re-invoke stop (ignore failure if not running)
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state

    if is_running(rd):
        state = read_state(rd)
        ctx.invoke(stop)
        time.sleep(0.5)
        ctx.invoke(start, docker=state.get("runtime") == "docker")
        return

    ctx.invoke(start)


# ── bos tui ───────────────────────────────────────────────────


@click.command()
@click.option("--host", default=None, help="Channel host (overrides agent.state).")
@click.option("--port", default=None, type=int, help="Channel port (overrides agent.state).")
@click.option("--client-id", default=None, help="Stable server-side cursor id (defaults to tui:<username>).")
@click.pass_context
def tui(ctx, host: str | None, port: int | None, client_id: str | None):
    """Connect the TUI to a running agent via the HTTP channel."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import read_state

    def _resolve_endpoint() -> tuple[str, int] | None:
        """Read agent.state to discover the current HttpChannel host:port."""
        state = read_state(rd)
        for ch in state.get("channels", []):
            if ch.get("name") == "HttpChannel":
                h = ch.get("host", "127.0.0.1")
                p = ch.get("port")
                if h and p:
                    return (h, int(p))
        return None

    # Discover initial endpoint (CLI overrides take precedence)
    if not (host and port):
        resolved = _resolve_endpoint()
        if resolved:
            host = host or resolved[0]
            port = port or resolved[1]

    if not host or not port:
        raise click.UsageError(
            "Could not determine channel endpoint. Use --host and --port, or make sure the agent is running."
        )

    from bos.cli.tui_app import run_chat_tui
    from bos.extensions.channels.http_client import HttpChannelClient

    async def _run():
        client = HttpChannelClient(
            host=host,
            port=port,
            address="tui",
            client_id=client_id or _default_tui_client_id(),
            chat_id=None,
            endpoint_resolver=_resolve_endpoint,
        )
        await _connect_tui_client(client)
        try:
            await run_chat_tui(client)
        finally:
            await client.aclose()

    asyncio.run(_run())
