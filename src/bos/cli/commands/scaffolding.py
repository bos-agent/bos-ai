"""Project scaffolding command — ``boscli init`` (BEP 9)."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import cast

import click

from bos.cli import prompts
from bos.cli.scaffold import ARCHETYPES, scaffold_workspace, toml_multiline_text
from bos.config import ConfigNotFoundError, Workspace, WorkspaceResolutionError, initialize_workspace

_DEFAULT_PURPOSE = "A general-purpose personal agent."

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

# API providers offered in the interactive wizard, in display order, with the
# litellm key env var each one reads. Mirrors _API_KEY_ENV_BY_PREFIX as a flat
# provider list. BOS-maintained; occasional drift against litellm is accepted.
_PROVIDER_KEY_ENV: tuple[tuple[str, str], ...] = (
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("mistral", "MISTRAL_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("xai", "XAI_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
)

_DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"

# Curated ids pinned to the top of the picker and used as the last-resort source
# when both the live API and the litellm catalog come back empty.
_RECOMMENDED_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4-5"),
}


# ── boscli init ─────────────────────────────────────────────────


@click.command(name="init")
@click.argument("directory", default=".")
@click.option("--archetype", type=click.Choice(ARCHETYPES), default=None, help="Starting topology.")
@click.option("--model", default=None, help="litellm model id (implies the API-key provider path).")
@click.option("--purpose", default=None, help="What the agent project is for.")
@click.option("--yes", is_flag=True, default=False, help="Accept defaults for unanswered questions; never prompt.")
@click.option("--minimal", is_flag=True, default=False, help="Only copy the reference config template, no wizard.")
@click.option("--git/--no-git", "init_git", default=None, help="Run git init and create a .gitignore.")
@click.option("--no-probe", is_flag=True, default=False, help="Skip the live model credential check.")
@click.option("--name", "pkg_name_opt", default=None, help="Package name (package archetype; default: dir name).")
def init(directory, archetype, model, purpose, yes, minimal, init_git, no_probe, pkg_name_opt):
    """Initialize a BOS project with a guided, runnable baseline."""
    workspace_path = Path(directory).expanduser().resolve()

    if minimal:
        try:
            bos_dir = initialize_workspace(workspace_path)
        except WorkspaceResolutionError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Initialized BOS workspace at {bos_dir}")
        if init_git:
            _git_init(workspace_path)
        return

    project_name = workspace_path.name or "bos-project"

    if purpose is None:
        purpose = _DEFAULT_PURPOSE if yes else prompts.text("What is this agent project for?", default=_DEFAULT_PURPOSE)
    if archetype is None:
        archetype = "workspace" if yes else _prompt_archetype()

    pkg_name = None
    if archetype == "package":
        pkg_name = _normalize_pkg_name(pkg_name_opt or project_name)

    model, env_pairs = _provider_step(model, yes)

    context = _build_context(project_name, purpose, archetype, model)
    if pkg_name:
        context["pkg_name"] = pkg_name
        context["dist_name"] = pkg_name.replace("_", "-")

    try:
        result = scaffold_workspace(
            workspace_path,
            archetype,
            context,
            env_content=_env_content(env_pairs),
        )
    except WorkspaceResolutionError as exc:
        raise click.ClickException(str(exc))

    written = ", ".join(sorted({_top_level_name(p, workspace_path) for p in result.written}))
    click.echo(f"Initialized {archetype} project at {workspace_path}")
    click.echo(f"Wrote {written}")

    if model and not no_probe:
        probe_ok, detail = _probe_model(workspace_path, model)
        if probe_ok:
            click.echo(f"Config validates ✓ · credential probe ✓ ({detail})")
        else:
            click.echo(f"Config validates ✓ · credential probe ✗ — {detail}", err=True)
            click.echo("  The project was still created; fix credentials and run `boscli doctor --probe`.", err=True)
    else:
        click.echo("Config validates ✓ · credential probe skipped")

    if init_git is None:
        default_git = not _inside_git_repo(workspace_path)
        init_git = default_git if yes else prompts.confirm("Initialize a git repository?", default=default_git)
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
    descriptions = {
        "workspace": "single agent with memory and skills (add a team or Telegram later)",
        "package": "an installable Python extension package (tools, channels, providers)",
    }
    choices = [prompts.Choice(name, name, descriptions[name]) for name in ARCHETYPES]
    return cast(str, prompts.select("Choose a starting topology:", choices, default=ARCHETYPES[0]))


def _provider_step(model: str | None, yes: bool) -> tuple[str | None, dict[str, str]]:
    """Resolve the model id and any env vars to capture into .env."""
    if model:
        return model, _api_key_env_pairs(model, yes)
    if yes:
        return None, {}
    if not prompts.is_interactive():
        return _provider_step_fallback()
    return _provider_step_interactive()


def _provider_step_fallback() -> tuple[str | None, dict[str, str]]:
    """The historical numbered provider menu, used for non-TTY callers (CI, pipes)."""
    click.echo("Choose a model provider:")
    click.echo("  1. API key (any litellm model id, e.g. anthropic/claude-…, gpt-…)")
    click.echo("  2. Skip — configure the model later")
    choice = click.prompt("Provider", type=click.IntRange(1, 2), default=1)
    if choice == 2:
        return None, {}
    model = click.prompt("Model id (litellm format)", default=_DEFAULT_MODEL)
    return model, _api_key_env_pairs(model, yes=False)


def _provider_step_interactive() -> tuple[str | None, dict[str, str]]:
    detected = _detect_provider_keys()
    choices = _provider_choices(detected)
    default = next(iter(detected)) if len(detected) == 1 else None
    # Selectable rows all carry str values; separators (value=None) are never returned.
    selection = cast(str, prompts.select("Choose a model provider:", choices, default=default))
    if selection == "__skip__":
        return None, {}
    return _api_provider(selection)


def _provider_choices(detected: dict[str, str]) -> list[prompts.Choice]:
    rows: list[prompts.Choice] = []
    for provider, env in _PROVIDER_KEY_ENV:
        if provider in detected:
            rows.append(prompts.Choice(provider, provider, f"✓ {env}"))
    for provider, env in _PROVIDER_KEY_ENV:
        if provider not in detected:
            rows.append(prompts.Choice(provider, provider, f"set {env}"))
    rows.append(prompts.Choice(None, "", selectable=False))
    rows.append(prompts.Choice("__skip__", "Skip — configure the model later"))
    return rows


def _api_provider(provider: str) -> tuple[str, dict[str, str]]:
    env_var = dict(_PROVIDER_KEY_ENV)[provider]
    existing = os.environ.get(env_var)
    if existing:
        api_key, env_pairs = existing, {}  # already in env; never copy into .env
    else:
        api_key = prompts.password(f"{env_var} (stored in .env, leave empty to skip)")
        env_pairs = {env_var: api_key}
    models, source = _fetch_models(provider, api_key)
    return _pick_model(provider, models, source), env_pairs


def _pick_model(provider: str, models: list[str], source: str) -> str:
    recommended = list(_RECOMMENDED_MODELS.get(provider, ()))
    ordered = recommended + [m for m in models if m not in recommended]
    if not ordered:
        ordered = [_DEFAULT_MODEL]
    notes = {
        "live": f"models live from your {provider} account",
        "catalog": "models from the litellm catalog (may be stale)",
        "curated": "built-in recommended models (could not reach the provider)",
    }
    click.echo(f"  {notes[source]}")
    click.echo(f"  {len(ordered)} model(s) available · ↑↓ to browse, type to filter, Enter for {ordered[0]}")
    # No pre-filled default: an empty buffer lets the completion menu show the
    # full list up front. autocomplete() still returns ordered[0] on empty Enter.
    return prompts.autocomplete("Model id (type to filter, or enter a custom id)", ordered)


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
) -> dict[str, str]:
    if model:
        model_line = f'model = "{model}"'
    else:
        model_line = '# model = ""  # set a litellm model id, or export BOS_MODEL'
    first_prompt = "use the WordCount tool to count the words in this sentence"
    return {
        "project_name": project_name,
        "purpose": purpose.strip(),
        "purpose_comment": " ".join(purpose.split()),
        "purpose_toml": toml_multiline_text(purpose),
        "archetype": archetype,
        "model_line": model_line,
        "config_path": ".bos/config.toml",
        "first_prompt": first_prompt,
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


# ── model probe ─────────────────────────────────────────────────


def _bootstrapped_workspace(workspace_path: Path) -> Workspace:
    ws = Workspace.from_discovery(workspace_path)
    ws.resolve_agents()
    ws.bootstrap_platform()
    return ws


_LLM_LOOP: asyncio.AbstractEventLoop | None = None


def _run_llm(coro):
    """Run an LLM coroutine on a single, reused event loop for the process.

    litellm enqueues its success/error callbacks onto a background
    ``LoggingWorker`` bound to whatever event loop is running. ``asyncio.run``
    creates and tears down a fresh loop per call, so the *next* call detects the
    loop change and resets the worker's queue (``logging_worker.py`` line 75,
    ``self._queue = None``), discarding still-queued callback coroutines — that
    is the source of the ``coroutine 'Logging.async_success_handler' was never
    awaited`` RuntimeWarning. Reusing one loop lets the worker drain those
    callbacks naturally between calls and via litellm's atexit flush.
    """
    global _LLM_LOOP
    if _LLM_LOOP is None or _LLM_LOOP.is_closed():
        _LLM_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LLM_LOOP)
    return _LLM_LOOP.run_until_complete(coro)


def _complete(model: str, prompt: str) -> str:
    from bos.core import LLMClient

    async def _call() -> str:
        response = await LLMClient().complete([{"role": "user", "content": prompt}], model=model)
        return response.text or ""

    return _run_llm(_call())


def _detect_provider_keys() -> dict[str, str]:
    """Providers whose key env var is set (non-empty) in the environment."""
    return {provider: env for provider, env in _PROVIDER_KEY_ENV if os.environ.get(env)}


def _qualify(provider: str, model: str) -> str:
    """Ensure a model id is in litellm ``provider/model`` form."""
    return model if "/" in model else f"{provider}/{model}"


def _fetch_models(provider: str, api_key: str | None) -> tuple[list[str], str]:
    """Return (models, source) where source is 'live', 'catalog', or 'curated'.

    Tries the provider's live /models endpoint first (needs a key), then the
    static litellm catalog, then the curated shortlist. Never raises.
    """
    import logging

    import litellm

    if api_key:
        logging.getLogger("LiteLLM").setLevel(logging.ERROR)  # silence the 401/empty warning
        try:
            live = litellm.get_valid_models(check_provider_endpoint=True, custom_llm_provider=provider, api_key=api_key)
        except Exception:
            live = []
        live = [_qualify(provider, m) for m in live]
        if live:
            return live, "live"

    catalog = sorted(_qualify(provider, m) for m in litellm.models_by_provider.get(provider, set()))
    if catalog:
        return catalog, "catalog"

    return list(_RECOMMENDED_MODELS.get(provider, ())), "curated"


def _probe_model(workspace_path: Path, model: str) -> tuple[bool, str]:
    try:
        _bootstrapped_workspace(workspace_path)
        _complete(model, "Reply with the single word: pong")
        return True, "1 model call"
    except Exception as exc:
        return False, str(exc) or type(exc).__name__


def _discover_project() -> Workspace:
    """Resolve the BOS project rooted at the cwd (shared by `doctor` and `memory`)."""
    from bos.cli.commands.agent import _echo_config_source

    try:
        ws = Workspace.from_discovery(".")
    except ConfigNotFoundError:
        raise click.ClickException("No BOS project found here. Run `boscli init` first.")
    if ws.config_file is None:
        raise click.ClickException("Project config file could not be resolved.")
    _echo_config_source(ws)
    return ws
