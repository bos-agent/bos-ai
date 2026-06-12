"""``boscli project`` — guided scaffolding, generators, and doctor (BEP 9)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import tomllib
from pathlib import Path

import click

from bos.cli.scaffold import (
    ARCHETYPES,
    frontmatter_text,
    render_template,
    scaffold_workspace,
    toml_multiline_text,
)
from bos.config import ConfigNotFoundError, Workspace, WorkspaceResolutionError, initialize_workspace

_DEFAULT_PURPOSE = "A general-purpose personal agent."
_DEFAULT_AGENT_TOOLS = '["ReadFile", "GrepSearch", "GlobSearch"]'
_AGENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_SPECIALIST_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}$")

# litellm model-prefix → API key env var. Maintained by BOS; occasional drift
# fixes against litellm are an accepted cost (BEP 9). Always overridable at the prompt.
_API_KEY_ENV_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("anthropic/", "ANTHROPIC_API_KEY"),
    ("claude-", "ANTHROPIC_API_KEY"),
    ("openai/", "OPENAI_API_KEY"),
    ("gpt-", "OPENAI_API_KEY"),
    ("gemini/", "GEMINI_API_KEY"),
    ("groq/", "GROQ_API_KEY"),
    ("mistral/", "MISTRAL_API_KEY"),
    ("deepseek/", "DEEPSEEK_API_KEY"),
    ("xai/", "XAI_API_KEY"),
    ("openrouter/", "OPENROUTER_API_KEY"),
)

_OAUTH_PROVIDERS = {
    "codex": ("codex", "codex/gpt-5.3-codex"),
    "gemini-cli": ("gemini_cli", "gemini-cli/gemini-2.5-pro"),
    "antigravity": ("antigravity", "antigravity/gemini-3-pro-preview"),
}


@click.group(name="project")
def project():
    """Create and grow a BOS agent project."""


# ── boscli project init ─────────────────────────────────────────


@project.command(name="init")
@click.argument("directory", default=".")
@click.option("--archetype", type=click.Choice(ARCHETYPES), default=None, help="Starting topology.")
@click.option("--model", default=None, help="litellm model id (implies the API-key provider path).")
@click.option("--purpose", default=None, help="What the agent project is for.")
@click.option("--yes", is_flag=True, default=False, help="Accept defaults for unanswered questions; never prompt.")
@click.option("--minimal", is_flag=True, default=False, help="Only copy the reference config template, no wizard.")
@click.option("--flat", is_flag=True, default=False, help="Write a root bos.toml instead of the default .bos/ layout.")
@click.option("--git/--no-git", "init_git", default=None, help="Run git init and create a .gitignore.")
@click.option("--no-probe", is_flag=True, default=False, help="Skip the live model credential check.")
@click.option("--no-generate", is_flag=True, default=False, help="Skip LLM generation of team specialists.")
@click.option("--name", "pkg_name_opt", default=None, help="Package name (package archetype; default: dir name).")
@click.pass_context
def init(
    ctx, directory, archetype, model, purpose, yes, minimal, flat, init_git, no_probe, no_generate, pkg_name_opt
):
    """Initialize a BOS project with a guided, runnable baseline."""
    workspace_path = Path(directory).expanduser().resolve()
    dotbos = not flat

    if minimal:
        try:
            bos_dir = initialize_workspace(workspace_path, dotbos=dotbos)
        except WorkspaceResolutionError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Initialized BOS workspace at {bos_dir}")
        if init_git:
            _git_init(workspace_path)
        return

    project_name = workspace_path.name or "bos-project"

    if purpose is None:
        purpose = _DEFAULT_PURPOSE if yes else click.prompt("What is this agent project for?", default=_DEFAULT_PURPOSE)
    if archetype is None:
        archetype = "assistant" if yes else _prompt_archetype()

    pkg_name = None
    if archetype == "package":
        if flat:
            raise click.ClickException(
                "--flat is not supported with the package archetype: the project root belongs to "
                "the Python package, so the config always lives in .bos/."
            )
        pkg_name = _normalize_pkg_name(pkg_name_opt or project_name)

    model, env_pairs = _provider_step(ctx, model, yes)

    env_pairs = dict(env_pairs)
    if archetype == "service":
        env_pairs.setdefault("BOS_GATEWAY_API_KEY", secrets.token_urlsafe(24))
    if archetype == "telegram-bot":
        env_pairs.setdefault("TELEGRAM_BOT_TOKEN", "")

    specialists = _fallback_specialists(purpose)
    context = _build_context(project_name, purpose, archetype, model, dotbos, specialists)
    if pkg_name:
        context["pkg_name"] = pkg_name
        context["dist_name"] = pkg_name.replace("_", "-")
    agent_files = _specialist_files(specialists, purpose) if archetype == "team" else {}

    try:
        result = scaffold_workspace(
            workspace_path,
            archetype,
            context,
            dotbos=dotbos,
            env_content=_env_content(env_pairs),
            agent_files=agent_files,
        )
    except WorkspaceResolutionError as exc:
        raise click.ClickException(str(exc))

    written = ", ".join(sorted({_top_level_name(p, workspace_path) for p in result.written}))
    click.echo(f"Initialized {archetype} project at {workspace_path}")
    click.echo(f"Wrote {written}")

    probe_ok = False
    if model and not no_probe:
        probe_ok, detail = _probe_model(workspace_path, model)
        if probe_ok:
            click.echo(f"Config validates ✓ · credential probe ✓ ({detail})")
        else:
            click.echo(f"Config validates ✓ · credential probe ✗ — {detail}", err=True)
            click.echo(
                "  The project was still created; fix credentials and run `boscli project doctor --probe`.", err=True
            )
    else:
        click.echo("Config validates ✓ · credential probe skipped")

    if archetype == "team" and model and probe_ok and not no_generate:
        generated = _generate_specialists(model, purpose)
        if generated:
            _apply_generated_specialists(result, context, generated, purpose)
            names = ", ".join(s["name"] for s in generated)
            click.echo(f"Generated team specialists: {names}")
        else:
            click.echo("Specialist generation failed — keeping the built-in researcher/writer templates.", err=True)

    if init_git is None:
        default_git = not _inside_git_repo(workspace_path)
        init_git = default_git if yes else click.confirm("Initialize a git repository?", default=default_git)
    if init_git:
        _git_init(workspace_path)

    click.echo("")
    click.echo("Next steps:")
    # The package archetype must run through the project venv so the package is importable.
    prefix = "uv run " if archetype == "package" else ""
    click.echo(f"  {prefix}boscli gateway start")
    click.echo(f"  {prefix}boscli tui")
    if model:
        click.echo(f'  Try: "{context["first_prompt"]}"')
    else:
        click.echo(f"  Set a model first: edit `model = ...` in {result.config_file.name} (or export BOS_MODEL).")


def _prompt_archetype() -> str:
    click.echo("Choose a starting topology:")
    descriptions = {
        "assistant": "single agent with memory and skills",
        "team": "a coordinator agent that delegates to specialists",
        "service": "headless HTTP gateway, API-first",
        "telegram-bot": "an agent wired to a Telegram channel",
        "package": "an installable Python extension package (tools, channels, providers)",
    }
    for i, name in enumerate(ARCHETYPES, start=1):
        click.echo(f"  {i}. {name} — {descriptions[name]}")
    choice = click.prompt("Archetype", type=click.IntRange(1, len(ARCHETYPES)), default=1)
    return ARCHETYPES[choice - 1]


def _provider_step(ctx, model: str | None, yes: bool) -> tuple[str | None, dict[str, str]]:
    """Resolve the model id and any env vars to capture into .env."""
    if model:
        return model, _api_key_env_pairs(model, yes)
    if yes:
        return None, {}

    click.echo("Choose a model provider:")
    click.echo("  1. API key (any litellm model id, e.g. anthropic/claude-…, gpt-…)")
    click.echo("  2. OpenAI Codex subscription (boscli auth codex)")
    click.echo("  3. Gemini CLI subscription (boscli auth gemini-cli)")
    click.echo("  4. Google Antigravity (boscli auth antigravity)")
    click.echo("  5. Skip — configure the model later")
    choice = click.prompt("Provider", type=click.IntRange(1, 5), default=1)

    if choice == 5:
        return None, {}
    if choice == 1:
        model = click.prompt("Model id (litellm format)", default="anthropic/claude-sonnet-4-6")
        return model, _api_key_env_pairs(model, yes=False)

    provider = ("codex", "gemini-cli", "antigravity")[choice - 2]
    auth_attr, default_model = _OAUTH_PROVIDERS[provider]
    try:
        from bos.cli.commands import auth as auth_module

        ctx.invoke(getattr(auth_module, auth_attr))
    except click.ClickException as exc:
        click.echo(f"Authentication failed ({exc.message}) — continuing; run `boscli auth {provider}` later.", err=True)
    except Exception as exc:  # OAuth flows talk to the network; never abort init on failure
        click.echo(f"Authentication failed ({exc}) — continuing; run `boscli auth {provider}` later.", err=True)
    model = click.prompt("Model id", default=default_model)
    return model, {}


def _api_key_env_pairs(model: str, yes: bool) -> dict[str, str]:
    env_name = _infer_key_env(model)
    if not yes:
        env_name = click.prompt("API key env var", default=env_name)
    if os.environ.get(env_name):
        return {}  # already provided by the environment; do not copy secrets into .env
    if yes:
        return {env_name: ""}
    value = click.prompt(
        f"{env_name} (stored in .env, leave empty to skip)", default="", hide_input=True, show_default=False
    )
    return {env_name: value}


def _infer_key_env(model: str) -> str:
    lowered = model.lower()
    for prefix, env_name in _API_KEY_ENV_BY_PREFIX:
        if lowered.startswith(prefix):
            return env_name
    return "OPENAI_API_KEY"


def _build_context(
    project_name: str,
    purpose: str,
    archetype: str,
    model: str | None,
    dotbos: bool,
    specialists: list[dict[str, str]],
) -> dict[str, str]:
    if model:
        model_line = f'model = "{model}"'
    else:
        model_line = '# model = ""  # set a litellm model id, or export BOS_MODEL'
    first_prompt = "use the WordCount tool to count the words in this sentence"
    notes = {
        "service": (
            "\n## Service notes\n\nThe gateway requires the API key from `.env` "
            "(`BOS_GATEWAY_API_KEY`) on HTTP and WebSocket requests.\n"
        ),
        "telegram-bot": (
            "\n## Telegram notes\n\nCreate a bot with @BotFather, put its token in `.env` as "
            "`TELEGRAM_BOT_TOKEN`, then restart the gateway.\n"
        ),
    }
    return {
        "project_name": project_name,
        "purpose": purpose.strip(),
        "purpose_comment": " ".join(purpose.split()),
        "purpose_toml": toml_multiline_text(purpose),
        "archetype": archetype,
        "model_line": model_line,
        "config_path": ".bos/config.toml" if dotbos else "bos.toml",
        "first_prompt": first_prompt,
        "archetype_notes": notes.get(archetype, ""),
        "specialist_a": specialists[0]["name"],
        "specialist_b": specialists[1]["name"],
        "specialist_a_display": specialists[0]["name"].replace("-", " ").title(),
    }


def _env_content(env_pairs: dict[str, str]) -> str:
    if not env_pairs:
        return "# Secrets for this project (gitignored). Loaded via [platform].envfile.\n"
    lines = [f"{name}={value}" for name, value in env_pairs.items()]
    return "# Secrets for this project (gitignored). Loaded via [platform].envfile.\n" + "\n".join(lines) + "\n"


def _top_level_name(path: Path, workspace: Path) -> str:
    rel = path.relative_to(workspace)
    head = rel.parts[0]
    return f"{head}/" if len(rel.parts) > 1 else head


def _normalize_pkg_name(raw: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z]+", "_", raw).strip("_").lower()
    if not name.isidentifier() or name[0].isdigit():
        raise click.ClickException(f"Cannot derive a Python package name from {raw!r} — pass one with --name.")
    return name


def _inside_git_repo(workspace: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=workspace, capture_output=True, text=True
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except OSError:
        return False


def _git_init(workspace: Path) -> None:
    try:
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        click.echo(f"Initialized git repository in {workspace}")
    except (OSError, subprocess.CalledProcessError) as exc:
        click.echo(f"git init failed: {exc}", err=True)


# ── model probe and specialist generation ──────────────────────


def _bootstrapped_workspace(workspace_path: Path) -> Workspace:
    ws = Workspace.from_discovery(workspace_path)
    ws.resolve_agents()
    ws.bootstrap_platform()
    return ws


def _complete(model: str, prompt: str) -> str:
    from bos.core import LLMClient

    async def _call() -> str:
        response = await LLMClient().complete([{"role": "user", "content": prompt}], model=model)
        return response.text or ""

    return asyncio.run(_call())


def _probe_model(workspace_path: Path, model: str) -> tuple[bool, str]:
    try:
        _bootstrapped_workspace(workspace_path)
        _complete(model, "Reply with the single word: pong")
        return True, "1 model call"
    except Exception as exc:
        return False, str(exc) or type(exc).__name__


def _generate_specialists(model: str, purpose: str) -> list[dict[str, str]] | None:
    prompt = render_template("team/specialists_prompt.txt", {"purpose": " ".join(purpose.split())})
    for _ in range(2):
        try:
            specs = _parse_specialists(_complete(model, prompt))
        except Exception:
            specs = None
        if specs:
            return specs
    return None


def _parse_specialists(text: str) -> list[dict[str, str]] | None:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not (isinstance(data, list) and len(data) == 2):
        return None
    specs: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        description = item.get("description")
        system_prompt = item.get("system_prompt")
        if not (isinstance(name, str) and isinstance(description, str) and isinstance(system_prompt, str)):
            return None
        if not _SPECIALIST_NAME_RE.fullmatch(name) or name == "main":
            return None
        if not description.strip() or not system_prompt.strip():
            return None
        specs.append({"name": name, "description": description, "system_prompt": system_prompt})
    if specs[0]["name"] == specs[1]["name"]:
        return None
    return specs


def _fallback_specialists(purpose: str) -> list[dict[str, str]]:
    purpose = " ".join(purpose.split())
    return [
        {
            "name": "researcher",
            "description": f"Gathers and verifies the information needed for: {purpose}",
            "system_prompt": (
                "You are the research specialist of this project.\n\n"
                f"Project purpose: {purpose}\n\n"
                "Investigate the questions the coordinator delegates to you: gather facts, verify them "
                "against their sources, and report findings with clear confidence levels. Stay within the "
                "delegated question and return a concise, structured result."
            ),
        },
        {
            "name": "writer",
            "description": f"Drafts and polishes written output for: {purpose}",
            "system_prompt": (
                "You are the writing specialist of this project.\n\n"
                f"Project purpose: {purpose}\n\n"
                "Turn the coordinator's notes and findings into clear, well-structured prose for the "
                "intended audience. Preserve factual content exactly; improve structure, flow, and tone. "
                "Return only the finished text."
            ),
        },
    ]


def _render_agent_md(description: str, system_prompt: str, tools_list: str = _DEFAULT_AGENT_TOOLS) -> str:
    return render_template(
        "_shared/agent.md.tmpl",
        {
            "description": frontmatter_text(description),
            "tools_list": tools_list,
            "system_prompt": system_prompt.strip(),
        },
    )


def _specialist_files(specialists: list[dict[str, str]], purpose: str) -> dict[str, str]:
    return {f"{s['name']}.md": _render_agent_md(s["description"], s["system_prompt"]) for s in specialists}


def _apply_generated_specialists(
    result, context: dict[str, str], generated: list[dict[str, str]], purpose: str
) -> None:
    """Replace the fallback specialist files and re-render the pristine config."""
    agents_dir = result.bos_dir / "agents"
    for old in _fallback_specialists(purpose):
        (agents_dir / f"{old['name']}.md").unlink(missing_ok=True)
    for spec in generated:
        (agents_dir / f"{spec['name']}.md").write_text(
            _render_agent_md(spec["description"], spec["system_prompt"]), encoding="utf-8"
        )
    context = context | {
        "specialist_a": generated[0]["name"],
        "specialist_b": generated[1]["name"],
        "specialist_a_display": generated[0]["name"].replace("-", " ").title(),
    }
    result.config_file.write_text(render_template("team/bos.toml.tmpl", context), encoding="utf-8")


# ── boscli project add ──────────────────────────────────────────


@project.group(name="add")
def add():
    """Add an agent, tool, or channel to the current project."""


def _discover_project() -> Workspace:
    from bos.cli.commands.agent import _echo_config_source

    try:
        ws = Workspace.from_discovery(".")
    except ConfigNotFoundError:
        raise click.ClickException("No BOS project found here. Run `boscli project init` first.")
    if ws.config_file is None:
        raise click.ClickException("Project config file could not be resolved.")
    _echo_config_source(ws)
    return ws


@add.command(name="agent")
@click.argument("name")
@click.option("--description", default=None, help="One-line description shown for delegation.")
@click.option("--actor", "as_actor", is_flag=True, default=False, help="Also expose the agent as a named actor.")
def add_agent(name: str, description: str | None, as_actor: bool):
    """Create agents/NAME.md from the standard agent skeleton."""
    if not _AGENT_NAME_RE.fullmatch(name):
        raise click.ClickException(f"Invalid agent name {name!r} (letters, digits, '-', '_'; starts with a letter).")
    ws = _discover_project()
    agents_dir = ws.bos_dir / "agents"
    target = agents_dir / f"{name}.md"
    if target.exists():
        raise click.ClickException(f"{target} already exists.")

    description = description or f"Specialist agent {name}"
    system_prompt = (
        f"You are {name}, a specialist agent of this project.\n\n"
        "TODO: describe this agent's expertise, working style, and output expectations."
    )
    agents_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_agent_md(description, system_prompt), encoding="utf-8")
    click.echo(f"Created {target}")

    if "./agents" not in ws.config.platform.agent_dirs and "agents" not in ws.config.platform.agent_dirs:
        click.echo("Note: ./agents is not in [platform].agent_dirs — add this line for the file to load:", err=True)
        click.echo('  agent_dirs = ["./agents"]', err=True)

    if as_actor:
        display = name.replace("-", " ").replace("_", " ").title()
        _append_actor(ws.config_file, name, display)


def _append_actor(config_file: Path, name: str, display_name: str) -> None:
    import tomlkit

    try:
        doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
        runtime = doc.setdefault("runtime", tomlkit.table())
        actors = runtime.setdefault("actors", tomlkit.table())
        if name in actors:
            raise click.ClickException(f"Actor {name!r} already exists in {config_file.name}.")
        actor = tomlkit.table()
        actor["agent"] = name
        actor["display_name"] = display_name
        actors[name] = actor
        config_file.write_text(tomlkit.dumps(doc), encoding="utf-8")
        click.echo(f"Added [runtime.actors.{name}] to {config_file.name}")
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(f"Could not edit {config_file.name} ({exc}). Add this snippet manually:", err=True)
        click.echo(f'\n[runtime.actors.{name}]\nagent = "{name}"\ndisplay_name = "{display_name}"', err=True)


@add.command(name="tool")
@click.argument("name")
def add_tool(name: str):
    """Create extensions/<name>.py with one @ep_tool stub."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise click.ClickException(f"Invalid tool name {name!r} (letters, digits, '_'; must start with a letter).")
    ws = _discover_project()
    func_name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    target = ws.bos_dir / "extensions" / f"{func_name}.py"
    if target.exists():
        raise click.ClickException(f"{target} already exists.")

    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_template("_shared/tool.py.tmpl", {"tool_name": name, "func_name": func_name})
    target.write_text(content, encoding="utf-8")
    click.echo(f"Created {target}")

    extensions = ws.config.platform.extensions
    if not any(entry in ("./extensions", "extensions") for entry in extensions):
        click.echo("Note: ./extensions is not in [platform].extensions — add this line for the file to load:", err=True)
        click.echo('  extensions = ["bos.exts", "./extensions"]', err=True)


