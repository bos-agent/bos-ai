"""The vendor facts BOS's Claude Code confinement rests on (BEP 19 §3.5.3, §3.10.4).

Every test but the last drives the real `claude` CLI bundled in claude-agent-sdk through
`ClaudeSDKClient`, against the scripted model in tests/fake_anthropic.py: the model is
fake; the CLI, the SDK and every permission decision are the vendor's. No BOS code runs.
Measured against claude-agent-sdk 0.2.159 (bundled CLI 2.1.281).

When one fails, read what the CLI returned first — each assertion on what a tool did
carries the tool results — and rule out the host: fact 6b needs socat on PATH and a bwrap
that can actually start a sandbox. Only then does a failure mean the vendor moved under
the design; re-measure and revisit the design before touching the assertion.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, get_args

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultError,
    ResultMessage,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from conftest import claude_cli_env
from fake_anthropic import FakeAnthropic

# Several tests set can_use_tool under bypassPermissions on purpose: whether it is
# consulted is what they measure, so the SDK's advisory that it will not be is noise.
pytestmark = pytest.mark.filterwarnings("ignore::claude_agent_sdk.CanUseToolShadowedWarning")

_PRESET = {"type": "preset", "preset": "claude_code"}
_DENY = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "outside the workspace",
    }
}
_SANDBOX = {"enabled": True, "allowUnsandboxedCommands": False}
_UNTRUSTED = "this workspace has not been trusted"
_SANDBOX_DISABLED = "Sandbox disabled"


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """The agent's cwd, and a sibling directory outside it."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    return ws, outside


def _options(ws: Path, env: dict[str, str], **overrides: Any) -> ClaudeAgentOptions:
    """No settings sources unless a test needs one, and Claude Code's own prompt —
    ``system_prompt=None`` would send an empty one (fact 8)."""
    return ClaudeAgentOptions(**{"cwd": ws, "env": env, "setting_sources": [], "system_prompt": _PRESET, **overrides})


def _write(tool_use_id: str, path: Path) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": "Write", "input": {"file_path": str(path), "content": "x"}}


def _bash(tool_use_id: str, command: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command, "description": "t"}}


def _hooks(hook: Any) -> dict[str, Any]:
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]}


def _sandboxed(sandbox: dict[str, Any]) -> dict[str, Any]:
    """``sandbox``, plus inline settings no other CLI shares.

    The SDK sends ``sandbox`` inside inline ``--settings`` JSON, and the CLI writes that
    to ``/tmp/claude-<uid>/claude-settings-<sha256 of the content, 16 hex>.json``
    (``GPo``/``h9`` in CLI 2.1.281) — a path the bash sandbox binds. That file is gone
    once the CLI exits: observed after every run, though the removal code itself was not
    found in the CLI source. Two CLIs started with byte-identical settings therefore share
    one file, and when the first exits, the other's sandboxed commands fail for the rest
    of its turn with ``bwrap: Can't find source path …`` (without the nonce, fact 6b
    failed 5 and 7 times in two samples of 24 under 4-way parallel runs). A nonce makes
    each call's settings, and so its file, its own. Nothing about this is test-specific:
    BOS's own clients share the race unless their settings differ too.
    """
    return {"sandbox": sandbox, "settings": json.dumps({"env": {"BOS_TEST_NONCE": uuid.uuid4().hex}})}


async def _turn(options: ClaudeAgentOptions) -> None:
    """One turn: prompt, then drain to the result. Bounded, so a CLI waiting on
    something that never comes fails the test instead of hanging the suite."""
    async with asyncio.timeout(60):
        async with ClaudeSDKClient(options) as client:
            await client.query("go")
            messages = [message async for message in client.receive_response()]
    result = messages[-1]
    assert isinstance(result, ResultMessage) and not result.is_error, result


def _tool_results(fake: FakeAnthropic) -> dict[str, str]:
    """tool_use_id -> the tool_result the CLI sent back to the model, as text."""
    results: dict[str, str] = {}
    for body in fake.requests:
        for message in body.get("messages", []):
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    value = block.get("content")
                    results[block["tool_use_id"]] = value if isinstance(value, str) else json.dumps(value)
    return results


