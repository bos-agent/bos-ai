"""``boscli init`` and ``boscli gen`` CLI tests (BEP 9)."""

import tomllib

from click.testing import CliRunner

from bos.cli.commands.scaffolding import _infer_key_env, _parse_specialists
from bos.cli.entry import cli


def _invoke(args, **kwargs):
    return CliRunner().invoke(cli, args, **kwargs)


def _init_project(workspace, *extra_args):
    result = _invoke(["init", str(workspace), "--yes", "--no-git", *extra_args])
    assert result.exit_code == 0, result.output
    return result


# ── init ────────────────────────────────────────────────────────


def test_init_yes_scaffolds_runnable_baseline(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    result = _init_project(tmp_path)

    assert "Initialized assistant project" in result.output
    assert "Config validates ✓" in result.output
    assert (tmp_path / ".bos" / "config.toml").is_file()
    assert not (tmp_path / "bos.toml").exists()
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / ".bos" / ".env").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / ".bos" / "extensions" / "project_tools.py").is_file()
    assert (tmp_path / ".bos" / "skills" / "example-skill" / "SKILL.md").is_file()
    assert not (tmp_path / ".git").exists()


def test_init_rejects_initialized_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    (tmp_path / "bos.toml").write_text("", encoding="utf-8")

    result = _invoke(["init", str(tmp_path), "--yes", "--no-git"])

    assert result.exit_code != 0
    assert "already initialized" in result.output


