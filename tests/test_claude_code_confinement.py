"""BEP 19 Layer 4b, Task 8: the confinement — a deny-by-default ``tools=`` allowlist per
permission level, a ``PreToolUse`` hook that path-checks the file tools and denies anything outside
the level's allowlist, a ``can_use_tool`` backstop, and a stderr sandbox tripwire (BEP 19 §3.5.3).

Everything behavioural is tested against the REAL bundled CLI driven by the fake Messages API
(tests/fake_anthropic.py), the technique the vendor-fact tests use (BEP 19 §3.5.5): the file landed
or it did not. Turns run through ``ClaudeCodeAgent.run()`` so the confinement is exercised as BOS
wires it, with the child pointed at the fake and an isolated HOME/config dir (conftest's
``claude_cli_env``), ``auth="api_key"`` so the subscription preflight does not refuse the fake key.
Workspace-write tests need ``bwrap`` and ``socat`` and skip without them; where they are absent,
``test_workspace_write_is_refused_without_the_sandbox`` asserts construction refuses the level.

When one fails, read the tool results first — each assertion carries them — and rule out the host
(a broken bwrap, a missing socat) before concluding the vendor moved under the design.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
)
from conftest import claude_cli_env
from fake_anthropic import FakeAnthropic
from test_claude_code_runtime import _turn as _fake_turn
from test_claude_code_vendor_facts import _bash, _tool_results, _write

from bos.core.defaults.structured_validator import JsonSchemaValidator
from bos.extensions.runtimes import claude_code
from bos.extensions.runtimes.claude_code import _TOOL_LEVELS, ClaudeCodeAgent, _SandboxDisabledError

pytestmark = pytest.mark.filterwarnings("ignore::claude_agent_sdk.CanUseToolShadowedWarning")

_needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or shutil.which("socat") is None, reason="needs bwrap and socat on PATH"
)
_LEVELS = ["read-only", pytest.param("workspace-write", marks=_needs_sandbox), "full-access"]


def _no_mcp() -> Any:
    raise AssertionError("construction asked for the MCP server")


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeAnthropic, **cfg: Any) -> ClaudeCodeAgent:
    """A ``ClaudeCodeAgent`` whose CLI child is pointed at *fake*. ``run()`` builds its own options,
    so the child's environment goes in this process's — the SDK hands the CLI the whole of it — and
    the hook reads ``CLAUDE_CONFIG_DIR``/``HOME`` from there too."""
    for name, value in claude_cli_env(tmp_path, fake).items():
        monkeypatch.setenv(name, value)
    cfg.setdefault("permission", "read-only")
    cfg.setdefault("auth", "api_key")
    cfg.setdefault("cwd", "ws")
    (tmp_path / "ws").mkdir(exist_ok=True)
    return ClaudeCodeAgent(
        kind="george",
        cfg=cfg,
        chat_store=None,
        workspace=tmp_path,
        mcp=_no_mcp,
        structured_validator=JsonSchemaValidator(),
    )


async def _run(agent: ClaudeCodeAgent, *, prompt: str = "go", turn_id: str = "t1") -> Any:
    async with asyncio.timeout(90):
        return await agent.run("chat-1", prompt, turn_id=turn_id)


def _tu(tool_id: str, name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _slug(cwd: Path) -> str:
    """The CLI's project slug for *cwd* (matches ``_inherited_route`` in test_claude_code_runtime)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


# ── The offered tool list is the classification's source (R9) ────────────────


async def _offered_full_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, permission: str) -> set[str]:
    """The FULL set the CLI would offer under BOS's options at *permission*, with BOS's ``tools=``
    allowlist removed so nothing is filtered — i.e. what there is to classify, not what BOS offers.
    The turn makes no tool call, so the hook never fires."""
    fake = FakeAnthropic()
    try:
        agent = _agent(tmp_path, monkeypatch, fake, permission=permission)
        options = replace(agent._options(), tools=None)
        fake.script([[{"type": "text", "text": "ok"}]])
        async with asyncio.timeout(90):
            async with ClaudeSDKClient(options) as client:
                await client.query("go")
                assert isinstance([m async for m in client.receive_response()][-1], ResultMessage)
        return {t["name"] for t in fake.requests[0]["tools"]}
    finally:
        fake.close()


@pytest.mark.asyncio
async def test_the_offered_tools_are_all_classified(tmp_path, monkeypatch):
    """R9, the classification's source: every tool the CLI offers under BOS's options is classified in
    ``_TOOL_LEVELS``, and nothing in ``_TOOL_LEVELS`` is a name the CLI never offers. A CLI release
    that adds, renames or drops a tool fails here until someone classifies it — the same guard as
    ``native_options``' allowlist, but over the CLI's own offered list rather than a dataclass's
    fields.

    The offered set is level-dependent: the CLI surfaces the interactive trio (AskUserQuestion,
    EnterPlanMode, ExitPlanMode) only when a permission handler is present, which BOS installs at
    read-only and workspace-write but not at full-access. So read-only alone sees the whole set; the
    union across the levels tried must equal ``_TOOL_LEVELS``, and each level's set must be a subset
    of it."""
    seen: set[str] = set()
    levels = ["read-only", "full-access"]
    if shutil.which("bwrap") is not None and shutil.which("socat") is not None:
        levels.append("workspace-write")
    for level in levels:
        offered = await _offered_full_set(tmp_path, monkeypatch, level)
        assert offered - set(_TOOL_LEVELS) == set(), (
            f"the CLI offers a tool nobody classified at {level!r}: {sorted(offered - set(_TOOL_LEVELS))}"
        )
        seen |= offered
    assert set(_TOOL_LEVELS) - seen == set(), (
        f"_TOOL_LEVELS classifies a tool the CLI never offered: {sorted(set(_TOOL_LEVELS) - seen)}"
    )


