"""BEP 19 Layer 4b: the Claude Code runtime.

Construction so far (BEP 19 §3.4, §3.5, §3.5.3, §3.10.3): the config, the permission
mapping, the per-client settings nonce, the ``native_options`` allowlist, and the
fail-closed preflights. No turn runs yet.

Most tests here build options and never start the CLI. Where the CLI's own behaviour is
the point — the settings file its bash sandbox binds — the test drives the real bundled
CLI against the fake Messages API (tests/fake_anthropic.py), as the vendor-fact tests do.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from conftest import BlockImport, claude_cli_env
from fake_anthropic import FakeAnthropic
from test_claude_code_vendor_facts import _bash, _tool_results
from test_external_agent_seam import _write_workspace

from bos.extensions.runtimes import claude_code
from bos.extensions.runtimes.claude_code import ClaudeCodeAgent

_WORKSPACE_WRITE_SANDBOX = {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True}
_FIELDS = sorted(field.name for field in dataclasses.fields(ClaudeAgentOptions))


@pytest.fixture(autouse=True)
def _no_api_credentials(monkeypatch):
    """The subscription preflight reads this process's environment, and a developer's
    shell may export either variable. Tests that want one set it themselves."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sandbox_available(monkeypatch):
    """For tests about what BOS *sends* under ``workspace-write``, not whether this host
    can run it: the construction-time check answers "available". No test that starts a
    sandboxed CLI uses this — those skip without bwrap and socat."""
    monkeypatch.setattr(claude_code, "_bash_sandbox_unavailable", lambda platform: None)


def _no_mcp_server() -> Any:
    raise AssertionError("construction asked for the MCP server")


def _agent(tmp_path: Path, **cfg: Any) -> ClaudeCodeAgent:
    """A ClaudeCodeAgent over *tmp_path*, ``permission="read-only"`` unless given. Its
    ``mcp`` accessor raises: construction must never ask for the MCP server (BEP 19 §3.1)."""
    from bos.core.defaults.structured_validator import JsonSchemaValidator

    cfg.setdefault("permission", "read-only")
    return ClaudeCodeAgent(
        kind="george",
        cfg=cfg,
        chat_store=None,
        workspace=tmp_path,
        mcp=_no_mcp_server,
        structured_validator=JsonSchemaValidator(),
    )


def _command(options: ClaudeAgentOptions) -> list[str]:
    """The argv the SDK starts the CLI with, for *options*. ``cli_path`` only so
    ``_build_command`` runs without ``connect()``; the vendor-fact tests pin this private
    method (fact 8), so a moved or renamed one fails there too."""
    return SubprocessCLITransport(prompt="x", options=replace(options, cli_path="claude"))._build_command()


def _flag(command: list[str], flag: str) -> str | None:
    """The value after *flag* in *command*, or None when the flag is absent."""
    return command[command.index(flag) + 1] if flag in command else None


def _for_the_fake(options: ClaudeAgentOptions, tmp_path: Path, fake: FakeAnthropic) -> ClaudeAgentOptions:
    """BOS's *options* with the child pointed at *fake*, in an isolated HOME and config
    directory (conftest's ``claude_cli_env``). ``env`` is BOS's, so this adds to it."""
    return replace(options, env={**options.env, **claude_cli_env(tmp_path, fake)})


# ── Construction and config (BEP 19 §3.4) ────────────────────────────────────


def test_construction_parses_and_resolves_the_config(tmp_path):
    (tmp_path / "services").mkdir()
    agent = _agent(tmp_path, cwd="services", model="claude-opus-4-5", mcp_tools=["NoSuchTool"])

    config = agent.resolved_config
    assert agent.name == "george"
    assert config["external_runtime"] == "claude-code"
    assert config["cwd"] == str((tmp_path / "services").resolve())
    assert config["permission"] == "read-only"
    assert config["permission_mode"] == "default"
    assert config["setting_sources"] == ["project"]
    assert config["max_turns"] is None
    assert config["model"] == "claude-opus-4-5"
    assert config["mcp_tools"] == ["NoSuchTool"]
    assert config["mcp_tools_unavailable"] == ["NoSuchTool"]
    assert config["native_options"] == {}


