"""``boscli gateway start/stop/status/restart, ask, tui`` — gateway lifecycle commands."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from bos.config import ConfigNotFoundError, Workspace, WorkspaceResolutionError, resolve_config_source
from bos.config.workspace import _resolve_path, presets_dir
from bos.core.actor import MessageType
from bos.core.agent import TurnEvent
from bos.gateway.state import GatewayRunDir


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

    * ``-c <preset>`` → workspace defaults to ``~/.bos/presets/<preset>`` unless overridden.
    * ``-c <file>`` → workspace defaults to ``"."`` unless overridden.
    * No ``-c`` → ancestor discovery; if not found, fall back to the ``default`` preset.
    """
    config_arg = ctx.obj.get("CONFIG")

    if config_arg:
        try:
            config_path, bos_dir, config = resolve_config_source(config_arg)
        except WorkspaceResolutionError as exc:
            raise click.UsageError(str(exc)) from exc
        ws_dir = _daemon_workspace_dir(config_arg, bos_dir, workspace_override)
        return Workspace(ws_dir, bos_dir, config, config_file=config_path)

    try:
        ws = Workspace.from_discovery(".")
        if workspace_override:
            ws.workspace = _resolve_path(workspace_override)
        return ws
    except ConfigNotFoundError:
        pass
    except WorkspaceResolutionError as exc:
        raise click.UsageError(str(exc)) from exc

    try:
        config_path, bos_dir, config = resolve_config_source("default")
    except WorkspaceResolutionError as exc:
        raise click.UsageError(str(exc)) from exc
    ws_dir = _daemon_workspace_dir("default", bos_dir, workspace_override)
    return Workspace(ws_dir, bos_dir, config, config_file=config_path)


def _get_ws_and_rd(ctx, workspace_override: str | None = None) -> tuple[Workspace, GatewayRunDir]:
    """Build Workspace + GatewayRunDir for daemon commands."""
    ws = _build_workspace_for_daemon(ctx, workspace_override)
    _echo_config_source(ws, config_arg=ctx.obj.get("CONFIG"))
    rd = GatewayRunDir(ws.bos_dir)
    return ws, rd


def _echo_config_source(ws: Workspace, config_arg: str | None = None) -> None:
    """Print one stderr line stating which config source is in use (BEP 9).

    Suppressed when stderr is not a terminal so piped output stays clean.
    """
    if not sys.stderr.isatty():
        return
    if preset := _builtin_preset_name_for_config_file(ws.config_file):
        message = f"Using built-in preset: {preset} ({ws.bos_dir})"
    elif config_arg:
        message = f"Using config file: {ws.config_file}"
    else:
        config_name = Path(ws.config_file).name if ws.config_file else "?"
        message = f"Using project: {ws.workspace} ({config_name})"
    click.echo(message, err=True)


def _runner_config_arg(ctx, ws: Workspace) -> str:
    """Return the config argument a runner subprocess should resolve.

    For built-in presets this preserves the preset name (for example
    ``default``), so the child resolves the same BOS home run directory instead
    of treating the package preset TOML as an ordinary config file.
    """
    config_arg = ctx.obj.get("CONFIG")
    if config_arg:
        return str(config_arg)
    if preset_name := _builtin_preset_name_for_config_file(ws.config_file):
        return preset_name
    if ws.config_file is None:
        raise click.UsageError("Could not determine config path for gateway runner.")
    return str(ws.config_file)


def _daemon_workspace_dir(config_arg: str, bos_dir, workspace_override: str | None) -> str | Path:
    if workspace_override:
        return workspace_override
    return Path(bos_dir) if _is_builtin_preset_arg(config_arg) else "."


def _is_builtin_preset_arg(config_arg: str) -> bool:
    return not Path(config_arg).expanduser().is_file() and (presets_dir() / f"{config_arg}.toml").is_file()


def _builtin_preset_name_for_config_file(config_file) -> str | None:
    if config_file is None:
        return None
    try:
        config_path = Path(config_file).resolve()
        return config_path.relative_to(presets_dir().resolve()).stem
    except ValueError:
        return None


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