def test_offered_tools_are_the_levels_allowlist(tmp_path, fake_anthropic, monkeypatch):
    """``_offered_tools`` is deny-by-default over ``_TOOL_LEVELS``: Read everywhere; the file
    writers and Bash at workspace-write and full-access; the cross-session pair nowhere; everything
    else at full-access only."""

    def offered(level: str) -> set[str]:
        return set(_agent(tmp_path, monkeypatch, fake_anthropic, permission=level)._offered_tools())

    assert offered("read-only") == {"Read"}
    assert offered("workspace-write") == {"Read", "Write", "Edit", "NotebookEdit", "Bash"}
    full = offered("full-access")
    never = {"ListAgents", "SendMessage", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode"}
    assert never.isdisjoint(full), "cross-session and interactive tools excluded even at full-access (R10)"
    assert full == set(_TOOL_LEVELS) - never
    assert {"Read", "Write", "Bash", "Agent", "WebFetch"} <= full, "full-access offers everything else"


# ── Per-level file-write confinement (the core behavioural claim) ─────────────


@pytest.mark.asyncio
async def test_read_only_writes_nothing_and_reads_only_inside_the_root(tmp_path, fake_anthropic, monkeypatch):
    """Under ``read-only``: an in-root Write does not land (Write is not offered, and the hook denies
    it as outside the allowlist), an out-of-root Write does not land, an in-root Read is served, and
    an out-of-root Read is denied (the hook path-checks Read, BEP §3.5.5)."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (ws / "notes.txt").write_text("in-root secret\n")
    (outside / "secret.txt").write_text("out-of-root secret\n")
    in_write, out_write = ws / "written.txt", outside / "written.txt"
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")
    fake_anthropic.script([
        [
            _write("tu_in", in_write),
            _write("tu_out", out_write),
            _tu("tu_read_in", "Read", file_path=str(ws / "notes.txt")),
            _tu("tu_read_out", "Read", file_path=str(outside / "secret.txt")),
        ]
    ])
    await _run(agent)
    results = _tool_results(fake_anthropic)

    assert not in_write.exists(), f"read-only wrote in-root: {results}"
    assert not out_write.exists(), f"read-only wrote out-of-root: {results}"
    assert "in-root secret" in results["tu_read_in"], f"in-root Read should be served: {results}"
    assert "out-of-root secret" not in results.get("tu_read_out", ""), f"out-of-root Read should be denied: {results}"
    # Write is not in read-only's `tools=` allowlist, so the CLI itself rejects it ("disabled for
    # this session") before the hook is even asked — the deny-by-default exclusion, the first layer.
    assert "disabled for this session" in results["tu_in"], f"Write must be excluded at read-only: {results}"
    # The out-of-root Read is denied by the hook (Read IS offered), with its model-facing reason.
    assert "outside the workspace root" in results["tu_read_out"], results


@pytest.mark.asyncio
@_needs_sandbox
async def test_workspace_write_confines_file_writes_to_the_root(tmp_path, fake_anthropic, monkeypatch):
    """Under ``workspace-write``: an in-root Write lands and an out-of-root Write does not (the hook
    denies it — this is exactly where ``can_use_tool`` is not the gate, fact 4)."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    outside.mkdir()
    in_write, out_write = ws / "in.txt", outside / "out.txt"
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    fake_anthropic.script([[_write("tu_in", in_write), _write("tu_out", out_write)]])
    await _run(agent)
    results = _tool_results(fake_anthropic)

    assert in_write.exists(), f"in-root Write should land: {results}"
    assert not out_write.exists(), f"out-of-root Write should be denied: {results}"
    assert "outside the workspace root" in results["tu_out"], results


@pytest.mark.asyncio
@_needs_sandbox
@pytest.mark.parametrize("spelling", ["dotdot", "symlink", "absolute", "relative", "tilde"])
async def test_each_escape_spelling_is_denied(tmp_path, fake_anthropic, monkeypatch, spelling):
    """Review Focus 3: a path that escapes by spelling is denied at ``workspace-write`` — ``..``, a
    symlink inside ``cwd`` pointing out, an absolute out-of-root path, a relative path that resolves
    against ``cwd`` to outside it, and ``~`` (which the CLI expands to an absolute ``$HOME`` path
    before the hook, so it lands on the same out-of-root deny). The hook resolves before comparing."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    marker = outside / "escaped.txt"
    if spelling == "dotdot":
        file_path = str(ws / ".." / "outside" / "escaped.txt")
    elif spelling == "symlink":
        (ws / "link").symlink_to(outside)  # a symlink inside cwd pointing out
        file_path = str(ws / "link" / "escaped.txt")
    elif spelling == "absolute":
        file_path = str(marker)
    elif spelling == "relative":  # resolved against the CLI's cwd (= ws), climbs out
        file_path = "../outside/escaped.txt"
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    if spelling == "tilde":  # the CLI expands ~ to $HOME (the test's isolated HOME), outside cwd
        marker = Path(os.environ["HOME"]) / "escaped.txt"
        file_path = "~/escaped.txt"
    write = {"type": "tool_use", "id": "tu", "name": "Write", "input": {"file_path": file_path, "content": "x"}}
    fake_anthropic.script([[write]])
    await _run(agent)

    assert not marker.exists(), f"the {spelling} escape wrote out-of-root: {_tool_results(fake_anthropic)}"


@pytest.mark.asyncio
async def test_full_access_writes_out_of_root(tmp_path, fake_anthropic, monkeypatch):
    """``full-access`` confines nothing on the filesystem — the test says so: an out-of-root Write
    lands. (It still excludes the cross-session tools; that is R10, tested separately.)"""
    outside = tmp_path / "outside"
    outside.mkdir()
    out_write = outside / "out.txt"
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="full-access")
    fake_anthropic.script([[_write("tu_out", out_write)]])
    await _run(agent)

    assert out_write.exists(), f"full-access should write out-of-root: {_tool_results(fake_anthropic)}"


# ── Bash confinement (the OS sandbox, or construction refuses the level) ──────


@pytest.mark.asyncio
@_needs_sandbox
async def test_workspace_write_confines_bash_writes_to_the_root(tmp_path, fake_anthropic, monkeypatch):
    """Under ``workspace-write`` an in-root Bash write lands and an out-of-root Bash write does not —
    the OS sandbox, not the hook, is what blocks bash (fact 6b). The hook sees the Bash call (R12)
    but has no path to check, so it passes; the sandbox refuses the escape."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    outside.mkdir()
    inside, escaped = ws / "in.txt", outside / "out.txt"
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    fake_anthropic.script([[_bash("tu_in", f"touch {inside}"), _bash("tu_out", f"touch {escaped}")]])
    await _run(agent)
    results = _tool_results(fake_anthropic)

    assert inside.exists(), f"in-root Bash write should land: {results}"
    assert not escaped.exists(), f"out-of-root Bash write should be refused by the sandbox: {results}"
    assert "Read-only file system" in results["tu_out"], results


@pytest.mark.skipif(
    shutil.which("bwrap") is not None and shutil.which("socat") is not None,
    reason="this host has the sandbox; the refusal path needs it absent",
)
def test_workspace_write_is_refused_without_the_sandbox(tmp_path, fake_anthropic, monkeypatch):
    """Where the host lacks ``bwrap``/``socat``, ``workspace-write`` is refused at construction
    rather than run bash unsandboxed (BEP §3.5.3)."""
    with pytest.raises(ValueError, match="workspace-write"):
        _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")


# ── The hook holds where can_use_tool does not: a trusted repo's allow rule ───


@pytest.mark.asyncio
@_needs_sandbox
async def test_a_trusted_repos_allow_rule_does_not_let_a_write_escape(tmp_path, fake_anthropic, monkeypatch):
    """Fact 4, with BOS's real hook: in a workspace the CLI trusts, whose own
    ``.claude/settings.json`` allows ``Write``, the allow rule approves an out-of-root Write before
    ``can_use_tool`` is asked — and the hook's deny still holds. This needs ``setting_sources`` to
    include ``project`` (the default loads no repo settings); trust is recorded in
    ``<CLAUDE_CONFIG_DIR>/.claude.json``."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Write", "Bash"]}}))
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write", setting_sources=["project"])
    # Mark the workspace trusted in the config dir the CLI reads (claude_cli_env set it in os.environ).
    Path(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json").write_text(
        json.dumps({"projects": {str(ws.resolve()): {"hasTrustDialogAccepted": True}}})
    )
    escaped = outside / "escaped.txt"
    fake_anthropic.script([[_write("tu", escaped)]])
    await _run(agent)

    assert not escaped.exists(), (
        f"the hook must deny the out-of-root Write the repo's allow rule approved: {_tool_results(fake_anthropic)}"
    )


# ── The CLI's config directory (R19, §8.2(g)) ────────────────────────────────


@pytest.mark.asyncio
async def test_a_read_only_write_into_the_config_memory_dir_does_not_land(tmp_path, fake_anthropic, monkeypatch):
    """R19/§8.2(g): the escape review measured was a ``read-only`` agent's Write into
    ``<CLAUDE_CONFIG_DIR>/projects/<slug>/memory/`` landing unasked (the CLI carves that directory
    out of the permission check). It no longer lands: ``read-only`` offers no Write at all (``tools=``
    excludes it, the deny-by-default first layer), so the CLI rejects the call before the hook. The
    config dir here is ``claude_cli_env``'s, outside ``cwd``."""
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")
    memory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / _slug(ws) / "memory"
    target = memory / "note.md"
    fake_anthropic.script([[_write("tu", target)]])
    await _run(agent)

    assert not target.exists(), f"the read-only memory-dir Write must not land: {_tool_results(fake_anthropic)}"
    assert "disabled for this session" in _tool_results(fake_anthropic)["tu"]


@pytest.mark.asyncio
@_needs_sandbox
async def test_the_hook_denies_the_config_dir_even_inside_cwd(tmp_path, fake_anthropic, monkeypatch):
    """The hook's own config-directory denial (R19), exercised where it is the load-bearing layer:
    ``workspace-write`` (Write is offered) with the CLI's config directory *inside* ``cwd`` — here
    ``cwd`` is the workspace root, so ``claude_cli_env``'s ``<workspace>/claude-config`` sits under it
    and a Write into its memory dir passes the out-of-root check but the hook denies it as the config
    directory. This is the case that matters when a host points ``cwd`` at ``$HOME``."""
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write", cwd=".")
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert tmp_path.resolve() in config.resolve().parents, "precondition: the config dir is inside cwd"
    target = config / "projects" / _slug(tmp_path) / "memory" / "note.md"
    fake_anthropic.script([[_write("tu", target)]])
    await _run(agent)

    assert not target.exists(), (
        f"the hook must deny a Write into the config dir inside cwd: {_tool_results(fake_anthropic)}"
    )
    assert "configuration directory" in _tool_results(fake_anthropic)["tu"], _tool_results(fake_anthropic)


# ── .claude under the opt-in (R15/R11) ───────────────────────────────────────


@pytest.mark.asyncio
@_needs_sandbox
async def test_dot_claude_writes_are_denied_under_the_opt_in(tmp_path, fake_anthropic, monkeypatch):
    """When a host opts into the repo's settings, the hook denies the agent's own writes under
    ``<cwd>/.claude`` so it cannot plant settings for its next turn (measured: the Write tool reached
    can_use_tool for ``.claude/settings.json`` and created it when allowed)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text("{}")
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write", setting_sources=["project"])
    target = ws / ".claude" / "planted.json"
    fake_anthropic.script([[_write("tu", target)]])
    await _run(agent)

    assert not target.exists(), (
        f"a write under <cwd>/.claude must be denied under the opt-in: {_tool_results(fake_anthropic)}"
    )
    assert ".claude" in _tool_results(fake_anthropic)["tu"]


@pytest.mark.asyncio
@_needs_sandbox
async def test_dot_claude_writes_are_allowed_without_the_opt_in(tmp_path, fake_anthropic, monkeypatch):
    """The control: without ``setting_sources``, ``<cwd>/.claude`` is an ordinary in-root directory
    — the CLI loads nothing from it (strict, no repo settings), so the hook does not single it out
    and an in-root write there lands. This keeps the opt-in test above from passing vacuously."""
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    target = ws / ".claude" / "planted.json"
    fake_anthropic.script([[_write("tu", target)]])
    await _run(agent)

    assert target.exists(), f"without the opt-in, an in-root .claude write should land: {_tool_results(fake_anthropic)}"


# ── Cross-session and beyond-session tools (R10, R9) ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", _LEVELS)
async def test_cross_session_tools_are_never_offered_or_callable(tmp_path, fake_anthropic, monkeypatch, permission):
    """R10: ListAgents and SendMessage reach other local Claude sessions, which no ``permission``
    grants, so they are excluded at every level, ``full-access`` included — neither offered nor
    callable (the CLI reports "No such tool available" when the model asks)."""
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission=permission)
    fake_anthropic.script([[_tu("tu_la", "ListAgents"), _tu("tu_sm", "SendMessage", to="main", message="hi")]])
    await _run(agent)
    offered = {t["name"] for t in fake_anthropic.requests[0]["tools"]}
    results = _tool_results(fake_anthropic)

    assert {"ListAgents", "SendMessage"}.isdisjoint(offered), f"cross-session tools offered at {permission!r}"
    assert "No such tool available" in results["tu_la"], results
    assert "No such tool available" in results["tu_sm"], results


@pytest.mark.asyncio
async def test_persist_or_move_tools_are_excluded_below_full_access(tmp_path, fake_anthropic, monkeypatch):
    """R9: tools that persist beyond the turn or move/branch the session (here CronList and
    EnterWorktree) are not offered below ``full-access``; at ``full-access`` they are (it confines
    nothing)."""
    ro = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")
    fake_anthropic.script([[{"type": "text", "text": "ok"}]])
    await _run(ro)
    ro_offered = {t["name"] for t in fake_anthropic.requests[0]["tools"]}
    assert {"CronList", "EnterWorktree", "Workflow", "Agent"}.isdisjoint(ro_offered)

    fa_fake = FakeAnthropic()
    try:
        fa = _agent(tmp_path, monkeypatch, fa_fake, permission="full-access")
        fa_fake.script([[{"type": "text", "text": "ok"}]])
        await _run(fa)
        fa_offered = {t["name"] for t in fa_fake.requests[0]["tools"]}
        assert {"CronList", "EnterWorktree", "Workflow", "Agent"} <= fa_offered
    finally:
        fa_fake.close()


# ── can_use_tool: the backstop, not the gate (R3, fact 3) ────────────────────


def test_can_use_tool_answers_by_policy(tmp_path, fake_anthropic, monkeypatch):
    """The backstop's answers, unit-tested: BOS's own MCP tools allowed (Task 10 scopes the name),
    Bash allowed under ``workspace-write`` (the sandbox confines it), everything else denied; and
    ``None`` under ``full-access``, which never consults it."""

    async def call(agent: ClaudeCodeAgent, name: str) -> Any:
        cb = agent._can_use_tool()
        assert cb is not None
        return await cb(name, {}, None)

    ro = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")
    ww = (
        _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
        if not (shutil.which("bwrap") is None or shutil.which("socat") is None)
        else None
    )
    fa = _agent(tmp_path, monkeypatch, fake_anthropic, permission="full-access")

    # read-only allows its own MCP tools and its one allowlisted, hook-vetted tool (Read); denies the rest.
    assert isinstance(asyncio.run(call(ro, "mcp__bos-tools__save")), PermissionResultAllow)
    assert isinstance(asyncio.run(call(ro, "Read")), PermissionResultAllow)
    assert isinstance(asyncio.run(call(ro, "Bash")), PermissionResultDeny), "Bash not in read-only's allowlist"
    assert isinstance(asyncio.run(call(ro, "WebFetch")), PermissionResultDeny), "web tools excluded below full-access"
    if ww is not None:
        assert isinstance(asyncio.run(call(ww, "Bash")), PermissionResultAllow), "the sandbox confines Bash"
        assert isinstance(asyncio.run(call(ww, "Write")), PermissionResultAllow), "the hook confined the Write to cwd"
        assert isinstance(asyncio.run(call(ww, "WebFetch")), PermissionResultDeny)
    assert fa._can_use_tool() is None, "full-access installs no can_use_tool (bypassPermissions shadows it)"


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", _LEVELS)
async def test_which_calls_reach_can_use_tool(tmp_path, fake_anthropic, monkeypatch, permission):
    """R3: which calls reach ``can_use_tool`` under each mapped mode, with the real hook in place —
    measured and pinned. The hook denies before ``can_use_tool`` for anything out of the allowlist or
    out of the root, the mode auto-allows in-root edits, and the sandbox handles bash — so under
    ``read-only`` and ``workspace-write`` only a handful reach the callback, and under ``full-access``
    (bypassPermissions) none does."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hi\n")
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission=permission)
    reached: list[tuple[str, str]] = []
    base = agent._can_use_tool()

    async def recording(name: str, tool_input: dict[str, Any], context: Any) -> Any:
        result = await base(name, tool_input, context) if base is not None else PermissionResultAllow()
        reached.append((name, type(result).__name__))
        return result

    # A representative in-root spread: a Read, an in-root Write, an in-root filesystem Bash, and an
    # in-root python3 -c that writes (the command class acceptEdits does not auto-allow, fact 3).
    scripted = ws / "s.txt"
    py = f"python3 -c \"open({str(ws / 'p.txt')!r}, 'w').close()\""
    fake_anthropic.script([
        [
            _tu("r", "Read", file_path=str(ws / "notes.txt")),
            _write("w", scripted),
            _bash("b", f"touch {ws / 't.txt'}"),
            _bash("py", py),
        ]
    ])
    base_options = agent._options()
    options = replace(
        base_options, can_use_tool=recording, env={**base_options.env, **claude_cli_env(tmp_path, fake_anthropic)}
    )
    async with asyncio.timeout(90):
        async with ClaudeSDKClient(options) as client:
            await client.query("go")
            messages = [m async for m in client.receive_response()]
    assert isinstance(messages[-1], ResultMessage), messages[-1]

    # Measured with BOS's mapped modes: under NONE of them does an in-root file or bash operation
    # reach can_use_tool. full-access (bypassPermissions) never consults it; workspace-write auto-
    # allows in-root edits (acceptEdits) and every bash command (the sandbox is on —
    # autoAllowBashIfSandboxed, fact 6b), so not even the `python3 -c` case that reaches it WITHOUT a
    # sandbox (fact 3) reaches it here; read-only offers no Write/Bash and serves Read without asking.
    # So the callback is the pure backstop §3.5.3 says it is — the only calls it would answer in
    # practice are BOS's own MCP tools (Task 10), not exercised here.
    assert reached == [], f"no in-root file/bash call reaches can_use_tool at {permission!r}: {reached}"