def test_construction_starts_nothing(tmp_path, monkeypatch, sandbox_available):
    """Construction builds no client, so starts no CLI (BEP 19 §3.1), and asks for no
    MCP server (``_agent``'s accessor raises), at every permission level. The sandbox check
    is stubbed here; the real one is ``shutil.which`` and ``os.access``."""

    def no_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("construction built a client")

    monkeypatch.setattr(claude_code, "_CLIENT_FACTORY", no_client)
    for permission in ("read-only", "workspace-write", "full-access"):
        _agent(tmp_path, permission=permission)


def test_it_satisfies_the_external_runtime_protocol(tmp_path):
    from bos.core.agent import ExternalRuntime

    assert isinstance(_agent(tmp_path), ExternalRuntime)


@pytest.mark.asyncio
async def test_create_agent_builds_it_through_the_harness(tmp_path):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("claude-code", agent_cfg={"permission": "read-only"})
        assert isinstance(agent, ClaudeCodeAgent)
        assert agent.resolved_config["cwd"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_the_beps_example_config_builds_and_inspects(tmp_path, monkeypatch):
    """BEP 19 §3.4's own ``[agents.claude-code]`` example, through the config loader, the
    harness and ``boscli inspect`` (§7.9) — the §8.2 carry-forward: ``setting_sources`` was
    an unknown key there. The agent is built and never run."""
    from bos.cli.commands.inspect import _agent_capabilities
    from bos.core import AgentRegistry

    # bootstrap_platform() registers `claude-code` in this class-level registry, which
    # other tests assert holds no reserved kind nobody configured.
    monkeypatch.setattr(AgentRegistry, "_registry", dict(AgentRegistry._registry))
    ws = _write_workspace(
        tmp_path,
        '[agents.claude-code]\ncwd = "."\npermission = "read-only"\nmodel = "claude-opus-4-5"\n'
        'setting_sources = ["project"]\nmcp_tools = []\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()

    info = await _agent_capabilities(ws, "claude-code")
    assert info["runtime"] == "claude-code"
    assert info["cwd"] == str(tmp_path.resolve())
    assert info["permission"] == "read-only"


@pytest.mark.asyncio
async def test_a_missing_extra_is_named_by_the_real_runtime_module(tmp_path, monkeypatch):
    """BEP 19 §3.10.4, through claude_code.py itself rather than a probe module. Blocks
    both packages the extra adds, ``claude_agent_sdk`` and ``mcp``, and drops them and
    this runtime module from ``sys.modules``, so ``create_agent`` imports claude_code.py
    afresh. Whichever of the two it imports first is the one that fails, so this also pins
    that ``claude_agent_sdk`` comes first: an ``mcp`` import ahead of it would fail as
    ``mcp`` and be reported as a broken runtime module, not as a missing extra."""
    from bos.core.harness import AgentHarness

    blocked = ("claude_agent_sdk", "mcp")
    monkeypatch.setattr(sys, "meta_path", [*(BlockImport(name) for name in blocked), *sys.meta_path])
    for name in list(sys.modules):
        if name == "bos.extensions.runtimes.claude_code" or name.split(".")[0] in blocked:
            monkeypatch.delitem(sys.modules, name)

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("claude-code", agent_cfg={"permission": "read-only"})
    assert "bos-ai[claude-code]" in str(excinfo.value)


@pytest.mark.parametrize(
    ("prompt_cfg", "system_prompt", "append"),
    [
        ({"system_prompt": "You are the implementer."}, None, "You are the implementer."),
        ({}, None, None),
        ({"base_instructions": "Replace it."}, "Replace it.", None),
    ],
    ids=["system_prompt", "neither", "base_instructions"],
)
def test_the_prompt_flags_on_the_built_command(tmp_path, prompt_cfg, system_prompt, append):
    """BEP 19 §7.13, asserted on the command BOS's options produce rather than on the
    resolved config: §3.4.1.3's trap — ``None`` sends an *empty* prompt — is invisible at
    the config layer. Fact 8 pins the SDK's half; this pins BOS's."""
    command = _command(_agent(tmp_path, **prompt_cfg)._options())

    assert _flag(command, "--system-prompt") == system_prompt
    assert _flag(command, "--append-system-prompt") == append


@pytest.mark.parametrize(
    ("setting_sources", "flag"),
    [
        (None, "--setting-sources=project"),
        (["user", "project"], "--setting-sources=user,project"),
        ([], "--setting-sources="),
    ],
    ids=["default", "user-and-project", "none"],
)
def test_setting_sources_is_always_sent(tmp_path, setting_sources, flag):
    """Fact 9: left at None, the SDK sends no ``--setting-sources`` and the CLI loads every
    source, the operator's own ~/.claude/settings.json included. BOS always sends it,
    ``["project"]`` by default (BEP 19 §3.5.3), and an explicit ``[]`` goes out as an empty
    list rather than being dropped."""
    cfg = {} if setting_sources is None else {"setting_sources": setting_sources}
    agent = _agent(tmp_path, **cfg)

    assert flag in _command(agent._options())
    assert agent.resolved_config["setting_sources"] == (["project"] if setting_sources is None else setting_sources)


@pytest.mark.parametrize("setting_sources", ["project", ["global"], ["project", 1], {"project": True}])
def test_setting_sources_must_be_a_list_drawn_from_user_project_local(tmp_path, setting_sources):
    with pytest.raises(ValueError) as excinfo:
        _agent(tmp_path, setting_sources=setting_sources)

    message = str(excinfo.value)
    assert "setting_sources" in message
    assert all(source in message for source in ("user", "project", "local"))


def test_setting_sources_is_known_to_claude_code_alone(tmp_path):
    """A Codex config naming it still fails as an unknown key instead of being silently
    ignored (BEP 19 §3.4: each runtime raises on a key it does not honour)."""
    from bos.extensions.runtimes._shared import parse_external_config

    cfg = {"permission": "read-only", "setting_sources": ["project"]}
    with pytest.raises(ValueError, match="setting_sources"):
        parse_external_config(cfg, runtime="codex", workspace=tmp_path)
    assert parse_external_config(cfg, runtime="claude-code", workspace=tmp_path).permission == "read-only"


def test_max_iterations_becomes_max_turns(tmp_path, caplog):
    """BEP 19 §3.9: Claude Code's counterpart of ``max_iterations`` is ``max_turns``. The
    shared parser drops the key for both runtimes, so the runtime takes it out first, and
    nothing here reports it as ignored."""
    with caplog.at_level(logging.DEBUG):
        agent = _agent(tmp_path, max_iterations=7)

    assert _flag(_command(agent._options()), "--max-turns") == "7"
    assert agent.resolved_config["max_turns"] == 7
    assert not [record for record in caplog.records if "max_iterations" in record.getMessage()]


@pytest.mark.parametrize("max_iterations", [0, -1, True, "7", 2.5])
def test_max_iterations_must_be_a_positive_integer(tmp_path, max_iterations):
    """0 most of all: the SDK sends ``--max-turns`` only for a truthy value, so 0 would
    arrive as no limit at all."""
    with pytest.raises(ValueError, match="max_iterations"):
        _agent(tmp_path, max_iterations=max_iterations)


# ── The permission mapping (BEP 19 §3.5) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("permission", "mode", "sandbox"),
    [
        ("read-only", "default", None),
        ("workspace-write", "acceptEdits", _WORKSPACE_WRITE_SANDBOX),
        ("full-access", "bypassPermissions", None),
    ],
)
def test_permission_maps_to_a_mode_and_a_sandbox(tmp_path, sandbox_available, permission, mode, sandbox):
    """BEP 19 §3.5's Claude Code table, on the built command. Across the three levels the
    modes are exactly these, so none of ``plan`` (not a write guard, fact 5), ``dontAsk``
    (refuses an in-root Write; measured, not pinned) or ``auto`` (a classifier model decides
    each call) is ever
    sent. ``failIfUnavailable`` is not in the SDK's ``SandboxSettings`` type but reaches the
    CLI inside ``--settings`` all the same (fact 6c)."""
    agent = _agent(tmp_path, permission=permission)
    command = _command(agent._options())

    assert _flag(command, "--permission-mode") == mode
    assert json.loads(_flag(command, "--settings") or "{}").get("sandbox") == sandbox
    assert agent.resolved_config["permission_mode"] == mode


# ── Each client's settings are its own (BEP 19 §3.5.3) ───────────────────────


def test_each_client_gets_settings_of_its_own(tmp_path, sandbox_available):
    """The CLI's bash sandbox binds a file named by a hash of the settings, so
    byte-identical settings would share one file between concurrent CLIs (the race
    ``_SETTINGS_NONCE_VAR`` records). ``_options()`` runs once per client, and each call's
    settings carry a fresh nonce beside the sandbox."""
    agent = _agent(tmp_path, permission="workspace-write")
    first, second = (_flag(_command(agent._options()), "--settings") for _ in range(2))

    assert first is not None and second is not None and first != second
    for settings in (first, second):
        parsed = json.loads(settings)
        assert parsed["sandbox"] == _WORKSPACE_WRITE_SANDBOX
        (nonce,) = parsed["env"].values()
        assert len(nonce) == 32


def _settings_mount_point(options: ClaudeAgentOptions) -> Path:
    """The file the CLI's bash sandbox binds for *options*' inline settings:
    ``<temp dir>/claude-<uid>/claude-settings-<16 hex>.json``, the hex being the start of
    the sha256 of the settings re-serialised as compact JSON. Read from the CLI 2.1.281
    source (``h9``; its temp dir is Node's ``os.tmpdir()`` unless ``CLAUDE_CODE_TMPDIR`` is
    set, which the ``fake_anthropic`` fixture scrubs), and matched against the file the CLI
    creates: no other serialisation tried named it."""
    settings = json.loads(_flag(_command(options), "--settings") or "")
    digest = hashlib.sha256(json.dumps(settings, separators=(",", ":")).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir(), f"claude-{os.getuid()}", f"claude-settings-{digest}.json")


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("bwrap") is None or shutil.which("socat") is None, reason="needs bwrap and socat on PATH"
)
async def test_each_clients_cli_binds_a_settings_file_of_its_own(tmp_path, fake_anthropic):
    """What the nonce buys, against the real CLI with BOS's own options. While a sandboxed
    command runs, the CLI holds a file at the path its settings name
    (``_settings_mount_point``), and the file is gone once the turn is over — so CLIs sent
    byte-identical settings share one file, the race ``_SETTINGS_NONCE_VAR`` records. Two
    clients of one agent name two files, and the file this client's CLI holds is the one
    its own settings name.

    The race itself is not reproduced here: it needs another CLI to remove the file in the
    moment between this CLI finding it present and its bwrap starting, which no ordering of
    turns arranges. Two turns ordered so that one ends while the other still has a sandboxed
    command to run pass with byte-identical settings too — measured both ways round —
    because a command that finds the file missing re-creates it."""
    (tmp_path / "ws").mkdir()
    agent = _agent(tmp_path, permission="workspace-write", cwd="ws")
    options, other = agent._options(), agent._options()
    mount_point = _settings_mount_point(options)
    assert mount_point != _settings_mount_point(other)

    started, go = tmp_path / "ws" / "started", tmp_path / "ws" / "go"
    fake_anthropic.script([[_bash("tu_wait", f"touch {started}; while [ ! -e {go} ]; do sleep 0.05; done")]])
    options = _for_the_fake(options, tmp_path / "env", fake_anthropic)  # `env` is not in the settings

    async def turn() -> list[Any]:
        async with ClaudeSDKClient(options) as client:
            await client.query("go")
            return [message async for message in client.receive_response()]

    async with asyncio.timeout(60):
        running = asyncio.create_task(turn())
        while not started.exists() and not running.done():
            await asyncio.sleep(0.02)
        held = mount_point.exists()
        go.touch()
        messages = await running

    seen = f"tool results: {_tool_results(fake_anthropic)}"
    assert isinstance(messages[-1], ResultMessage) and not messages[-1].is_error, seen
    assert started.exists(), f"BOS's options ran a sandboxed command: {seen}"
    assert held, f"no file at {mount_point} while the sandboxed command ran: {seen}"
    assert not mount_point.exists(), "removed once the CLI's sandboxed commands were done"