def _safe_username() -> str:
    try:
        username = getpass.getuser()
    except Exception:
        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    safe = re.sub(r"[^a-z0-9_.-]+", "-", username.strip().lower()).strip("-")
    return safe or "local"


def _default_tui_client_id() -> str:
    return f"tui:{_safe_username()}"


def _read_gateway_endpoint(rd: GatewayRunDir) -> tuple[str, int] | None:
    """Read gateway.state to discover the current gateway host:port."""
    from bos.runner.proc import read_state

    state = read_state(rd)
    gateway_state = state.get("gateway", {})
    host = gateway_state.get("host")
    port = gateway_state.get("port")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    if host and port:
        return host, int(port)
    return None


def _ensure_gateway_endpoint(ctx, rd: GatewayRunDir, workspace_dir: str | None) -> tuple[str, int]:
    """Return the running gateway endpoint, starting the gateway if needed.

    A gateway started here is left running after the command finishes.
    """
    from bos.runner.proc import is_running

    if not is_running(rd):
        click.echo("No gateway running — starting one in the background (it stays running).", err=True)
        try:
            ctx.invoke(start, foreground=False, docker=False, workspace_dir=workspace_dir)
        except SystemExit:
            # Lost a start race to another process — fine as long as a gateway is up now.
            if not is_running(rd):
                raise

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (endpoint := _read_gateway_endpoint(rd)) is not None:
            return endpoint
        time.sleep(0.3)
    raise click.ClickException(
        "Gateway endpoint did not become available — check `boscli gateway status` and the gateway log."
    )


def _preview(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


class _TaskProgressDisplay:
    """Streams oneshot turn events to the console, formatted like the TUI chat log.

    Lines print to stderr as they arrive (when stderr is a terminal), so the
    final reply on stdout stays clean for piping.
    """

    _TASK_TOOL_NAMES = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}

    def __init__(self) -> None:
        self._console = Console(stderr=True)
        self._enabled = self._console.is_terminal
        self._pending_tool_calls: list[tuple[str, str]] = []

    async def emit(self, event: TurnEvent) -> None:
        if not self._enabled:
            return
        if event.event_type == "task" and event.detail == "task_state":
            lines = self._format_task_board(event)
            if lines:
                self._console.print()
        else:
            lines = self._format_event(event)
        for line in lines:
            self._console.print(Text.from_markup(line))

    @staticmethod
    def _format_task_board(event: TurnEvent) -> list[str]:
        tasks = (event.metadata or {}).get("tasks", [])
        if not tasks:
            return []
        order = {"completed": 0, "in_progress": 1, "pending": 2}
        ordered = sorted(tasks, key=lambda t: order.get(t.get("status"), 3))
        lines = ["[bold]• Tasks[/]"]
        for i, t in enumerate(ordered):
            prefix = "└" if i == 0 else " "
            subject = escape(str(t.get("subject", "")))
            if t.get("status") == "completed":
                lines.append(f"  {prefix} [dim][s]✔ {subject}[/s][/dim]")
            elif t.get("status") == "in_progress":
                lines.append(f"  {prefix} [bold]■ {subject}[/]")
            else:
                lines.append(f"  {prefix} □ {subject}")
        return lines

    def _format_event(self, event: TurnEvent) -> list[str]:
        if event.event_type == "llm" and event.detail in ("tool_calls", "response_ready"):
            # Unified LLM response event — show thinking content if present
            preview = escape(_preview(event.content, 240))
            return [f"\n[dim italic]● {preview}[/]"] if preview else []

        if event.event_type == "tool" and event.detail == "tool_call":
            name = event.tool_name or "?"
            if name in self._TASK_TOOL_NAMES:
                return []
            args_str = ""
            if event.content:
                try:
                    args = json.loads(event.content) if isinstance(event.content, str) else event.content
                    if isinstance(args, dict):
                        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                except (json.JSONDecodeError, TypeError):
                    pass
            self._pending_tool_calls.append((name, args_str))
            return []

        if event.event_type == "tool" and event.detail == "tool_result":
            name = event.tool_name or "?"
            if name in self._TASK_TOOL_NAMES:
                return []
            preview = escape(_preview(event.content, 240))
            if self._pending_tool_calls:
                call_name, call_args = self._pending_tool_calls.pop(0)
                return [
                    f"  [bold]{escape(call_name)}[/]({escape(_preview(call_args, 160))})",
                    f"   └ [dim]{preview}[/]",
                ]
            return [f"  {escape(name)}: [dim]{preview}[/]"]

        if event.detail == "max_iteration":
            return ["[yellow]  max iterations reached[/]"]

        if event.detail == "error":
            return [f"[red]  error: {escape(str(event.content or 'unknown error'))}[/]"]

        return []