# ── The sandbox tripwire (plan text) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sandbox_tripwire_ends_the_turn(tmp_path, fake_claude, monkeypatch):
    """A simulated "Sandbox disabled" stderr line under ``workspace-write`` interrupts the turn and
    raises ``_SandboxDisabledError`` — the third layer behind construction and ``failIfUnavailable``
    (fact 6c), through Task 7's interrupt machinery. Driven against ``FakeClaudeClient`` so the line
    can be injected on demand."""
    monkeypatch.setattr(claude_code, "_bash_sandbox_unavailable", lambda platform: None)  # let ww construct
    from conftest import HANG

    (tmp_path / "ws").mkdir()
    agent = ClaudeCodeAgent(
        kind="george",
        cfg={"permission": "workspace-write", "auth": "api_key", "cwd": "ws"},
        chat_store=None,
        workspace=tmp_path,
        mcp=_no_mcp,
        structured_validator=JsonSchemaValidator(),
    )
    from claude_agent_sdk import AssistantMessage, TextBlock

    working = AssistantMessage(content=[TextBlock(text="working")], model="m", parent_tool_use_id=None)
    fake_claude.arm(messages=[working, HANG])
    turn = asyncio.ensure_future(_run(agent))
    client = await _poll(lambda: fake_claude.instances[0] if fake_claude.instances else None)
    await _poll(lambda: client.hang_reached.is_set() or None)
    # The stderr reader would call this from its own task; simulate the CLI's warning line.
    client.options.stderr("[ERROR] Sandbox disabled: dependencies missing. Commands will run WITHOUT sandboxing")
    client.hang_release.set()

    with pytest.raises(_SandboxDisabledError, match="sandbox disabled"):
        await asyncio.wait_for(turn, timeout=5)
    assert {"subtype": "interrupt", "cancel_queued": True} in client.control_requests, "the turn was interrupted"


