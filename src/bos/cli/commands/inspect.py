"""``boscli inspect`` — read-only dump of harness-level information.

Reports what the active workspace resolves to: paths and config source, the
selected harness implementations, gateway status, default models, actors, and
the capabilities (agents, plugins, tools, skills) and extension points available
after bootstrap. The ``agent NAME`` subcommand instead reflects a single
agent's resolved model, plugins, tools, and skills by building it through the
harness.
Read-only — it never starts the gateway or mutates config.
"""

from __future__ import annotations

import asyncio
import json as json_lib
import os
from typing import Any

import click

from bos.config.schema import AgentSection, HarnessConfig, RuntimeConfig


def _harness_info(ws, config_arg: str | None) -> dict[str, Any]:
    from bos.cli.commands.agent import _builtin_preset_name_for_config_file

    preset = _builtin_preset_name_for_config_file(ws.config_file)
    if preset is not None:
        source = "preset"
    elif config_arg:
        source = "file"
    else:
        source = "project"

    harness = ws.config.harness or HarnessConfig()
    return {
        "bos_dir": str(ws.bos_dir),
        "workspace": str(ws.workspace),
        "config_file": str(ws.config_file) if ws.config_file else None,
        "source": source,
        "is_preset": preset is not None,
        "preset_name": preset,
        "implementations": {
            "consolidator": harness.consolidator,
            "chat_store": harness.chat_store,
            "mail_route": harness.mail_route,
            "job_runner": harness.job_runner,
            "interceptors": list(harness.interceptors),
        },
    }


def _gateway_info(ws) -> dict[str, Any]:
    from bos.gateway.state import GatewayRunDir
    from bos.runner.proc import is_running, read_state

    rd = GatewayRunDir(ws.bos_dir)
    running = is_running(rd)
    state = read_state(rd)
    if not running and not state:
        return {"running": False}

    gateway_state = state.get("gateway", {})
    host = gateway_state.get("host")
    if state.get("runtime") == "docker" and host == "0.0.0.0":
        host = "127.0.0.1"
    port = gateway_state.get("port")
    return {
        "running": running,
        "runtime": state.get("runtime", "process"),
        "pid": state.get("pid"),
        "container_id": state.get("container_id"),
        "started_at": state.get("started_at"),
        "endpoint": f"http://{host}:{port}" if host and port else None,
        "actors": list(state.get("actors", {})),
        "channels": list(state.get("channels", {})),
    }


def _default_models_info(ws) -> dict[str, str | None]:
    agent_model = getattr((ws.config.agent or AgentSection()).defaults, "model", None) or os.environ.get("BOS_MODEL")

    exts = ws.config.exts.model_dump() if getattr(ws.config, "exts", None) is not None else {}
    cons_cfg = exts.get("ep_consolidator", {}).get("LLMConsolidator", {}) or {}
    consolidator_model = (
        cons_cfg.get("model") or os.environ.get("BOS_CONSOLIDATOR_MODEL") or os.environ.get("BOS_MODEL")
    )
    return {"agent": agent_model, "consolidator": consolidator_model}


def _actors_info(ws) -> dict[str, Any]:
    runtime = ws.config.runtime or RuntimeConfig()
    return {
        "main_actor": runtime.main_actor,
        "actors": {
            name: {"agent": cfg.agent, "display_name": cfg.display_name} for name, cfg in runtime.actors.items()
        },
    }


async def _skills_info(ws) -> dict[str, str]:
    """Resolve the SkillsPlugin loader and return {skill_name: description}."""
    from bos.core import _deep_merge
    from bos.plugins.skills.plugin import SkillsHarnessPlugin, pep_skills_loader

    cfg = dict(SkillsHarnessPlugin().default_config())
    if getattr(ws.config, "exts", None) is not None:
        user = ws.config.exts.model_dump().get("ep_plugin", {}).get("SkillsPlugin", {}) or {}
        cfg = _deep_merge(cfg, user)

    loader_ext = pep_skills_loader.get(cfg.get("loader", "FileSystemSkillsLoader"))
    if loader_ext is None:
        return {}
    loader = loader_ext.fn(bos_dir=ws.bos_dir, skill_dirs=list(cfg.get("skill_dirs", ["skills"])))
    try:
        metas = await loader.search_skills()
    finally:
        from bos.core import _aclose

        await _aclose(loader)
    return {name: (meta.description or "").strip() for name, meta in metas.items()}


