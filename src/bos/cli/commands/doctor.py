"""``boscli doctor`` — read-only project health check (BEP 9)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import click

from bos.cli.commands.scaffolding import _discover_project, _probe_model
from bos.config import Workspace

_STATUS_MARK = {"ok": ("✓", "green"), "fail": ("✗", "red"), "warn": ("!", "yellow"), "skip": ("-", None)}


@click.command(name="doctor")
@click.option("--probe", "do_probe", is_flag=True, default=False, help="Also make one live model call.")
def doctor(do_probe: bool):
    """Check project config, paths, env vars, and credentials. Read-only."""
    results: list[tuple[str, str, str]] = []
    try:
        ws = _discover_project()
    except click.ClickException:
        raise
    results.append(("ok", "config", f"{ws.config_file.name} parses and validates"))

    results.append(_check_paths(ws))
    results.append(_check_extension_imports(ws))
    results.append(_check_agents(ws))
    env_map = _effective_env(ws)
    results.extend(_check_env(ws, env_map))
    model = _configured_model(ws, env_map)
    results.append(_check_model(model))
    results.append(_check_credentials(ws, model))
    results.append(_check_gateway(ws))

    if do_probe and model:
        ok, detail = _probe_model(ws.workspace, model)
        results.append(("ok" if ok else "fail", "model probe", detail if not ok else f"{model} responded"))
    else:
        results.append(("skip", "model probe", "skipped (use --probe)" if model else "skipped (no model configured)"))

    failed = False
    for status, label, detail in results:
        mark, color = _STATUS_MARK[status]
        click.echo(f"{click.style(mark, fg=color) if color else mark} {label:<14} {detail}")
        failed = failed or status == "fail"
    if failed:
        raise SystemExit(1)


def _check_paths(ws: Workspace) -> tuple[str, str, str]:
    platform = ws.config.platform
    missing: list[str] = []
    for raw in platform.agent_dirs:
        if not (ws.bos_dir / Path(raw).expanduser()).is_dir():
            missing.append(raw)
    for raw in platform.extensions:
        if raw.startswith((".", "/", "~")) and not (ws.bos_dir / Path(raw).expanduser()).exists():
            missing.append(raw)
    if platform.envfile and not (ws.bos_dir / Path(platform.envfile).expanduser()).is_file():
        missing.append(platform.envfile)
    if missing:
        return ("fail", "paths", f"missing: {', '.join(missing)}")
    return ("ok", "paths", "agent_dirs, extensions, envfile paths exist")


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _check_extension_imports(ws: Workspace) -> tuple[str, str, str]:
    """Module entries in [platform.extensions] and the project's own bos.exts
    entry points must be importable by this interpreter (BEP 9, package archetype)."""
    missing = [e for e in ws.config.platform.extensions if not e.startswith((".", "/", "~")) and not _importable(e)]

    pyproject = ws.workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            targets = data.get("project", {}).get("entry-points", {}).get("bos.exts", {}).values()
        except (tomllib.TOMLDecodeError, OSError):
            targets = []
        for target in targets:
            module = str(target).split(":", 1)[0]
            if not _importable(module):
                missing.append(f"{module} (bos.exts entry point)")

    if missing:
        detail = "not importable: " + ", ".join(missing) + " — run via `uv run boscli …` so the project venv is used"
        return ("fail", "imports", detail)
    return ("ok", "imports", "extension modules import")


def _check_agents(ws: Workspace) -> tuple[str, str, str]:
    try:
        ws.resolve_agents()
    except Exception as exc:
        return ("fail", "agents", f"agent specs failed to load: {exc}")
    names = sorted(ws.config.agents)
    detail = f"{len(names)} agent spec(s) load" + (f" ({', '.join(names)})" if names else "")
    return ("ok", "agents", detail)


def _effective_env(ws: Workspace) -> dict[str, str]:
    env_map: dict[str, str] = {k: v for k, v in os.environ.items()}
    env_map.update(ws.config.platform.envs)
    envfile = ws.config.platform.envfile
    if envfile:
        env_path = ws.bos_dir / Path(envfile).expanduser()
        if env_path.is_file():
            from dotenv import dotenv_values

            env_map.update({k: v or "" for k, v in dotenv_values(env_path).items()})
    return env_map


def _check_env(ws: Workspace, env_map: dict[str, str]) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    missing: list[str] = []
    for channel in ws.config.runtime.channels:
        for key, value in channel.settings.items():
            if key.endswith("_env") and isinstance(value, str) and value and not env_map.get(value):
                missing.append(f"{value} (channel {channel.channel_id!r})")
    if missing:
        results.append(("fail", "env", "unset: " + "; ".join(missing)))
    else:
        results.append(("ok", "env", "all *_env references resolve"))

    api_key_env = ws.config.runtime.gateway.api_key_env
    if api_key_env and not env_map.get(api_key_env):
        results.append(("warn", "gateway auth", f"{api_key_env} unset — gateway runs without an API key"))
    return results


def _configured_model(ws: Workspace, env_map: dict[str, str]) -> str | None:
    model = getattr(ws.config.agent.defaults, "model", None)
    if not model:
        for spec in ws.config.agents.values():
            model = getattr(spec, "model", None)
            if model:
                break
    return model or env_map.get("BOS_MODEL") or None


def _check_model(model: str | None) -> tuple[str, str, str]:
    if model:
        return ("ok", "model", model)
    return ("warn", "model", "no model configured (set [agent.defaults].model or BOS_MODEL)")


_AUTH_FILE_PREFIX = {"codex": "codex_auth", "gemini-cli": "gemini_cli_auth", "antigravity": "antigravity_auth"}


def _check_credentials(ws: Workspace, model: str | None) -> tuple[str, str, str]:
    from bos.core import _get_bos_home

    referenced: set[str] = set()
    if model and "/" in model:
        prefix = model.split("/", 1)[0]
        if prefix in _AUTH_FILE_PREFIX:
            referenced.add(prefix)
    exts = getattr(ws.config.exts, "model_extra", None) or {}
    for impl in exts.get("ep_provider", {}):
        if impl in _AUTH_FILE_PREFIX:
            referenced.add(impl)
    if not referenced:
        return ("skip", "credentials", "no OAuth provider configured")

    auth_dir = _get_bos_home() / "auth"
    missing = [p for p in sorted(referenced) if not list(auth_dir.glob(f"{_AUTH_FILE_PREFIX[p]}.*.json"))]
    if missing:
        details = ", ".join(f"{p} (run `boscli auth {p}`)" for p in missing)
        return ("fail", "credentials", f"not authenticated: {details}")
    return ("ok", "credentials", "auth credentials present for " + ", ".join(sorted(referenced)))


def _check_gateway(ws: Workspace) -> tuple[str, str, str]:
    import socket

    from bos.gateway.state import GatewayRunDir
    from bos.runner.proc import is_running, read_state

    rd = GatewayRunDir(ws.bos_dir)
    if is_running(rd):
        state = read_state(rd)
        return ("ok", "gateway", f"running ({state.get('runtime', 'process')} {state.get('pid', '?')})")

    gateway = ws.config.runtime.gateway
    if not gateway.port:
        return ("ok", "gateway", "not running (dynamic port)")
    host = gateway.host if gateway.host not in ("0.0.0.0", "") else "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, gateway.port))
        return ("ok", "gateway", f"port {gateway.port} free (no gateway running for this project)")
    except OSError:
        return ("fail", "gateway", f"port {gateway.port} is in use by another process")
