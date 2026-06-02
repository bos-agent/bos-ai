"""``boscli gateway start/stop/status/restart, ask, tui`` — gateway lifecycle commands."""

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
from typing import Any

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from bos.config import ConfigNotFoundError, Workspace, WorkspaceResolutionError, resolve_config_source
from bos.config.workspace import _resolve_path, presets_dir
from bos.gateway.state import GatewayRunDir
from bos.protocol import TurnEvent


def _build_workspace_for_ask(ctx, workspace_override: str | None = None) -> Workspace:
    """Build a Workspace for ``boscli ask``.

    * workspace defaults to ``"."`` unless overridden by ``--workspace``.
    * ``-c <preset|file>`` → resolve via :func:`resolve_config_source`.
    * No ``-c`` → discover project config; fall back to ``default`` preset.
    """
    config_arg = ctx.obj.get("CONFIG")
    ws_dir = workspace_override or "."

    if config_arg:
        try:
            config_path, bos_dir, config = resolve_config_source(config_arg)
        except WorkspaceResolutionError as exc:
            raise click.UsageError(str(exc)) from exc
        return Workspace(ws_dir, bos_dir, config, config_file=config_path)

    # No -c: try project discovery, fall back to default preset
    try:
        ws = Workspace.from_discovery(".")
        if workspace_override:
            ws.workspace = _resolve_path(workspace_override)
        return ws
    except ConfigNotFoundError:
        pass

    try:
        config_path, bos_dir, config = resolve_config_source("default")
    except WorkspaceResolutionError as exc:
        raise click.UsageError(str(exc)) from exc
    return Workspace(ws_dir, bos_dir, config, config_file=config_path)


def _build_workspace_for_daemon(ctx, workspace_override: str | None = None) -> Workspace:
    """Build a Workspace for daemon commands (``boscli gateway start``).

    * ``-c <preset|file>`` → workspace defaults to ``"."`` unless overridden.
    * No ``-c`` → ancestor discovery (error if not found).
    """
    config_arg = ctx.obj.get("CONFIG")
    ws_dir = workspace_override or "."

    if config_arg:
        try:
            config_path, bos_dir, config = resolve_config_source(config_arg)
        except WorkspaceResolutionError as exc:
            raise click.UsageError(str(exc)) from exc
        return Workspace(ws_dir, bos_dir, config, config_file=config_path)

    try:
        ws = Workspace.from_discovery(".")
        if workspace_override:
            ws.workspace = _resolve_path(workspace_override)
        return ws
    except WorkspaceResolutionError as exc:
        hint = str(exc)
        presets = presets_dir()
        available = sorted(p.stem for p in presets.glob("*.toml")) if presets.exists() else []
        names = ", ".join(available) or "none"
        hint += f"\nTip: use -c <preset> to run without a workspace. Available presets: {names}."
        raise click.UsageError(hint) from exc


