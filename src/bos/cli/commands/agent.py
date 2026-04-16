"""``bos start/stop/status/restart/tui`` — agent process lifecycle commands."""

from __future__ import annotations

import asyncio
import signal
import sys
import time

import click

from bos.config import Workspace


def _get_ws_and_rd(ctx):
    workspace = ctx.obj.get("WORKSPACE", ".")
    ws = Workspace(workspace)
    from bos.runner.proc import RunDir

    rd = RunDir(ws.bos_dir)
    return ws, rd


# ── bos start ─────────────────────────────────────────────────


@click.command()
@click.option("--foreground", "-f", is_flag=True, default=False, help="Run in the foreground (don't daemonize).")
@click.pass_context
def start(ctx, foreground: bool):
    """Start the agent actor and channel server."""
    ws, rd = _get_ws_and_rd(ctx)
    ws.bootstrap_platform()

    from bos.runner.proc import is_running, read_state, start_background
    from bos.runner.runner import start as runner_start

    if is_running(rd):
        state = read_state(rd)
        click.echo(f"Agent is already running (PID {state.get('pid')}).", err=True)
        raise SystemExit(1)

    if foreground:
        click.echo("Starting agent in foreground…")
        asyncio.run(runner_start(ws))
        return

    # Daemonize
    argv = [sys.executable, "-m", "bos.runner._main", "--workspace", str(ws.workspace)]
    pid = start_background(argv, rd)
    click.echo(f"Agent starting (PID {pid})…")

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
                    click.echo(f"Agent started (PID {pid}) · ws://{host}:{port}/ws")
                    return
            click.echo(f"Agent started (PID {pid})")
            return

    click.echo(f"Agent started (PID {pid}) — channel info not yet available (check bos status)")


# ── bos stop ──────────────────────────────────────────────────


@click.command()
@click.pass_context
def stop(ctx):
    """Stop the running agent."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running, kill_process, read_state

    if not is_running(rd):
        click.echo("No agent is running.", err=True)
        raise SystemExit(1)

    state = read_state(rd)
    pid = state.get("pid", "?")
    click.echo(f"Stopping agent (PID {pid})…")

    kill_process(rd, signal.SIGTERM)

    # Wait up to 5s for clean exit
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if not is_running(rd):
            break
    else:
        click.echo("Agent did not exit cleanly — sending SIGKILL")
        try:
            kill_process(rd, signal.SIGKILL)
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
    pid = state.get("pid", "—")
    started = state.get("started_at", "—")
    last_active = state.get("last_active", "—")

    # Uptime
    uptime_str = "—"
    try:
        from datetime import datetime

        started_dt = datetime.fromisoformat(started)
        uptime = datetime.now() - started_dt
        h, rem = divmod(int(uptime.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    except Exception:
        pass

    click.echo(f"Status:      {status_str}")
    click.echo(f"PID:         {pid}")
    click.echo(f"Started:     {started}")
    click.echo(f"Last active: {last_active}")
    click.echo(f"Uptime:      {uptime_str}")

    for ch in state.get("channels", []):
        name = ch.get("name", "?")
        host = ch.get("host", "?")
        port = ch.get("port", "?")
        addr = ch.get("address", "?")
        click.echo(f"Channel:     {name} @ {addr} → ws://{host}:{port}/ws")


# ── bos restart ───────────────────────────────────────────────


@click.command()
@click.pass_context
def restart(ctx):
    """Restart the agent (stop then start)."""
    # Re-invoke stop (ignore failure if not running)
    ws, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import is_running

    if is_running(rd):
        ctx.invoke(stop)
        time.sleep(0.5)

    ctx.invoke(start)


# ── bos tui ───────────────────────────────────────────────────


@click.command()
@click.option("--host", default=None, help="Channel host (overrides agent.state).")
@click.option("--port", default=None, type=int, help="Channel port (overrides agent.state).")
@click.option("--address", default="tui", show_default=True, help="This TUI's mailbox address.")
@click.pass_context
def tui(ctx, host: str | None, port: int | None, address: str):
    """Connect the TUI to a running agent via the HTTP channel."""
    _, rd = _get_ws_and_rd(ctx)
    from bos.runner.proc import read_state

    # Discover endpoint from agent.state
    if not (host and port):
        state = read_state(rd)
        for ch in state.get("channels", []):
            if ch.get("name") == "HttpChannel":
                host = host or ch.get("host", "127.0.0.1")
                port = port or ch.get("port")
                break

    if not host or not port:
        raise click.UsageError(
            "Could not determine channel endpoint. Use --host and --port, or make sure the agent is running."
        )

    from bos.cli.tui_app import run_chat_tui
    from bos.extensions.channels.http_client import HttpChannelClient

    async def _run():
        client = HttpChannelClient(host=host, port=port, address=address)
        await client.connect()
        try:
            await run_chat_tui(client)
        finally:
            await client.aclose()

    asyncio.run(_run())