async def _poll(predicate: Any, *, timeout: float = 3.0) -> Any:
    async with asyncio.timeout(timeout):
        while True:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.01)


def test_the_tripwire_only_arms_under_workspace_write(tmp_path, fake_anthropic, monkeypatch):
    """Only ``workspace-write`` has a bash sandbox to degrade, so ``run()`` sets the stderr tripwire
    only there; at other levels ``_options()`` leaves ``stderr`` unset. Asserted on the callback the
    agent builds, so the wiring is pinned without a turn."""
    tripped: list[str] = []
    cb = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")._sandbox_tripwire(tripped, "c", "t")
    cb("[ERROR] Sandbox disabled: whatever")
    assert tripped == ["[ERROR] Sandbox disabled: whatever"], "the callback records the line whenever called"
    # A benign line is ignored.
    cb("some other stderr noise")
    assert len(tripped) == 1


# ── The git the CLI runs on the host (§8.2(h)) ───────────────────────────────


@pytest.mark.asyncio
async def test_git_config_exec_settings_do_not_run_at_startup(tmp_path, fake_anthropic, monkeypatch):
    """§8.2(h): the CLI runs ``git status`` etc. in ``cwd`` on the host at every turn's start, and a
    ``core.fsmonitor`` in ``<cwd>/.git/config`` is an exec vector. The CLI neutralizes it — by
    default it pins ``core.fsmonitor``/``core.hooksPath`` on every git call it makes
    (``allowRepoGitHooks`` defaults false, ``_Ne``/``hct`` in the CLI source) — so a fsmonitor
    planted in the repo does NOT run at the CLI's startup git, while a direct host ``git status``
    does. This is why the hook does not need to deny ``.git`` writes. A CLI that flips that default
    fails here."""
    import subprocess

    ws = tmp_path / "ws"
    ws.mkdir()
    marker = tmp_path / "fsmonitor_ran"
    env0 = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env0)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True, env=env0)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True, env=env0)
    (ws / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True, env=env0)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, check=True, env=env0)
    fsm = ws / "fsm.sh"
    fsm.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fsm.chmod(0o755)
    with (ws / ".git" / "config").open("a") as handle:
        handle.write(f"[core]\n\tfsmonitor = {fsm}\n")

    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="full-access")
    fake_anthropic.script([[{"type": "text", "text": "hi"}]])
    await _run(agent)
    assert not marker.exists(), "the CLI's startup git ran the repo's core.fsmonitor on the host"

    subprocess.run(["git", "status", "--short"], cwd=ws, check=True, env=env0)
    assert marker.exists(), "control: the fsmonitor IS exec-capable on a direct host git status"