def _get_ws_and_rd(ctx, workspace_override: str | None = None) -> tuple[Workspace, GatewayRunDir]:
    """Build Workspace + RunDir for daemon commands."""
    ws = _build_workspace_for_daemon(ctx, workspace_override)
    rd = GatewayRunDir(ws.bos_dir)
    return ws, rd


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
    """Live renderer for oneshot task turn events.

    Task state is rendered as a fixed board at the top; other events
    scroll in the area below.
    """

    def __init__(self, *, max_rows: int = 10) -> None:
        self._console = Console(stderr=True)
        self._enabled = self._console.is_terminal
        self._live: Live | None = None
        self._rows: deque[tuple[str, str]] = deque(maxlen=max_rows)
        self._task_board: str = ""

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
        if event.event_type == "task" and event.detail == "task_state":
            self._task_board = self._format_task_board(event)
        else:
            style, message = self._format_event(event)
            if message:
                self._append(style, message)
        if self._live is not None:
            self._live.update(self._render())

    def _append(self, style: str, message: str) -> None:
        self._rows.append((style, _preview(message, 160)))

    def _render(self) -> Panel:
        body = Text()
        if self._task_board:
            body.append(self._task_board, style="bold")
            body.append("\n")
            body.append("─" * 40, style="dim")
        for idx, (style, message) in enumerate(self._rows):
            if idx or self._task_board:
                body.append("\n")
            body.append(message, style=style)
        return Panel(body, title="boscli ask", border_style="cyan", padding=(0, 1))

    @staticmethod
    def _format_task_board(event: TurnEvent) -> str:
        tasks = (event.metadata or {}).get("tasks", [])
        if not tasks:
            return ""
        lines = ["■ Tasks"]
        for t in tasks:
            marker = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(t.get("status"), "  ")
            blocked = f" (blocked: {', '.join(t.get('blocked_by', []))})" if t.get("blocked_by") else ""
            lines.append(f"  {marker} [{t.get('id')}] {t.get('subject')}{blocked}")
        return "\n".join(lines)

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
    agent_kind: str,
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

    async with ws.harness() as harness:
        agent = await harness.create_agent(agent_kind, agent_cfg=agent_cfg)
        actor_mbox = harness.mail_route.bind("agent@main")
        client_mbox = harness.mail_route.bind("client@local")

        try:
            username = getpass.getuser()
        except Exception:
            username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        safe = re.sub(r"[^a-z0-9_.-]+", "-", username.strip().lower()).strip("-")
        client_id = f"local:{safe or 'local'}-{uuid.uuid4().hex[:8]}"

        chat_state = ChatState()
        actor = AgentActor(agent, actor_mbox, chat_state=chat_state)
        client = LocalClient(
            client_id=client_id,
            client_mbox=client_mbox,
            chat_state=chat_state,
        )

        await client.connect()

        from bos.cli.tui_app import run_chat_tui

        actor_task = asyncio.create_task(actor.run())

        # If an initial message is provided, send it first
        if initial_message:
            await client.send(initial_message, chat_id=client.chat_id)

        try:
            await run_chat_tui(client, local_mode=True)
        finally:
            actor_task.cancel()
            try:
                await actor_task
            except asyncio.CancelledError:
                pass
            await client.aclose()


# ── boscli ask ──────────────────────────────────────────────────


@click.command()
@click.argument("message", required=False)
@click.option(
    "--agent",
    "agent_kind",
    default=None,
    help="Agent kind to use (defaults to the configured main agent).",
)
@click.option(
    "--default-model",
    "default_model",
    default=None,
    help="Set the default model for all components (agent, consolidator, subagents).",
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
    "-w",
    "--workspace",
    "workspace_dir",
    default=None,
    help="Override the workspace directory (defaults to '.' or project root).",
)
@click.pass_context
def ask(
    ctx,
    message: str | None,
    agent_kind: str | None,
    default_model: str | None,
    use_stdin: bool,
    max_iterations: int | None,
    interactive: bool,
    workspace_dir: str | None,
):
    """Run a oneshot agent task or start an interactive chat session.

    \b
    Examples:
        boscli ask "refactor the auth module"
        boscli ask -i
        boscli ask -w /path/to/project -i
        boscli -c coding ask -i
        boscli ask --default-model gpt-4o "explain this"
        cat spec.md | boscli ask --stdin
    """
    if use_stdin and not sys.stdin.isatty():
        stdin_content = sys.stdin.read()
        message = ((message or "") + "\n" + stdin_content).strip() if message else stdin_content.strip()

    if not interactive and not message:
        raise click.UsageError("Provide a task message, use --stdin, or use -i for interactive mode.")

    ws = _build_workspace_for_ask(ctx, workspace_dir)
    ws.resolve_agents()
    ws.bootstrap_platform()
    selected_agent = agent_kind or ws.get_main_agent_kind()

    if default_model:
        os.environ["BOS_MODEL"] = default_model

    if interactive:
        agent_cfg: dict = {"agent_name": "main"}
        if max_iterations is not None:
            agent_cfg["max_iterations"] = max_iterations
        try:
            asyncio.run(_run_interactive(ws, selected_agent, agent_cfg, initial_message=message))
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        return

    # One-shot mode
    async def _run(event_sink: _TaskProgressDisplay | None = None) -> str:
        agent_cfg: dict = {"agent_name": "main"}
        if max_iterations is not None:
            agent_cfg["max_iterations"] = max_iterations
        async with ws.harness() as harness:
            agent = await harness.create_agent(selected_agent, agent_cfg=agent_cfg)
            return await agent.ask(
                uuid.uuid4().hex,
                message,
                event_sink=event_sink,
            )

    try:
        with _TaskProgressDisplay() as progress:
            result = asyncio.run(_run(progress))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    click.echo(result)