# ── native_options: an allowlist over the vendor's own fields (BEP 19 §3.4) ──


def test_native_options_classification_partitions_the_vendor_fields():
    """The source of the enumeration: every field of the SDK's own ``ClaudeAgentOptions``
    sits in exactly one of claude_code.py's three sets. A claude-agent-sdk release that
    adds, renames or drops a field fails here until someone classifies it."""
    owned, refused, allowed = set(claude_code._BOS_OWNED), set(claude_code._REFUSED), set(claude_code._ALLOWED)
    classified = owned | refused | allowed

    assert set(_FIELDS) - classified == set(), "a ClaudeAgentOptions field nobody has classified"
    assert classified - set(_FIELDS) == set(), "a classified name ClaudeAgentOptions no longer has"
    assert not (owned & refused or owned & allowed or refused & allowed), "a field in two sets"


def test_allowed_native_options_reach_the_cli(tmp_path):
    agent = _agent(tmp_path, native_options={"fallback_model": "claude-sonnet-4-5", "max_budget_usd": 2.5})
    command = _command(agent._options())

    assert _flag(command, "--fallback-model") == "claude-sonnet-4-5"
    assert _flag(command, "--max-budget-usd") == "2.5"
    assert agent.resolved_config["native_options"] == {"fallback_model": "claude-sonnet-4-5", "max_budget_usd": 2.5}