def test_init_minimal_copies_reference_template(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    result = _invoke(["init", str(tmp_path), "--minimal"])

    assert result.exit_code == 0
    assert f"Initialized BOS workspace at {tmp_path / '.bos'}" in result.output
    assert (tmp_path / ".bos" / "config.toml").is_file()
    assert not (tmp_path / ".bos" / "extensions").exists()


def test_init_flat_layout(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path, "--flat")

    assert (tmp_path / "bos.toml").is_file()
    assert not (tmp_path / ".bos").exists()


def test_init_flat_rejected_for_package_archetype(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    result = _invoke(["init", str(tmp_path), "--yes", "--no-git", "--archetype", "package", "--flat"])

    assert result.exit_code != 0
    assert "--flat is not supported with the package archetype" in result.output


def test_init_wizard_team_with_skipped_model(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    # purpose, archetype 2 (team), provider 5 (skip), git: no
    result = _invoke(
        ["init", str(tmp_path)],
        input="A bot that reviews pull requests\n2\n5\nn\n",
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".bos" / "agents" / "researcher.md").is_file()
    assert (tmp_path / ".bos" / "agents" / "writer.md").is_file()
    config = tomllib.loads((tmp_path / ".bos" / "config.toml").read_text(encoding="utf-8"))
    binding = config["agents"]["main"]["plugin-bindings"]["SubagentPlugin"]
    assert binding["enabled"] == ["researcher", "writer"]


def test_init_package_archetype(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    target = tmp_path / "my-weather-tools"
    target.mkdir()
    result = _invoke(["init", str(target), "--yes", "--no-git", "--archetype", "package"])

    assert result.exit_code == 0, result.output
    # dotbos is implied: the root belongs to the Python package
    assert (target / ".bos" / "config.toml").is_file()
    assert not (target / "bos.toml").exists()
    assert (target / "src" / "my_weather_tools" / "tools.py").is_file()
    pyproject = tomllib.loads((target / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["name"] == "my-weather-tools"
    assert pyproject["project"]["entry-points"]["bos.exts"] == {"my_weather_tools": "my_weather_tools.tools"}
    assert "uv run boscli gateway start" in result.output


def test_init_package_name_override(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    result = _invoke(
        ["init", str(tmp_path), "--yes", "--no-git", "--archetype", "package", "--name", "CoolTools"]
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "src" / "cooltools" / "tools.py").is_file()


def test_normalize_pkg_name():
    from bos.cli.commands.scaffolding import _normalize_pkg_name

    assert _normalize_pkg_name("my-agent") == "my_agent"
    assert _normalize_pkg_name("My Weather.Tools") == "my_weather_tools"
    import click
    import pytest

    with pytest.raises(click.ClickException):
        _normalize_pkg_name("123")


def test_init_with_model_writes_env_placeholder(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _init_project(tmp_path, "--model", "anthropic/claude-sonnet-4-6", "--no-probe")

    config = tomllib.loads((tmp_path / ".bos" / "config.toml").read_text(encoding="utf-8"))
    assert config["agent"]["defaults"]["model"] == "anthropic/claude-sonnet-4-6"
    assert "ANTHROPIC_API_KEY=" in (tmp_path / ".bos" / ".env").read_text(encoding="utf-8")


# ── gen ─────────────────────────────────────────────────────────


def test_gen_agent_creates_markdown_spec(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["gen", "agent", "analyst"])

    assert result.exit_code == 0, result.output
    content = (tmp_path / ".bos" / "agents" / "analyst.md").read_text(encoding="utf-8")
    assert content.startswith("---\ndescription:")
    assert "You are analyst" in content

    duplicate = _invoke(["gen", "agent", "analyst"])
    assert duplicate.exit_code != 0
    assert "already exists" in duplicate.output


def test_gen_agent_actor_appends_runtime_entry(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["gen", "agent", "analyst", "--actor"])

    assert result.exit_code == 0, result.output
    config_file = tmp_path / ".bos" / "config.toml"
    config = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert config["runtime"]["actors"]["analyst"]["agent"] == "analyst"
    # the original entries and comments survive the tomlkit round-trip
    assert config["runtime"]["actors"]["main"]["agent"] == "main"
    assert "# purpose:" in config_file.read_text(encoding="utf-8")


def test_gen_tool_creates_stub(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["gen", "tool", "FetchQuote"])

    assert result.exit_code == 0, result.output
    content = (tmp_path / ".bos" / "extensions" / "fetch_quote.py").read_text(encoding="utf-8")
    assert 'name="FetchQuote"' in content
    assert "async def fetch_quote" in content


def test_gen_channel_telegram_appends_config_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["gen", "channel", "telegram"])

    assert result.exit_code == 0, result.output
    config = tomllib.loads((tmp_path / ".bos" / "config.toml").read_text(encoding="utf-8"))
    channels = config["runtime"]["channels"]
    assert channels[0]["type"] == "TelegramChannel"
    assert channels[0]["settings"]["token_env"] == "TELEGRAM_BOT_TOKEN"
    assert "TELEGRAM_BOT_TOKEN=" in (tmp_path / ".bos" / ".env").read_text(encoding="utf-8")

    duplicate = _invoke(["gen", "channel", "telegram"])
    assert duplicate.exit_code != 0
    assert "already configured" in duplicate.output


def test_gen_outside_project_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["gen", "agent", "analyst"])

    assert result.exit_code != 0
    assert "No BOS project found" in result.output


# ── helpers and parsing ─────────────────────────────────────────


def test_infer_key_env_known_prefixes():
    assert _infer_key_env("anthropic/claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
    assert _infer_key_env("gpt-5.2") == "OPENAI_API_KEY"
    assert _infer_key_env("gemini/gemini-2.5-pro") == "GEMINI_API_KEY"
    assert _infer_key_env("something/unknown") == "OPENAI_API_KEY"


def _spec(name: str) -> str:
    return f'{{"name": "{name}", "description": "d", "system_prompt": "p"}}'


def test_parse_specialists_accepts_valid_json_with_noise():
    text = f"Here you go:\n```json\n[{_spec('market-analyst')},\n {_spec('report-writer')}]\n```"
    specs = _parse_specialists(text)
    assert [s["name"] for s in specs] == ["market-analyst", "report-writer"]


def test_parse_specialists_rejects_bad_output():
    assert _parse_specialists("no json here") is None
    assert _parse_specialists(f"[{_spec('only-one')}]") is None
    assert _parse_specialists(f"[{_spec('Main!')}, {_spec('x')}]") is None
    assert _parse_specialists(f"[{_spec('x')}, {_spec('x')}]") is None


def test_config_source_banner_formats(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import bos.cli.commands.agent as agent_module

    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    ws = SimpleNamespace(config_file=tmp_path / "bos.toml", bos_dir=tmp_path, workspace=tmp_path)

    captured = []
    monkeypatch.setattr("click.echo", lambda msg, err=False: captured.append((msg, err)))

    agent_module._echo_config_source(ws)
    assert captured[-1] == (f"Using project: {tmp_path} (bos.toml)", True)

    agent_module._echo_config_source(ws, config_arg="/some/custom.toml")
    assert captured[-1][0].startswith("Using config file:")


def test_detect_provider_keys(monkeypatch):
    from bos.cli.commands import scaffolding as s

    for _, env in s._PROVIDER_KEY_ENV:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    assert s._detect_provider_keys() == {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }


def test_qualify_model_id():
    from bos.cli.commands import scaffolding as s

    assert s._qualify("anthropic", "claude-x") == "anthropic/claude-x"
    assert s._qualify("anthropic", "anthropic/claude-x") == "anthropic/claude-x"


def test_fetch_models_live(monkeypatch):
    import litellm

    from bos.cli.commands import scaffolding as s

    monkeypatch.setattr(litellm, "get_valid_models", lambda **k: ["claude-a", "claude-b"])
    models, source = s._fetch_models("anthropic", "key")
    assert source == "live"
    assert models == ["anthropic/claude-a", "anthropic/claude-b"]


def test_fetch_models_catalog_fallback(monkeypatch):
    import litellm

    from bos.cli.commands import scaffolding as s

    monkeypatch.setattr(litellm, "get_valid_models", lambda **k: [])
    monkeypatch.setattr(litellm, "models_by_provider", {"anthropic": {"claude-z", "claude-a"}})
    models, source = s._fetch_models("anthropic", "key")
    assert source == "catalog"
    assert models == ["anthropic/claude-a", "anthropic/claude-z"]  # sorted, qualified


def test_fetch_models_curated_fallback(monkeypatch):
    import litellm

    from bos.cli.commands import scaffolding as s

    monkeypatch.setattr(litellm, "get_valid_models", lambda **k: [])
    monkeypatch.setattr(litellm, "models_by_provider", {})
    models, source = s._fetch_models("anthropic", None)
    assert source == "curated"
    assert models == list(s._RECOMMENDED_MODELS["anthropic"])


def test_provider_step_interactive_existing_key(monkeypatch):
    from bos.cli import prompts
    from bos.cli.commands import scaffolding as s

    for _, env in s._PROVIDER_KEY_ENV:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "live-key")
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)
    monkeypatch.setattr(prompts, "select", lambda *a, **k: "anthropic")
    monkeypatch.setattr(s, "_fetch_models", lambda p, key: (["anthropic/claude-sonnet-4-6"], "live"))
    monkeypatch.setattr(prompts, "autocomplete", lambda *a, **k: "anthropic/claude-sonnet-4-6")

    model, env_pairs = s._provider_step(None, None, False)
    assert model == "anthropic/claude-sonnet-4-6"
    assert env_pairs == {}  # key already in env, never copied into .env


def test_provider_step_interactive_prompts_for_missing_key(monkeypatch):
    from bos.cli import prompts
    from bos.cli.commands import scaffolding as s

    for _, env in s._PROVIDER_KEY_ENV:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)
    monkeypatch.setattr(prompts, "select", lambda *a, **k: "openai")
    monkeypatch.setattr(prompts, "password", lambda *a, **k: "typed-key")
    monkeypatch.setattr(s, "_fetch_models", lambda p, key: (["openai/gpt-4.1"], "live"))
    monkeypatch.setattr(prompts, "autocomplete", lambda *a, **k: "openai/gpt-4.1")

    model, env_pairs = s._provider_step(None, None, False)
    assert model == "openai/gpt-4.1"
    assert env_pairs == {"OPENAI_API_KEY": "typed-key"}


def test_provider_step_interactive_skip(monkeypatch):
    from bos.cli import prompts
    from bos.cli.commands import scaffolding as s

    monkeypatch.setattr(prompts, "is_interactive", lambda: True)
    monkeypatch.setattr(prompts, "select", lambda *a, **k: "__skip__")
    assert s._provider_step(None, None, False) == (None, {})