# ── boscli gateway ──────────────────────────────────────────────


@click.group(name="gateway")
def gateway():
    """Manage the BOS gateway process."""


# ── boscli gateway start ────────────────────────────────────────


@gateway.command()
@click.option("--foreground", "-f", is_flag=True, default=False, help="Run in the foreground (don't daemonize).")
@click.option("--docker", is_flag=True, default=False, help="Run the agent inside a Docker container.")
@click.option(
    "-w",
    "--workspace",
    "workspace_dir",
    default=None,
    help="Override the workspace directory (defaults to '.' or project root).",
)
@click.pass_context
def start(ctx, foreground: bool, docker: bool, workspace_dir: str | None):
    """Start the BOS gateway."""
    ws, rd = _get_ws_and_rd(ctx, workspace_dir)

    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.runner.proc import is_running, read_state, run_docker_foreground, start_background, start_docker
    from bos.runner.runner import start as start_gateway

    if is_running(rd):
        state = read_state(rd)
        identifier = state.get("container_id") if state.get("runtime") == "docker" else state.get("pid")
        click.echo(f"Gateway is already running ({state.get('runtime', 'process')} {identifier}).", err=True)
        raise SystemExit(1)

    runtime = ws.get_runtime_config(force_kind="docker" if docker else None)

    if runtime.kind not in {"process", "docker"}:
        raise click.UsageError(f"Unsupported runtime kind: {runtime.kind!r}")

    if runtime.kind == "docker":
        if foreground:
            click.echo("Starting gateway in Docker foreground…")
            raise SystemExit(run_docker_foreground(ws, runtime))

        container_id = start_docker(ws, rd, runtime)
        click.echo(f"Gateway starting in Docker ({container_id[:12]})…")
        pid = None
    elif foreground:
        click.echo("Starting gateway in foreground…")
        asyncio.run(start_gateway(ws))
        return
    else:
        argv = [sys.executable, "-m", "bos.runner", "--config", str(ws.config_file)]
        pid = start_background(argv, rd, cwd=ws.workspace)
        click.echo(f"Gateway starting (PID {pid})…")
        container_id = None

    state = read_state(rd)
    pid = state.get("pid") or pid
    container_id = state.get("container_id") or container_id

    # Poll gateway.state until the HTTP gateway reports its bound endpoint (up to 10s).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.3)
        state = read_state(rd)
        gateway_state = state.get("gateway", {})
        port = gateway_state.get("port")
        if port:
            host = gateway_state.get("host", "127.0.0.1")
            if state.get("runtime") == "docker" and host == "0.0.0.0":
                host = "127.0.0.1"
            ident = container_id[:12] if container_id else pid
            click.echo(f"Gateway started ({state.get('runtime', runtime.kind)} {ident}) · http://{host}:{port}")
            return

    ident = container_id[:12] if container_id else pid
    click.echo(f"Gateway started ({runtime.kind} {ident}) — endpoint not yet available (check boscli gateway status)")


# ── boscli gateway stop ─────────────────────────────────────────


