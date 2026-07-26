"""``boscli gateway start/stop/status/restart, ask, tui`` — gateway lifecycle commands."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import math
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
            ctx.invoke(start, foreground=False, workspace_dir=workspace_dir)
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

        if event.detail == "shutdown":
            return ["[yellow]  interrupted — agent shutting down[/]"]

        if event.detail == "error":
            return [f"[red]  error: {escape(str(event.content or 'unknown error'))}[/]"]

        return []


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
@click.option("--model", "model", default=None, help="Model id to use for this run (overrides BOS_MODEL).")
@click.option("--agent", "agent_name", default=None, help="Agent to run (defaults to the main actor's agent).")
@click.option(
    "--no-steps",
    "no_steps",
    is_flag=True,
    default=False,
    help="Suppress step-by-step progress output; print only the final reply (useful in scripts).",
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
    model: str | None,
    agent_name: str | None,
    no_steps: bool,
    workspace_dir: str | None,
):
    """Run a oneshot agent task in-process and print the agent's final reply.

    Runs the agent in this process (not via the gateway), so BOS_MODEL / --model
    are honored on every invocation and no gateway is left running.

    \b
    Examples:
        boscli ask "refactor the auth module"
        boscli -c coding ask "explain this"
        boscli ask --agent researcher "summarize the spec"
        boscli ask --no-steps "explain this" > answer.md
        cat spec.md | boscli ask --stdin
    """
    if use_stdin and not sys.stdin.isatty():
        stdin_content = sys.stdin.read()
        message = ((message or "") + "\n" + stdin_content).strip() if message else stdin_content.strip()

    if not message:
        raise click.UsageError("Provide a task message or use --stdin.")

    ws, _ = _get_ws_and_rd(ctx, workspace_dir)
    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.core import AgentRegistry

    # --agent names an agent kind directly; otherwise the main actor locates it.
    if agent_name:
        if not AgentRegistry.has_registered(agent_name):
            known = ", ".join(sorted(AgentRegistry.describe())) or "none"
            raise click.ClickException(f"Unknown agent {agent_name!r}. Available agents: {known}")
        agent_kind = agent_name
        agent_cfg: dict[str, Any] | None = None
    else:
        from bos.config.schema import _agent_config_to_core_kwargs

        runtime = ws.config.runtime
        actors = runtime.actors if runtime else {}
        actor_cfg = actors[ws.resolve_default_actor()]
        agent_kind = actor_cfg.agent
        # Only explicitly-set overrides, in core-kwargs shape — a raw
        # model_dump() would materialize defaults (plugins.enabled=[], ...)
        # that wipe the agent kind's registry defaults on merge.
        agent_cfg = _agent_config_to_core_kwargs(actor_cfg.agent_cfg)

    async def _run() -> str:
        async with ws.harness() as harness:
            agent = await harness.create_agent(kind=agent_kind, agent_cfg=agent_cfg)
            result = await agent.run(
                uuid.uuid4().hex,
                message,
                event_sink=None if no_steps else _TaskProgressDisplay(),
                llm_args={"model": model} if model else None,
            )
            return str(result.output or "")

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


async def _run_foreground_gateway(start_gateway, ws) -> None:
    """Run the gateway in this process, with Ctrl-C as a graceful stop.

    First interrupt drains in-flight turns (each closes with a handoff); a
    second stops immediately. Without this the foreground gateway would die on
    the first Ctrl-C mid-turn, unlike the daemon under SIGTERM."""
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    handled: list[Any] = []

    def _on_ready(instance) -> None:
        handled.append(instance)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _request_stop)

    def _request_stop() -> None:
        instance = handled[0]
        if not instance.shutdown_requested:
            click.echo("\nStopping — draining in-flight turns…", err=True)
            instance.request_shutdown()
            return
        click.echo("\nStopping now.", err=True)
        # Hand the signals back to the default handlers on the way out, so a
        # third Ctrl-C can still kill a teardown that has gone wrong.
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        if main_task is not None:
            main_task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await start_gateway(ws, on_ready=_on_ready)


@gateway.command()
@click.option("--foreground", "-f", is_flag=True, default=False, help="Run in the foreground (don't daemonize).")
@click.option(
    "-w",
    "--workspace",
    "workspace_dir",
    default=None,
    help="Override the workspace directory (defaults to '.' or project root).",
)
@click.pass_context
def start(ctx, foreground: bool, workspace_dir: str | None):
    """Start the BOS gateway."""
    ws, rd = _get_ws_and_rd(ctx, workspace_dir)

    ws.resolve_agents()
    ws.bootstrap_platform()

    from bos.runner.proc import (
        _pid_alive,
        _pid_is_gateway,
        is_running,
        read_state,
        reap_stale,
        start_background,
    )
    from bos.runner.runner import start as start_gateway

    if is_running(rd):
        state = read_state(rd)
        click.echo(f"Gateway is already running (process {state.get('pid')}).", err=True)
        raise SystemExit(1)

    # No live gateway: clear any leftover pid/state from a crashed process.
    if reap_stale(rd):
        click.echo("Cleared stale gateway pid/state from a previous run.", err=True)

    runner_config_arg = _runner_config_arg(ctx, ws)

    if foreground:
        click.echo("Starting gateway in foreground…")
        asyncio.run(_run_foreground_gateway(start_gateway, ws))
        return

    argv = [sys.executable, "-m", "bos.runner", "--config", runner_config_arg]
    pid = start_background(argv, rd, cwd=ws.workspace)
    click.echo(f"Gateway starting (PID {pid})…")

    state = read_state(rd)
    pid = state.get("pid") or pid

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.3)
        state = read_state(rd)
        gateway_state = state.get("gateway", {})
        port = gateway_state.get("port")
        if port:
            host = gateway_state.get("host", "127.0.0.1")
            click.echo(f"Gateway started (process {pid}) · http://{host}:{port}")
            return
        if pid and not (_pid_alive(int(pid)) and _pid_is_gateway(int(pid))):
            click.echo(
                f"Gateway process {pid} exited during startup — check {rd.log_file} for the cause.",
                err=True,
            )
            raise SystemExit(1)

    click.echo(f"Gateway started (process {pid}) — endpoint not yet available (check boscli gateway status)")


# ── boscli gateway stop ─────────────────────────────────────────

# Headroom over the drain grace before `stop` escalates to SIGKILL: the reply
# flush, harness teardown, and process exit.
_STOP_DEADLINE_MARGIN = 5.0


def _resolved_stop_grace(ws, state: dict) -> float:
    """The grace the *running* gateway is using, if it published one.

    The process resolved its config at startup; re-reading config from disk can
    disagree with it (the file may have been edited since), which would either
    SIGKILL a handoff still in flight or wait far longer than the operator asked
    for. Falls back to this workspace's config, then to the schema default — and
    says so, because past that point the deadline below is a guess."""
    from bos.config.schema import GatewayConfig

    published = state.get("gateway", {}).get("shutdown_grace_seconds")
    if isinstance(published, (int, float)) and math.isfinite(published) and published >= 0:
        return float(published)
    try:
        return ws.resolve_gateway_config().shutdown_grace_seconds
    except Exception as exc:
        fallback = float(GatewayConfig.model_fields["shutdown_grace_seconds"].default)
        click.echo(f"Could not resolve shutdown_grace_seconds ({exc}); assuming {fallback}s.", err=True)
        return fallback


@gateway.command()
@click.pass_context
def stop(ctx):
    """Stop the running gateway."""
    ws, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, read_state, stop_gateway

    if not is_running(rd):
        click.echo("No gateway is running.", err=True)
        raise SystemExit(1)

    state = read_state(rd)
    click.echo(f"Stopping gateway (process {state.get('pid', '?')})…")

    stop_gateway(rd, signal.SIGTERM)

    # The gateway drains in-flight turns before exiting, so the kill deadline
    # has to outlast that grace — otherwise this command SIGKILLs the very
    # handoff it asked for. Budget the grace plus room to flush and unwind.
    grace = _resolved_stop_grace(ws, state)
    deadline = time.monotonic() + grace + _STOP_DEADLINE_MARGIN
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
    click.echo(f"Started:     {started}")
    click.echo(f"Last active: {last_active}")
    click.echo(f"Uptime:      {uptime_str}")

    gateway_state = state.get("gateway", {})
    host = gateway_state.get("host")
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
    from bos.runner.proc import is_running, lock_is_free

    if is_running(rd):
        ctx.invoke(stop)
        # A dying process unlinks its pid file before the OS drops the flock;
        # poll the lock itself so the fresh gateway doesn't race a still-exiting one.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not lock_is_free(rd):
            time.sleep(0.1)
        ctx.invoke(start)
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