# ── A broken bwrap fails closed rather than running unsandboxed (R13) ─────────


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bwrap to shadow with a broken one")
async def test_a_broken_bwrap_fails_closed(tmp_path, fake_anthropic, monkeypatch):
    """R13: a ``bwrap`` that is installed but exits non-zero (BOS's construction check finds it
    present) does NOT run a sandboxed command unsandboxed — the command fails closed. So an
    installed-but-unusable bwrap is a per-command failure, not an escape."""
    farm = tmp_path / "farm"
    entries = []
    for i, entry in enumerate(os.environ["PATH"].split(os.pathsep)):
        if entry and (Path(entry) / "bwrap").exists():
            slot = farm / str(i)
            slot.mkdir(parents=True)
            for item in Path(entry).iterdir():
                if item.name != "bwrap":
                    (slot / item.name).symlink_to(item)
            broken = slot / "bwrap"
            broken.write_text("#!/bin/sh\necho 'bwrap: broken' >&2\nexit 1\n")
            broken.chmod(0o755)
            entry = str(slot)
        entries.append(entry)
    if shutil.which("socat") is None:
        pytest.skip("needs socat for workspace-write to construct")
    monkeypatch.setenv("PATH", os.pathsep.join(entries))

    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / "escaped.txt"
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    fake_anthropic.script([[_bash("tu", f"touch {escaped}")]])
    await _run(agent)

    assert not escaped.exists(), f"a broken bwrap must not run the command unsandboxed: {_tool_results(fake_anthropic)}"