async def _run_oneshot_exchange(client, message: str, progress: _TaskProgressDisplay | None) -> str:
    """Send *message* over the gateway client and consume envelopes until the final reply."""
    await client.send(message)
    while True:
        env = await client.receive()
        if env.content_type == MessageType.TURN_EVENT:
            try:
                data = json.loads(env.content) if isinstance(env.content, str) else {}
            except json.JSONDecodeError:
                data = {}
            if data and progress is not None:
                await progress.emit(TurnEvent.from_payload(data))
        elif env.content_type == MessageType.MESSAGE:
            return str(env.content or "")
        # SYSTEM / ECHO / COMMAND_RESULT envelopes are not part of the oneshot exchange.


# ── boscli ask ──────────────────────────────────────────────────


@click.command()
@click.argument("message", required=False)
@click.option(
    "--stdin",
    "use_stdin",
    is_flag=True,
    default=False,
    help="Read task content from stdin (appended after MESSAGE if both given).",
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
    use_stdin: bool,
    workspace_dir: str | None,
):
    """Run a oneshot agent task against the gateway.

    Connects to the running gateway (starting one in the background if
    needed — it is left running) and prints the agent's final reply.

    \b
    Examples:
        boscli ask "refactor the auth module"
        boscli -c coding ask "explain this"
        cat spec.md | boscli ask --stdin
    """
    if use_stdin and not sys.stdin.isatty():
        stdin_content = sys.stdin.read()
        message = ((message or "") + "\n" + stdin_content).strip() if message else stdin_content.strip()

    if not message:
        raise click.UsageError("Provide a task message or use --stdin.")

    ws, rd = _get_ws_and_rd(ctx, workspace_dir)
    gateway_config = ws.resolve_gateway_config()
    api_key = os.environ.get(gateway_config.api_key_env, "").strip() or None

    host, port = _ensure_gateway_endpoint(ctx, rd, workspace_dir)

    from bos.gateway.client import GatewayClient

    # Stamp the invocation directory (or -w override) on each message so the
    # agent knows where the user is working, not just the gateway workspace.
    client_workdir = str(_resolve_path(workspace_dir)) if workspace_dir else os.getcwd()

    async def _run() -> str:
        # A unique channel id per invocation gives the task a fresh chat.
        client = GatewayClient(
            host=host,
            port=port,
            address="ask",
            channel_id=f"ask:{_safe_username()}-{uuid.uuid4().hex[:8]}",
            chat_id=None,
            endpoint_resolver=lambda: _read_gateway_endpoint(rd),
            api_key=api_key,
            workdir=client_workdir,
        )
        await client.connect()
        try:
            return await _run_oneshot_exchange(client, message, _TaskProgressDisplay())
        finally:
            await client.aclose()

    result = asyncio.run(_run())

    if sys.stdout.isatty():
        from rich.markdown import Markdown

        console = Console()
        console.print()
        console.rule(style="dim")
        try:
            console.print(Markdown(result or "(no response)"))
        except Exception:
            click.echo(result)
    else:
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

    from bos.runner.proc import (
        _pid_alive,
        is_running,
        read_state,
        reap_stale,
        run_docker_foreground,
        start_background,
        start_docker,
    )
    from bos.runner.runner import start as start_gateway

    if is_running(rd):
        state = read_state(rd)
        identifier = state.get("container_id") if state.get("runtime") == "docker" else state.get("pid")
        click.echo(f"Gateway is already running ({state.get('runtime', 'process')} {identifier}).", err=True)
        raise SystemExit(1)

    # No live gateway: clear any leftover pid/state from a process that was
    # killed or crashed without cleaning up, so it neither blocks the start nor
    # leaves a stale endpoint behind for `boscli ask`.
    if reap_stale(rd):
        click.echo("Cleared stale gateway pid/state from a previous run.", err=True)

    runtime = ws.get_runtime_config(force_kind="docker" if docker else None)

    if runtime.kind not in {"process", "docker"}:
        raise click.UsageError(f"Unsupported runtime kind: {runtime.kind!r}")

    runner_config_arg = _runner_config_arg(ctx, ws)

    if runtime.kind == "docker":
        if foreground:
            click.echo("Starting gateway in Docker foreground…")
            raise SystemExit(run_docker_foreground(ws, runtime, config_arg=runner_config_arg))

        container_id = start_docker(ws, rd, runtime, config_arg=runner_config_arg)
        click.echo(f"Gateway starting in Docker ({container_id[:12]})…")
        pid = None
    elif foreground:
        click.echo("Starting gateway in foreground…")
        asyncio.run(start_gateway(ws))
        return
    else:
        argv = [sys.executable, "-m", "bos.runner", "--config", runner_config_arg]
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
        # No endpoint yet: if the spawned process has already exited (lost the singleton
        # lock, crashed at startup, …), surface it now instead of waiting out the timeout.
        if pid and runtime.kind == "process" and not _pid_alive(int(pid)):
            click.echo(
                f"Gateway process {pid} exited during startup — check {rd.log_file} for the cause.",
                err=True,
            )
            raise SystemExit(1)

    ident = container_id[:12] if container_id else pid
    click.echo(f"Gateway started ({runtime.kind} {ident}) — endpoint not yet available (check boscli gateway status)")