def _capabilities_info(ws) -> dict[str, Any]:
    from bos.core import AgentRegistry, ep_plugin, ep_tool

    skills: dict[str, str] = {}
    try:
        skills = asyncio.run(_skills_info(ws))
    except Exception as exc:  # skills are plugin-provided; never fail the whole report
        skills = {"<error>": str(exc)}

    return {
        "agents": AgentRegistry.describe(),
        "plugins": ep_plugin.describe(),
        "tools": ep_tool.describe(),
        "skills": skills,
    }


def _extension_points_info() -> dict[str, dict[str, str]]:
    # The built-in adapters (LLMConsolidator, JsonlChatStore, …) register here,
    # mirroring what AgentHarness does at open-time, so the listing reflects the
    # impls actually resolvable — including the harness defaults.
    import bos.core.defaults  # noqa: F401
    from bos.core import (
        ep_channel,
        ep_chat_store,
        ep_consolidator,
        ep_job_runner,
        ep_mail_route,
        ep_provider,
        ep_turn_interceptor,
    )

    return {
        "consolidator": ep_consolidator.describe(),
        "chat_store": ep_chat_store.describe(),
        "mail_route": ep_mail_route.describe(),
        "job_runner": ep_job_runner.describe(),
        "turn_interceptor": ep_turn_interceptor.describe(),
        "provider": ep_provider.describe(),
        "channel": ep_channel.describe(),
    }