# Fact 2's control, measured per mode: an out-of-root Write the hook lets through, under a
# can_use_tool that allows it — (landed, can_use_tool asked, the refusal the model saw).
# The hook's deny is load-bearing only where the control lands. Under dontAsk the mode
# refuses the control itself, and under auto (against this fake) the classifier's failure
# does, so there the deny is not load-bearing: the model still receives its reason, but
# the write would have been refused anyway.
_UNHOOKED_OUT_OF_ROOT_WRITE: dict[str, tuple[bool, bool, str | None]] = {
    "default": (True, True, None),
    "acceptEdits": (True, True, None),
    "plan": (True, True, None),  # fact 5: plan is not a write guard
    "bypassPermissions": (True, False, None),
    "dontAsk": (False, False, "don't ask mode"),
    # auto asks a classifier before running the tool — a model call, which the fake
    # answers with "done", so the CLI reads no verdict and blocks. A real model's verdict
    # is not measured.
    "auto": (False, False, "Auto mode could not evaluate"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", get_args(PermissionMode))
async def test_fact_2_a_pretooluse_hook_sees_every_call_and_its_deny_holds_in_every_mode(
    tmp_path, fake_anthropic, mode
):
    """Fact 2, and the out-of-root Write column of fact 3's matrix. The modes are the
    SDK's own ``PermissionMode``, so a mode a later SDK adds fails here until it is
    measured and given a row above."""
    assert mode in _UNHOOKED_OUT_OF_ROOT_WRITE, f"new PermissionMode {mode!r}: measure it and add a row"
    ws, outside = _dirs(tmp_path)
    denied, control = outside / "denied.txt", outside / "control.txt"
    fake_anthropic.script([[_write("tu_denied", denied), _write("tu_control", control)]])
    hooked: list[str | None] = []
    asked: list[str] = []

    async def hook(input_data, tool_use_id, context):
        hooked.append(tool_use_id)
        return _DENY if input_data["tool_input"]["file_path"] == str(denied) else {}

    async def allow(tool_name, tool_input, context):
        asked.append(tool_input["file_path"])
        return PermissionResultAllow()

    env = claude_cli_env(tmp_path, fake_anthropic)
    await _turn(_options(ws, env, permission_mode=mode, can_use_tool=allow, hooks=_hooks(hook)))

    results = _tool_results(fake_anthropic)
    assert sorted(hooked) == ["tu_control", "tu_denied"], "the hook fires once per tool call"
    assert not denied.exists(), results
    assert "outside the workspace" in results["tu_denied"], "the model is told the hook's reason"
    landed, consulted, refusal = _UNHOOKED_OUT_OF_ROOT_WRITE[mode]
    assert control.exists() is landed, results
    assert asked == ([str(control)] if consulted else [])
    if refusal is not None:
        assert refusal in results["tu_control"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "asked_about"), [("acceptEdits", ["tu_script"]), ("default", ["tu_script", "tu_touch", "tu_write"])]
)
async def test_fact_3_can_use_tool_is_not_a_gate(tmp_path, fake_anthropic, mode, asked_about):
    """Fact 3's canary. Under acceptEdits the CLI applies an in-root Write and an in-root
    ``touch``, and serves an in-root Read, without asking can_use_tool — here one that
    refuses everything. ``default`` is the control: there the same callback is asked and
    its refusal holds, so the acceptEdits result is the mode skipping the callback, not
    the callback being unwired. An in-root Read is asked about in neither mode.

    acceptEdits skips the callback for filesystem commands like ``touch``, not for every
    Bash call: a script writing the same directory is still asked about, and refused.
    The script is a bare ``python3`` naming only an in-root path, so nothing in the command
    points outside cwd; refused, it never runs, so the host need not have ``python3``."""
    ws, _ = _dirs(tmp_path)
    notes, written, touched, scripted = (ws / name for name in ("notes.txt", "written.txt", "touched", "scripted"))
    notes.write_text("in-root content")
    script = f"python3 -c \"open({str(scripted)!r}, 'w').close()\""
    read = {"type": "tool_use", "id": "tu_read", "name": "Read", "input": {"file_path": str(notes)}}
    fake_anthropic.script([
        [_write("tu_write", written), _bash("tu_touch", f"touch {touched}"), _bash("tu_script", script), read]
    ])
    asked: list[str] = []

    async def refuse(tool_name, tool_input, context):
        asked.append(context.tool_use_id)
        return PermissionResultDeny(message="refused by can_use_tool")

    await _turn(_options(ws, claude_cli_env(tmp_path, fake_anthropic), permission_mode=mode, can_use_tool=refuse))

    results = _tool_results(fake_anthropic)
    assert sorted(asked) == asked_about, results
    applied = mode == "acceptEdits"
    assert written.exists() is applied, results
    assert touched.exists() is applied, results
    assert not scripted.exists(), results
    assert "in-root content" in results["tu_read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
async def test_fact_4_a_trusted_repos_allow_rules_switch_can_use_tool_off(tmp_path, fake_anthropic, mode):
    """Fact 4, under default and acceptEdits, which ask can_use_tool about an out-of-root
    Write when nothing else decides it (fact 2's table). The repo's own
    .claude/settings.json allows Write; in a trusted workspace that rule approves the
    Write before can_use_tool — here one that would refuse — is asked, and only the
    hook's deny still holds.

    Which file carries the trust is part of the fact. With CLAUDE_CONFIG_DIR set, as
    claude_cli_env sets it, the CLI reads ``<CLAUDE_CONFIG_DIR>/.claude.json``; the same
    marker in ``<HOME>/.claude.json`` is ignored. The second turn shows that through the
    CLI's own untrusted-workspace warning, which names the file it read. The first turn
    asserts that warning is absent, so a marker the CLI did not pick up fails the test
    rather than passing it vacuously.
    """
    ws, outside = _dirs(tmp_path)
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Write", "Bash"]}}))
    marker = json.dumps({"projects": {str(ws): {"hasTrustDialogAccepted": True}}})
    asked: list[str] = []
    stderr: list[str] = []

    async def hook(input_data, tool_use_id, context):
        return _DENY if input_data["tool_input"]["file_path"].endswith("denied.txt") else {}

    async def refuse(tool_name, tool_input, context):
        asked.append(tool_input["file_path"])
        return PermissionResultDeny(message="refused by can_use_tool")

    def options(env: dict[str, str]) -> ClaudeAgentOptions:
        return _options(
            ws,
            env,
            setting_sources=["project"],
            permission_mode=mode,
            can_use_tool=refuse,
            hooks=_hooks(hook),
            stderr=stderr.append,
        )

    env = claude_cli_env(tmp_path / "trusted", fake_anthropic)
    Path(env["CLAUDE_CONFIG_DIR"], ".claude.json").write_text(marker)
    allowed, denied = outside / "allowed.txt", outside / "denied.txt"
    fake_anthropic.script([[_write("tu_allowed", allowed), _write("tu_denied", denied)]])
    await _turn(options(env))
    results = _tool_results(fake_anthropic)
    assert not [line for line in stderr if _UNTRUSTED in line], f"the CLI did not pick up the trust marker: {stderr}"
    assert allowed.exists(), f"the repo's allow rule approved the out-of-root Write: {results}"
    assert asked == [], "without asking can_use_tool, which would have refused"
    assert not denied.exists(), f"the hook's deny still holds: {results}"

    asked.clear()
    stderr.clear()
    env = claude_cli_env(tmp_path / "home-marker", fake_anthropic)
    Path(env["HOME"], ".claude.json").write_text(marker)
    ignored = outside / "ignored.txt"
    fake_anthropic.script([[_write("tu_ignored", ignored)]])
    await _turn(options(env))
    read = str(Path(env["CLAUDE_CONFIG_DIR"], ".claude.json"))
    assert [line for line in stderr if _UNTRUSTED in line and read in line], stderr
    assert asked == [str(ignored)], "untrusted: the allow rule is ignored and can_use_tool is asked"
    assert not ignored.exists(), _tool_results(fake_anthropic)


def _path_without(name: str, farm_root: Path) -> str:
    """This process's PATH with *name* made unresolvable and nothing else changed.

    Each PATH entry holding *name* is replaced, in place, by a directory of symlinks to
    everything in it but *name*; every other entry is kept verbatim, so lookup order is
    unchanged. Dropping a directory outright would not do: on merged-/usr systems /bin
    is /usr/bin, and it holds the rest of the userland too.
    """
    entries = []
    for i, entry in enumerate(os.environ["PATH"].split(os.pathsep)):
        if entry and (Path(entry) / name).exists():
            farm = farm_root / str(i)
            farm.mkdir(parents=True)
            for item in Path(entry).iterdir():
                if item.name != name:
                    (farm / item.name).symlink_to(item)
            entry = str(farm)
        entries.append(entry)
    path = os.pathsep.join(entries)
    assert shutil.which(name, path=path) is None
    return path


_needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bwrap, the Linux bash sandbox")


@pytest.mark.asyncio
@_needs_bwrap
async def test_fact_6_the_bash_sandbox_fails_open_when_a_dependency_is_missing(tmp_path, fake_anthropic):
    """Fact 6, the tripwire. With socat hidden from the child's PATH, the CLI says
    "Sandbox disabled" on stderr and runs an out-of-root touch unsandboxed — failing open
    is its default (fact 6c is the setting that changes that). A can_use_tool that allows
    everything leaves the sandbox as the only thing that could have stopped the touch."""
    ws, outside = _dirs(tmp_path)
    env = {**claude_cli_env(tmp_path, fake_anthropic), "PATH": _path_without("socat", tmp_path / "path")}
    target = outside / "f.txt"
    fake_anthropic.script([[_bash("tu_touch", f"touch {target}")]])
    stderr: list[str] = []

    async def allow(tool_name, tool_input, context):
        return PermissionResultAllow()

    await _turn(
        _options(
            ws, env, permission_mode="acceptEdits", can_use_tool=allow, stderr=stderr.append, **_sandboxed(_SANDBOX)
        )
    )
    assert [line for line in stderr if _SANDBOX_DISABLED in line and "socat" in line], stderr
    assert target.exists(), f"the out-of-root touch ran unsandboxed: {_tool_results(fake_anthropic)}"


@pytest.mark.asyncio
@_needs_bwrap
async def test_fact_6c_fail_if_unavailable_makes_the_cli_refuse_instead(tmp_path, fake_anthropic):
    """Fact 6c, the setting that turns fact 6's fail-open into a refusal. The CLI has a
    ``sandbox.failIfUnavailable`` setting ("Exit with an error at startup if
    sandbox.enabled is true but the sandbox cannot start"), and the SDK copies the
    ``sandbox`` dict into ``--settings`` verbatim, so it reaches the CLI although the
    SDK's ``SandboxSettings`` TypedDict does not declare it. Fact 6's setup plus that key:
    the turn ends in a ``ResultError`` before any model call, and the touch never runs."""
    ws, outside = _dirs(tmp_path)
    env = {**claude_cli_env(tmp_path, fake_anthropic), "PATH": _path_without("socat", tmp_path / "path")}
    target = outside / "f.txt"
    fake_anthropic.script([[_bash("tu_touch", f"touch {target}")]])
    sandbox = {**_SANDBOX, "failIfUnavailable": True}

    async def allow(tool_name, tool_input, context):
        return PermissionResultAllow()

    with pytest.raises(ResultError, match="Sandbox required but unavailable"):
        await _turn(_options(ws, env, permission_mode="acceptEdits", can_use_tool=allow, **_sandboxed(sandbox)))
    assert fake_anthropic.requests == [], "refused at startup, before the first model call"
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("bwrap") is None or shutil.which("socat") is None, reason="needs bwrap and socat on PATH"
)
@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_fact_6b_with_its_dependencies_the_sandbox_confines_bash_writes_not_reads(tmp_path, fake_anthropic, mode):
    """Fact 6b, the positive half of fact 6. With bwrap and socat on PATH: an in-root
    touch lands (the sandbox runs commands at all); an out-of-root touch fails with
    "Read-only file system", under acceptEdits and bypassPermissions alike; a touch in
    the host's /tmp does not reach it; a cat outside cwd returns the file — reads are
    not confined. can_use_tool, which here refuses everything, is never asked about any
    of the four, so the sandbox, not the permission layer, is what blocks. (Under
    acceptEdits that is the SDK's documented autoAllowBashIfSandboxed default at work;
    bypassPermissions approves everything anyway.)"""
    ws, outside = _dirs(tmp_path)
    secret = outside / "secret.txt"
    secret.write_text("outside content")
    inside, escaped = ws / "in.txt", outside / "out.txt"
    host_tmp = Path("/tmp") / f"bos-fact-6b-{uuid.uuid4().hex}"
    fake_anthropic.script([
        [
            _bash("tu_in", f"touch {inside}"),
            _bash("tu_out", f"touch {escaped}"),
            _bash("tu_tmp", f"touch {host_tmp}"),
            _bash("tu_read", f"cat {secret}"),
        ]
    ])
    asked: list[str] = []
    stderr: list[str] = []

    async def refuse(tool_name, tool_input, context):
        asked.append(tool_input.get("command", tool_name))
        return PermissionResultDeny(message="refused by can_use_tool")

    env = claude_cli_env(tmp_path, fake_anthropic)
    try:
        await _turn(
            _options(ws, env, permission_mode=mode, can_use_tool=refuse, stderr=stderr.append, **_sandboxed(_SANDBOX))
        )
        results = _tool_results(fake_anthropic)
        seen = f"tool results: {results}; stderr: {stderr}"
        assert not [line for line in stderr if _SANDBOX_DISABLED in line], seen
        assert asked == [], seen
        assert inside.exists(), seen
        assert not escaped.exists(), seen
        assert "Read-only file system" in results["tu_out"], seen
        assert not host_tmp.exists(), seen
        assert "outside content" in results["tu_read"], seen
    finally:
        host_tmp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_fact_7_the_cli_expands_env_vars_in_mcp_headers(tmp_path, fake_anthropic):
    """Fact 7. The SDK puts ``mcp_servers`` on the CLI's command line, which other local
    users can read from /proc/<pid>/cmdline, so a bearer must not appear there. The CLI
    expands ``${VAR}`` in an HTTP server's headers from its own environment: the
    placeholder rides the command line, the value only ``options.env``, and the server
    receives ``Bearer <value>``."""
    seen: list[str | None] = []

    class Recorder(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            if self.path == "/mcp":
                seen.append(self.headers.get("authorization"))
            # Refusing is enough: the header arrives with the first request.
            self.send_response(401)
            self.end_headers()

        do_GET = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        ws, _ = _dirs(tmp_path)
        env = {**claude_cli_env(tmp_path, fake_anthropic), "BOS_MCP_BEARER_FACT7": "s3cret-token"}
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        headers = {"Authorization": "Bearer ${BOS_MCP_BEARER_FACT7}"}
        options = _options(ws, env, mcp_servers={"bos-tools": {"type": "http", "url": url, "headers": headers}})
        # cli_path only so _build_command runs without connect(); the turn below uses the bundled CLI.
        command = SubprocessCLITransport(prompt="x", options=replace(options, cli_path="claude"))._build_command()
        assert "${BOS_MCP_BEARER_FACT7}" in command[command.index("--mcp-config") + 1]
        assert "s3cret-token" not in " ".join(command)
        await _turn(options)
    finally:
        server.shutdown()
        server.server_close()
    assert seen, "the CLI never reached the MCP server"
    assert set(seen) == {"Bearer s3cret-token"}


@pytest.mark.parametrize(
    ("system_prompt", "flags"),
    [
        # The trap (BEP 19 §3.4.1.3): None is an empty prompt, not Claude Code's own.
        (None, {"--system-prompt": "", "--append-system-prompt": None}),
        # base_instructions: replaces the prompt.
        ("Replace it.", {"--system-prompt": "Replace it.", "--append-system-prompt": None}),
        # Neither key set: a preset without "append" emits neither flag.
        (_PRESET, {"--system-prompt": None, "--append-system-prompt": None}),
        # system_prompt: appended to Claude Code's own.
        ({**_PRESET, "append": "Add this."}, {"--system-prompt": None, "--append-system-prompt": "Add this."}),
    ],
)
def test_fact_8_the_system_prompt_flags_the_sdk_builds(system_prompt, flags):
    """Fact 8. Pins the SDK's private ``SubprocessCLITransport._build_command``, as
    ``test_codex_approval_handler_attribute_exists`` pins a private Codex attribute: BEP
    19 §7.13 is asserted on the built command, so if the SDK moves or renames this, the
    test must fail loudly and be re-pointed, not deleted. Offline — nothing is spawned."""
    options = ClaudeAgentOptions(cli_path="claude", system_prompt=system_prompt)
    command = SubprocessCLITransport(prompt="x", options=options)._build_command()
    for flag, value in flags.items():
        if value is None:
            assert flag not in command
        else:
            assert command[command.index(flag) + 1] == value