# ── boscli gateway stop ─────────────────────────────────────────


@gateway.command()
@click.pass_context
def stop(ctx):
    """Stop the running gateway."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state, stop_gateway

    if not is_running(rd):
        click.echo("No gateway is running.", err=True)
        raise SystemExit(1)

    state = read_state(rd)
    runtime = state.get("runtime", "process")
    ident = state.get("container_id", "?")[:12] if runtime == "docker" else state.get("pid", "?")
    click.echo(f"Stopping gateway ({runtime} {ident})…")

    stop_gateway(rd, signal.SIGTERM)

    # Wait up to 5s for clean exit
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if not is_running(rd):
            break
    else:
        click.echo("Gateway did not exit cleanly — sending SIGKILL")
        try:
            stop_gateway(rd, signal.SIGKILL)
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
        # Wait for the old process to fully release the run dir (and its lock)
        # before starting again, so the fresh gateway doesn't race a still-exiting
        # one and lose the singleton lock.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and is_running(rd):
            time.sleep(0.1)
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
    """Connect the TUI to the gateway, starting one in the background if needed."""
    ws, rd = _get_ws_and_rd(ctx)

    def _resolve_endpoint() -> tuple[str, int] | None:
        return _read_gateway_endpoint(rd)

    # Discover the endpoint, starting the gateway if none is running.
    # Explicit --host/--port overrides take precedence and never auto-start.
    if not (host and port):
        resolved_host, resolved_port = _ensure_gateway_endpoint(ctx, rd, None)
        host = host or resolved_host
        port = port or resolved_port
    gateway_config = ws.resolve_gateway_config()
    api_key = os.environ.get(gateway_config.api_key_env, "").strip() or None

    from bos.cli.tui_app import run_chat_tui
    from bos.gateway.client import GatewayClient

    async def _run():
        client = GatewayClient(
            host=host,
            port=port,
            address="tui",
            channel_id=channel_id or _default_tui_client_id(),
            chat_id=None,
            endpoint_resolver=_resolve_endpoint,
            api_key=api_key,
            workdir=os.getcwd(),
        )
        await _connect_tui_client(client)
        try:
            await run_chat_tui(client)
        finally:
            await client.aclose()

    asyncio.run(_run())