async def _agent_capabilities(ws, agent_kind: str, agent_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reflect the plugins, tools, and skills a specific agent resolves to.

    The inspector is authoritative over agent internals: it builds the agent
    through the harness exactly as the runtime would, then reads its private
    resolved tool set and bound plugins rather than re-deriving config — so it
    reports precisely what that agent sees, after include/exclude filtering and
    plugin-contributed tools are merged in. ``agent_cfg`` applies per-actor
    overrides so an actor's effective view matches what the gateway would run.
    """
    from bos.core import AgentRegistry, _pick_collection
    from bos.plugins.skills.plugin import SkillsAgentPlugin

    if not AgentRegistry.has_registered(agent_kind):
        known = ", ".join(sorted(AgentRegistry.describe())) or "none"
        raise click.ClickException(f"Agent {agent_kind!r} is not a registered agent kind. Known: {known}")

    harness = ws.harness()
    await harness.__aenter__()
    try:
        agent = await harness.create_agent(agent_kind, agent_cfg=agent_cfg)
        bound = list(getattr(agent._prompt_provider, "_plugins", []))

        skills: dict[str, str] = {}
        for plugin in bound:
            if not isinstance(plugin, SkillsAgentPlugin):
                continue
            try:
                metas = await plugin._loader.search_skills()
            except Exception as exc:
                skills = {"<error>": str(exc)}
                break
            metas = _pick_collection(metas, plugin._allow, plugin._exclude)
            skills = {name: (meta.description or "").strip() for name, meta in metas.items()}

        return {
            "kind": agent._kind,
            "name": agent._name,
            "model": agent._model,
            "plugins": sorted({plugin.name for plugin in bound}),
            "tools": agent._tools.describe_usage(),
            "skills": skills,
        }
    finally:
        await harness.__aexit__(None, None, None)


def _collect(ctx, workspace_dir: str | None, agent_kind: str | None, actor_name: str | None = None) -> dict[str, Any]:
    from bos.cli.commands.agent import _build_workspace_for_daemon

    ws = _build_workspace_for_daemon(ctx, workspace_dir)
    config_arg = ctx.obj.get("CONFIG")

    report: dict[str, Any] = {"harness": _harness_info(ws, config_arg)}

    # An actor names a configured runtime actor; resolve it to the agent it runs
    # (with its per-actor agent_cfg overrides) so the rest of the flow mirrors
    # the agent inspection and reflects what the gateway would actually run.
    actor_meta: dict[str, Any] | None = None
    actor_overrides: dict[str, Any] | None = None
    if actor_name:
        from bos.config.schema import _agent_config_to_core_kwargs

        runtime = ws.config.runtime or RuntimeConfig()
        actor_cfg = runtime.actors.get(actor_name)
        if actor_cfg is None:
            known = ", ".join(sorted(runtime.actors)) or "none"
            raise click.ClickException(f"Actor {actor_name!r} is not a configured actor. Known: {known}")
        agent_kind = actor_cfg.agent
        # Mirror ActorManager._start_record: per-actor overrides plus the
        # actor's name and multi-actor history attribution.
        actor_overrides = _agent_config_to_core_kwargs(actor_cfg.agent_cfg)
        actor_overrides["agent_name"] = actor_name
        actor_overrides["history_attribution"] = len(runtime.actors) > 1
        actor_meta = {
            "actor": actor_name,
            "agent": agent_kind,
            "display_name": actor_cfg.display_name,
            "is_main": actor_name == runtime.main_actor,
        }

    scoped = bool(agent_kind or actor_name)
    # Gateway/models/actors are process-level concerns, irrelevant to a single
    # agent's or actor's resolved capabilities.
    if not scoped:
        report["gateway"] = _gateway_info(ws)
        report["default_models"] = _default_models_info(ws)
        report["actors"] = _actors_info(ws)

    # Loading external agents + extensions populates the registries the
    # capability sections read from. Best-effort: a broken extension should not
    # blank out the paths/gateway info already gathered.
    try:
        ws.resolve_agents()
        ws.bootstrap_platform()
    except Exception as exc:
        if scoped:
            report["agent"] = {"kind": agent_kind, "error": str(exc)}
            if actor_meta is not None:
                report["agent"]["actor"] = actor_meta
        else:
            report["capabilities"] = {"<error>": str(exc)}
            report["extension_points"] = {}
        return report

    if scoped:
        assert agent_kind is not None
        agent_report = asyncio.run(_agent_capabilities(ws, agent_kind, actor_overrides))
        if actor_meta is not None:
            agent_report["actor"] = actor_meta
        report["agent"] = agent_report
    else:
        report["capabilities"] = _capabilities_info(ws)
        report["extension_points"] = _extension_points_info()

    return report


# ── rendering ────────────────────────────────────────────────────


def _render_text(report: dict[str, Any]) -> None:
    from rich.console import Console

    console = Console()
    h = report["harness"]

    console.print("[bold]Harness[/]")
    console.print(f"  workspace:   {h['workspace']}")
    console.print(f"  bos_dir:     {h['bos_dir']}")
    console.print(f"  config:      {h['config_file']}")
    source = h["source"]
    if h["is_preset"]:
        source = f"preset ({h['preset_name']})"
    console.print(f"  source:      {source}")
    impls = h["implementations"]
    console.print(f"  consolidator: {impls['consolidator']}")
    console.print(f"  chat_store:   {impls['chat_store']}")
    console.print(f"  mail_route:   {impls['mail_route']}")
    console.print(f"  job_runner:   {impls['job_runner']}")
    interceptors = ", ".join(impls["interceptors"]) or "—"
    console.print(f"  interceptors: {interceptors}")

    g = report.get("gateway")
    if g is not None:
        console.print("\n[bold]Gateway[/]")
        if not g.get("running") and not g.get("pid"):
            console.print("  [red]○ not running[/]")
        else:
            mark = "[green]● running[/]" if g.get("running") else "[red]○ stopped[/]"
            console.print(f"  status:      {mark}")
            console.print(f"  runtime:     {g.get('runtime', '—')}")
            if g.get("pid"):
                console.print(f"  pid:         {g['pid']}")
            if g.get("endpoint"):
                console.print(f"  endpoint:    {g['endpoint']}")

    dm = report.get("default_models")
    if dm is not None:
        console.print("\n[bold]Default Models[/]")
        console.print(f"  agent:        {dm['agent'] or '— (set agent.defaults.model or BOS_MODEL)'}")
        console.print(
            f"  consolidator: {dm['consolidator'] or '— (set exts.ep_consolidator.LLMConsolidator.model or BOS_MODEL)'}"
        )

    ac = report.get("actors")
    if ac is not None:
        from rich.table import Table

        actors = ac["actors"]
        console.print(f"\n[bold]Actors[/] ({len(actors)})")
        table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 2))
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(overflow="fold")
        for name, actor in actors.items():
            label = f"{name} [bright_yellow]★[/]" if name == ac["main_actor"] else name
            details = f"agent={actor['agent']}"
            if actor.get("display_name"):
                details += f"; {actor['display_name']}"
            table.add_row(label, details)
        console.print(table)

    if "agent" in report:
        _render_agent(console, report["agent"])
        return

    caps = report.get("capabilities", {})
    for label in ("agents", "plugins", "tools", "skills"):
        _render_caps_table(console, label.capitalize(), caps.get(label, {}))

    eps = report.get("extension_points", {})
    if eps:
        console.print("\n[bold]Extension points[/] [dim](available implementations)[/]")
        for ep_name, impls in eps.items():
            names = ", ".join(sorted(impls)) or "—"
            console.print(f"  {ep_name}: {names}")


def _render_agent(console, a: dict[str, Any]) -> None:
    actor = a.get("actor")
    if actor is not None:
        star = " [bright_yellow]★[/]" if actor.get("is_main") else ""
        display = f"; {actor['display_name']}" if actor.get("display_name") else ""
        console.print(f"\n[bold]Actor[/] [cyan]{actor['actor']}[/]{star}{display}")

    name = a.get("name")
    suffix = f" [dim](name={name})[/]" if name and name != a.get("kind") else ""
    console.print(f"\n[bold]Agent[/] [cyan]{a.get('kind')}[/]{suffix}")
    if a.get("error"):
        console.print(f"  [red]error: {a['error']}[/]")
        return
    console.print(f"  model:        {a.get('model') or '— (set agent.defaults.model or BOS_MODEL)'}")
    plugins = a.get("plugins", [])
    console.print(f"\n[bold]Plugins[/] ({len(plugins)})")
    console.print("  " + (", ".join(plugins) or "—"))
    _render_caps_table(console, "Tools", a.get("tools", {}))
    _render_caps_table(console, "Skills", a.get("skills", {}))


def _render_caps_table(console, label: str, items: dict[str, str]) -> None:
    from rich.table import Table

    console.print(f"\n[bold]{label}[/] ({len(items)})")
    if not items:
        console.print("  —")
        return
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 2))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(overflow="fold")
    for name, desc in sorted(items.items()):
        table.add_row(name, _shorten(desc))
    console.print(table)


def _shorten(text: str, limit: int = 100) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(json_lib.dumps(report, indent=2, default=str))
    else:
        _render_text(report)


@click.group(name="inspect", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the report as JSON.")
@click.option(
    "-w",
    "--workspace",
    "workspace_dir",
    default=None,
    help="Override the workspace directory (defaults to '.' or project root).",
)
@click.pass_context
def inspect(ctx, as_json: bool, workspace_dir: str | None):
    """Show harness-level info: paths, gateway, capabilities, extension points.

    Use the ``agent`` subcommand to instead report the plugins, tools, and
    skills a single agent resolves to (its filtered, plugin-merged view), or
    ``actor`` to do the same for the agent a configured actor runs.

    Read-only. Honors ``-c <preset|file>`` like the gateway commands; with no
    config it discovers the project, falling back to the ``default`` preset.
    """
    ctx.obj["INSPECT_JSON"] = as_json
    ctx.obj["INSPECT_WORKSPACE"] = workspace_dir
    if ctx.invoked_subcommand is None:
        _emit(_collect(ctx, workspace_dir, None), as_json)


@inspect.command(name="agent")
@click.argument("agent_kind", metavar="NAME")
@click.pass_context
def inspect_agent(ctx, agent_kind: str):
    """Inspect a single agent: the plugins, tools, and skills it resolves to.

    Builds the agent through the harness and reports its filtered,
    plugin-merged view. Read-only.
    """
    as_json = ctx.obj.get("INSPECT_JSON", False)
    workspace_dir = ctx.obj.get("INSPECT_WORKSPACE")
    _emit(_collect(ctx, workspace_dir, agent_kind), as_json)


@inspect.command(name="actor")
@click.argument("actor_name", metavar="NAME")
@click.pass_context
def inspect_actor(ctx, actor_name: str):
    """Inspect a configured actor: the agent it runs and that agent's
    resolved plugins, tools, and skills. Read-only.
    """
    as_json = ctx.obj.get("INSPECT_JSON", False)
    workspace_dir = ctx.obj.get("INSPECT_WORKSPACE")
    _emit(_collect(ctx, workspace_dir, None, actor_name=actor_name), as_json)