# ── @path reads are unconfined, documented not fixed ─────────────────────────


@pytest.mark.asyncio
async def test_an_at_path_prompt_reads_outside_the_root_unconfined(tmp_path, fake_anthropic, monkeypatch):
    """Measured residual (BEP §3.5.5, §8.2): an ``@<path>`` in the prompt makes the CLI read the file
    itself and send it to the model — unseen by the hook, and outside ``cwd``. This is another
    unconfined read path, like bash reads; documented, not fixed (``verbatim_prompts`` is refused,
    so it is not a tuning knob, but the CLI expands ``@path`` by default). Pinned so a change is
    noticed."""
    secret = tmp_path / "secret.txt"
    secret.write_text("CANARY-atpath-7c2f\n")
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")
    fake_anthropic.script([[{"type": "text", "text": "ok"}]])
    async with asyncio.timeout(90):
        await agent.run("chat-1", f"Summarize @{secret}", turn_id="t1")

    assert any("CANARY-atpath-7c2f" in json.dumps(body) for body in fake_anthropic.requests), (
        "the @path file reached the model (unconfined read, documented not fixed)"
    )


# ── R14: the shared /tmp/claude-<uid> is denied to the agent's sandboxed bash ─


@pytest.mark.asyncio
@_needs_sandbox
async def test_workspace_write_bash_cannot_write_the_shared_tmp_root(tmp_path, fake_anthropic, monkeypatch):
    """R14 hardening: under ``workspace-write`` a per-turn ``TMPDIR`` moves the bash sandbox's writable
    temp root off the shared ``<system temp>/claude-<uid>`` (every live Claude Code session of the same
    user shares it), so the agent's own commands keep a working temp there while a bash write to the
    shared root is refused. Measured: TMPDIR alone suffices (the plan's candidate ``permissions.deny``
    Edit rule proved redundant). The test creates its own probe dir under the shared root and removes
    it; it never touches any other session's files there.

    Reads are not restricted (§3.5.5), so this closes the bash-*write* half only."""
    shared = Path(tempfile.gettempdir()) / f"claude-{os.getuid()}"
    probe = shared / f"bos-t8fix-{uuid.uuid4().hex}"
    probe.mkdir(parents=True)
    shared_target = probe / "escaped.txt"
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
        inroot = ws / "in.txt"
        # Simple, separate steps: an in-root touch, a mktemp (which must land in the per-turn TMPDIR,
        # not the shared root), then a touch of the shared root the sandbox must refuse.
        cmd = (
            f"touch {inroot} && echo IN_OK; "
            f"d=$(mktemp -d) && echo MKTEMP_OK:$d || echo MKTEMP_FAIL; "
            f"touch {shared_target} 2>&1; echo done"
        )
        fake_anthropic.script([[_bash("tu", cmd)]])
        await _run(agent)
        res = _tool_results(fake_anthropic).get("tu", "")

        assert not shared_target.exists(), f"the agent's bash wrote the shared tmp root: {res}"
        assert "Read-only file system" in res, f"the sandbox should refuse the shared write: {res}"
        assert "IN_OK" in res, f"an in-root command must still work: {res}"
        assert "MKTEMP_OK" in res, f"the per-turn TMPDIR must keep temp working: {res}"
        assert str(shared) not in res.split("MKTEMP_OK:", 1)[-1].splitlines()[0], "mktemp must not use the shared root"
    finally:
        shutil.rmtree(probe, ignore_errors=True)