@pytest.mark.parametrize("key", [name for name in _FIELDS if name not in claude_code._ALLOWED] + ["fallback_modle"])
def test_native_options_refuses_every_other_key_naming_it_and_why(tmp_path, key):
    """Review Focus 4. Everything not deliberately allowed is refused at construction —
    every BOS-owned field, every refused one, and a name that is no field at all — and the
    message says why, from the classification, and what *is* allowed."""
    with pytest.raises(ValueError) as excinfo:
        _agent(tmp_path, native_options={key: None})

    message = str(excinfo.value)
    assert repr(key) in message and "native_options" in message
    assert (claude_code._BOS_OWNED.get(key) or claude_code._REFUSED.get(key) or "not a field of") in message
    assert all(allowed in message for allowed in claude_code._ALLOWED)


# ── The fail-closed preflights (BEP 19 §3.5.3, §3.10.3) ──────────────────────


def test_workspace_write_is_refused_where_the_bash_sandbox_is_unavailable(tmp_path, monkeypatch):
    """Refused at construction with the check's reason. The levels that run no sandbox do
    not consult it."""
    monkeypatch.setattr(claude_code, "_bash_sandbox_unavailable", lambda platform: "socat not found on PATH")

    with pytest.raises(ValueError) as excinfo:
        _agent(tmp_path, permission="workspace-write")
    message = str(excinfo.value)
    assert "socat not found on PATH" in message and "workspace-write" in message
    _agent(tmp_path, permission="read-only")
    _agent(tmp_path, permission="full-access")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables on PATH")