@add.command(name="channel")
@click.argument("kind", type=click.Choice(["telegram"]))
def add_channel(kind: str):
    """Wire a channel into [runtime.channels] (currently: telegram)."""
    import tomlkit

    ws = _discover_project()
    config_file = ws.config_file
    channel_id = f"{kind}:main"
    if any(ch.channel_id == channel_id for ch in ws.config.runtime.channels):
        raise click.ClickException(f"Channel {channel_id!r} is already configured.")

    snippet = (
        '\n[[runtime.channels]]\ntype = "TelegramChannel"\nchannel_id = "telegram:main"\n'
        'display_name = "Telegram"\ntarget_actor = "main"\nsettings = { token_env = "TELEGRAM_BOT_TOKEN" }'
    )
    try:
        doc = tomlkit.parse(config_file.read_text(encoding="utf-8"))
        runtime = doc.setdefault("runtime", tomlkit.table())
        entry = tomlkit.table()
        entry["type"] = "TelegramChannel"
        entry["channel_id"] = channel_id
        entry["display_name"] = "Telegram"
        entry["target_actor"] = str(ws.config.runtime.default_actor)
        settings = tomlkit.inline_table()
        settings["token_env"] = "TELEGRAM_BOT_TOKEN"
        entry["settings"] = settings
        channels = runtime.get("channels")
        if channels is None:
            channels = tomlkit.aot()
            channels.append(entry)
            runtime["channels"] = channels
        else:
            channels.append(entry)
        config_file.write_text(tomlkit.dumps(doc), encoding="utf-8")
        click.echo(f"Added [[runtime.channels]] {channel_id} to {config_file.name}")
    except Exception as exc:
        click.echo(f"Could not edit {config_file.name} ({exc}). Add this snippet manually:", err=True)
        click.echo(snippet, err=True)
        return

    _ensure_env_placeholder(ws, "TELEGRAM_BOT_TOKEN")
    click.echo("Put the bot token from @BotFather into .env as TELEGRAM_BOT_TOKEN, then restart the gateway.")


def _ensure_env_placeholder(ws: Workspace, env_name: str) -> None:
    envfile = ws.config.platform.envfile or ".env"
    env_path = ws.bos_dir / envfile
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if any(line.split("=", 1)[0].strip() == env_name for line in existing.splitlines()):
        return
    content = existing + ("" if not existing or existing.endswith("\n") else "\n") + f"{env_name}=\n"
    env_path.write_text(content, encoding="utf-8")
    click.echo(f"Added {env_name}= placeholder to {env_path.name}")


# ── boscli project doctor ───────────────────────────────────────

_STATUS_MARK = {"ok": ("✓", "green"), "fail": ("✗", "red"), "warn": ("!", "yellow"), "skip": ("-", None)}


@project.command(name="doctor")
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