@pytest.mark.asyncio
@_needs_sandbox
async def test_the_per_turn_tmpdir_is_removed_after_the_turn(tmp_path, fake_anthropic, monkeypatch):
    """The per-turn ``TMPDIR`` BOS creates under ``workspace-write`` is removed once the turn's client
    disconnects (BEP 19 §3.5.3, R14). Captured from the built options via the client factory, then
    asserted gone after ``run()`` returns."""
    seen: list[str] = []
    real_factory = claude_code._CLIENT_FACTORY

    def factory(options):  # type: ignore[no-untyped-def]
        tmpdir = (options.env or {}).get("TMPDIR")
        if tmpdir:
            seen.append(tmpdir)
        return real_factory(options)

    monkeypatch.setattr(claude_code, "_CLIENT_FACTORY", factory)
    (tmp_path / "ws").mkdir()
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    fake_anthropic.script([[{"type": "text", "text": "ok"}]])
    await _run(agent)

    assert seen, "run() set a per-turn TMPDIR under workspace-write"
    assert seen[0].startswith(tempfile.gettempdir()), seen
    assert not Path(seen[0]).exists(), "the per-turn TMPDIR was removed after the turn"


# ── Review Minors ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_hook_denies_a_tool_outside_the_levels_allowlist(tmp_path, fake_anthropic, monkeypatch):
    """Minor #1: the hook's allowlist backstop (`tool not in allowed` → deny), pinned directly —
    `tools=` shadows it end-to-end (an excluded tool never reaches the hook), so a unit call is what
    catches it silently breaking. A `read-only` agent's hook denies `Bash` (not in its allowlist)."""
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")
    hook = agent._hook()
    decision = await hook({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, "tu", None)

    out = decision.get("hookSpecificOutput", {})  # type: ignore[union-attr]
    assert out.get("permissionDecision") == "deny", decision
    assert "not available" in out.get("permissionDecisionReason", ""), decision
    # A tool that IS in the allowlist, in-root, is not denied by the backstop.
    allowed = await hook({"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "ws" / "f")}}, "tu", None)
    assert allowed == {}, allowed


@pytest.mark.asyncio
async def test_the_stderr_tripwire_is_wired_only_under_workspace_write(tmp_path, fake_claude, monkeypatch):
    """Minor #2: `run()` wires the stderr tripwire only under `workspace-write` (the one level with a
    bash sandbox to degrade). Asserted on the options the client factory received: `stderr` is set at
    `workspace-write`, `None` at `read-only` and `full-access`. Driven against `FakeClaudeClient`."""
    monkeypatch.setattr(claude_code, "_bash_sandbox_unavailable", lambda platform: None)  # let ww construct

    async def stderr_for(permission: str) -> Any:
        (tmp_path / permission).mkdir()
        agent = ClaudeCodeAgent(
            kind="george",
            cfg={"permission": permission, "auth": "api_key", "cwd": "."},
            chat_store=None,
            workspace=tmp_path / permission,
            mcp=_no_mcp,
            structured_validator=JsonSchemaValidator(),
        )
        fake_claude.arm(messages=_fake_turn("ok"))
        await _run(agent)
        return fake_claude.instances[-1].options.stderr

    assert callable(await stderr_for("workspace-write")), "the tripwire is wired under workspace-write"
    assert await stderr_for("read-only") is None, "no tripwire at read-only (no sandbox)"
    assert await stderr_for("full-access") is None, "no tripwire at full-access (sandbox off)"


def test_cli_config_dir_falls_back_to_home_when_unset(tmp_path, monkeypatch):
    """Minor #3: `_cli_config_dir()` returns `<HOME>/.claude` when `CLAUDE_CONFIG_DIR` is unset —
    computed only, under a patched `HOME`, never reading the owner's real directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert claude_code._cli_config_dir() == (home / ".claude").resolve()
    # And it honours an explicit CLAUDE_CONFIG_DIR.
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert claude_code._cli_config_dir() == cfg.resolve()


# ── R12: cells §3.5.3 named but had not pinned ───────────────────────────────


@pytest.mark.asyncio
@_needs_sandbox
async def test_the_hook_sees_a_bash_call(tmp_path, fake_anthropic, monkeypatch):
    """R12: the PreToolUse hook fires for `Bash`, not only the file tools — pinned by wrapping BOS's
    own hook to record the tool names it is handed on a real `workspace-write` turn."""
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="workspace-write")
    seen: list[str] = []
    real_hook = agent._hook()

    async def recording(input_data, tool_use_id, context):  # type: ignore[no-untyped-def]
        seen.append(dict(input_data).get("tool_name", ""))
        return await real_hook(input_data, tool_use_id, context)

    options = replace(
        agent._options(),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[recording])]},
        env={**agent._options().env, **claude_cli_env(tmp_path, fake_anthropic)},
    )
    fake_anthropic.script([[_bash("tu", f"touch {ws / 'f.txt'}")]])
    async with asyncio.timeout(90):
        async with ClaudeSDKClient(options) as client:
            await client.query("go")
            messages = [m async for m in client.receive_response()]
    assert isinstance(messages[-1], ResultMessage), messages[-1]
    assert "Bash" in seen, f"the hook should see the Bash call: {seen}"


@pytest.mark.asyncio
async def test_dontask_refuses_an_in_root_write(tmp_path, fake_anthropic, monkeypatch):
    """R12: `dontAsk` refuses even an in-root `Write` that nothing pre-approved — the vendor fact
    behind §3.5.3's note that BOS never maps to it (a `workspace-write` agent under `dontAsk` could
    not write at all). A standalone real-CLI turn; BOS never sends this mode."""
    ws = tmp_path / "ws"
    ws.mkdir()
    inroot = ws / "in.txt"
    env = claude_cli_env(tmp_path, fake_anthropic)
    options = ClaudeAgentOptions(
        cwd=ws,
        env=env,
        setting_sources=[],
        system_prompt={"type": "preset", "preset": "claude_code"},
        permission_mode="dontAsk",
    )
    fake_anthropic.script([[_write("tu", inroot)]])
    async with asyncio.timeout(90):
        async with ClaudeSDKClient(options) as client:
            await client.query("go")
            messages = [m async for m in client.receive_response()]
    assert isinstance(messages[-1], ResultMessage), messages[-1]
    assert not inroot.exists(), (
        f"dontAsk should refuse an in-root Write nothing pre-approved: {_tool_results(fake_anthropic)}"
    )
    assert "don't ask" in _tool_results(fake_anthropic)["tu"], _tool_results(fake_anthropic)


# ── Tool search off: the full tool surface is offered up front ───────────────


@pytest.mark.asyncio
async def test_bos_pins_tool_search_off_so_the_full_tool_surface_is_offered(tmp_path, fake_anthropic, monkeypatch):
    """The CLI turns tool search ON on a first-party Anthropic host (production, subscription login;
    read from source), and with it on offers a ``DeferredToolPlaceholder`` the classification does not
    know. BOS pins it off with ``ENABLE_TOOL_SEARCH="false"`` (BEP 19 §3.12), keeping the surface the
    confinement and the MCP egress were measured against.

    Pinned by forcing tool search on in the test process's environment (``ENABLE_TOOL_SEARCH=true``,
    which the CLI honours even against the fake base URL), then showing BOS's override wins: the tools
    offered under BOS's options carry no ``DeferredToolPlaceholder`` and equal the classified set. The
    control — the same options with BOS's override removed — shows the placeholder appears, so the
    test cannot pass vacuously."""
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")  # force it on even against the fake base URL
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = _agent(tmp_path, monkeypatch, fake_anthropic, permission="read-only")

    async def offered(env_override: dict[str, str]) -> set[str]:
        fake = FakeAnthropic()
        try:
            base = agent._options()
            options = replace(base, tools=None, env={**base.env, **claude_cli_env(tmp_path, fake), **env_override})
            fake.script([[{"type": "text", "text": "ok"}]])
            async with asyncio.timeout(90):
                async with ClaudeSDKClient(options) as client:
                    await client.query("go")
                    assert isinstance([m async for m in client.receive_response()][-1], ResultMessage)
            return {t["name"] for t in fake.requests[0]["tools"]}
        finally:
            fake.close()

    with_override = await offered({})  # BOS's env already carries ENABLE_TOOL_SEARCH="false"
    assert "DeferredToolPlaceholder" not in with_override, with_override
    assert "ToolSearch" not in with_override, with_override
    assert with_override == set(_TOOL_LEVELS), "the full classified surface is offered up front"

    without_override = await offered({"ENABLE_TOOL_SEARCH": "true"})  # drop BOS's pin
    assert "DeferredToolPlaceholder" in without_override, (
        f"control: with tool search on, the CLI offers a placeholder: {sorted(without_override)}"
    )