@gateway.command()
@click.pass_context
def stop(ctx):
    """Stop the running gateway."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state, stop_agent

    if not is_running(rd):
        click.echo("No gateway is running.", err=True)
        raise SystemExit(1)

    state = read_state(rd)
    runtime = state.get("runtime", "process")
    ident = state.get("container_id", "?")[:12] if runtime == "docker" else state.get("pid", "?")
    click.echo(f"Stopping gateway ({runtime} {ident})…")

    stop_agent(rd, signal.SIGTERM)

    # Wait up to 5s for clean exit
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if not is_running(rd):
            break
    else:
        click.echo("Gateway did not exit cleanly — sending SIGKILL")
        try:
            stop_agent(rd, signal.SIGKILL)
        except Exception:
            pass

    # Clean up state files if process left them behind
    rd.pid_file.unlink(missing_ok=True)
    rd.state_file.unlink(missing_ok=True)
    click.echo("Gateway stopped.")


# ── boscli gateway status ───────────────────────────────────────


@gateway.command()
@click.pass_context
def status(ctx):
    """Show gateway running status."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state

    state = read_state(rd)
    running = is_running(rd)

    if not state and not running:
        click.echo("Gateway is not running.")
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

    gateway_state = state.get("gateway", {})
    host = gateway_state.get("host")
    if runtime == "docker" and host == "0.0.0.0":
        host = "127.0.0.1"
    port = gateway_state.get("port")
    if host and port:
        click.echo(f"Gateway:     http://{host}:{port}")

    for name, actor in state.get("actors", {}).items():
        click.echo(f"Actor:       {name} ({actor.get('display_name', '?')}) · {actor.get('status', '?')}")

    for channel_id, ch in state.get("channels", {}).items():
        kind = ch.get("kind", "?")
        type_name = ch.get("type", "?")
        addr = ch.get("address", "?")
        target = ch.get("target_actor", "?")
        click.echo(f"Channel:     {channel_id} [{kind}/{type_name}] @ {addr} → {target}")


# ── boscli gateway restart ──────────────────────────────────────


@gateway.command()
@click.pass_context
def restart(ctx):
    """Restart the gateway (stop then start)."""
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


# ── boscli tui ───────────────────────────────────────────────────


@click.command()
@click.option("--host", default=None, help="Gateway host (overrides gateway.state).")
@click.option("--port", default=None, type=int, help="Gateway port (overrides gateway.state).")
@click.option("--channel-id", default=None, help="Stable gateway channel id (defaults to tui:<username>).")
@click.pass_context
def tui(ctx, host: str | None, port: int | None, channel_id: str | None):
    """Connect the TUI to a running gateway."""
    ws, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import read_state

    def _resolve_endpoint() -> tuple[str, int] | None:
        """Read gateway.state to discover the current gateway host:port."""
        state = read_state(rd)
        gateway_state = state.get("gateway", {})
        h = gateway_state.get("host")
        p = gateway_state.get("port")
        if h == "0.0.0.0":
            h = "127.0.0.1"
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
            "Could not determine gateway endpoint. Use --host and --port, or make sure the gateway is running."
        )
    gateway_config = ws.resolve_gateway_config()
    api_key = os.environ.get(gateway_config.api_key_env)
    if not api_key:
        raise click.UsageError(f"Gateway API key environment variable {gateway_config.api_key_env!r} is not set.")

    from bos.cli.tui_app import run_chat_tui
    from bos.extensions.channels.http_client import HttpChannelClient

    async def _run():
        client = HttpChannelClient(
            host=host,
            port=port,
            address="tui",
            channel_id=channel_id or _default_tui_client_id(),
            chat_id=None,
            endpoint_resolver=_resolve_endpoint,
            api_key=api_key,
        )
        await _connect_tui_client(client)
        try:
            await run_chat_tui(client)
        finally:
            await client.aclose()

    asyncio.run(_run())