def test_on_linux_the_check_names_what_is_missing_and_what_to_install(tmp_path, monkeypatch):
    """The real lookup, not a patched one: a PATH holding neither tool, then one, then both."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))

    def install(tool: str) -> None:
        (bin_dir / tool).write_text("#!/bin/sh\n")
        (bin_dir / tool).chmod(0o755)

    both_missing = claude_code._bash_sandbox_unavailable("linux")
    assert both_missing is not None and "bwrap" in both_missing and "socat" in both_missing
    assert "bubblewrap" in both_missing, "names the package that provides bwrap"
    install("bwrap")
    socat_missing = claude_code._bash_sandbox_unavailable("linux")
    assert socat_missing is not None and "socat" in socat_missing and "bwrap" not in socat_missing
    install("socat")
    assert claude_code._bash_sandbox_unavailable("linux") is None


def test_the_check_on_other_platforms():
    windows = claude_code._bash_sandbox_unavailable("win32")
    assert windows is not None and "Windows" in windows
    other = claude_code._bash_sandbox_unavailable("sunos5")
    assert other is not None and "sunos5" in other
    if not Path("/usr/bin/sandbox-exec").exists():  # anywhere but a real macOS host
        macos = claude_code._bash_sandbox_unavailable("darwin")
        assert macos is not None and "/usr/bin/sandbox-exec" in macos


@pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_subscription_auth_refuses_an_api_credential_in_the_environment(tmp_path, monkeypatch, variable):
    """BEP 19 §3.10.3, Review Focus 5: the CLI inherits BOS's environment whole, and either
    variable silently turns a subscription run into a billed API run. ``auth = "api_key"``
    is the explicit opt-in. The value itself never reaches the message."""
    monkeypatch.setenv(variable, "sk-ant-not-a-real-key")

    with pytest.raises(ValueError) as excinfo:
        _agent(tmp_path)
    message = str(excinfo.value)
    assert variable in message and 'auth = "api_key"' in message
    assert "sk-ant-not-a-real-key" not in message
    _agent(tmp_path, auth="api_key")


def test_the_refusal_names_every_credential_that_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "b")

    with pytest.raises(ValueError) as excinfo:
        _agent(tmp_path)
    assert "ANTHROPIC_API_KEY" in str(excinfo.value) and "ANTHROPIC_AUTH_TOKEN" in str(excinfo.value)


def test_an_empty_credential_variable_is_not_a_credential(tmp_path, monkeypatch):
    """The CLI reads both by truthiness or through a parser that maps an empty value to
    unset (read from the CLI 2.1.281 source), so an empty one bills nothing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")

    _agent(tmp_path)
