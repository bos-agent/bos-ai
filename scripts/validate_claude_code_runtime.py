#!/usr/bin/env python
"""Live validation of BEP 19 Layer 4b (``ClaudeCodeAgent``) against a real Claude subscription.

**This is not a test and must never run in CI or be imported from a test path.** It signs in as
whoever owns the machine, spends that person's Claude subscription quota on real model turns, and
writes files both inside and — deliberately, as part of the confinement items — outside the
agent's working directory. A green CI run must never cost a human money or touch their account, so
the only thing that starts this script is a person typing its name. It lives under ``scripts/``
(no ``__init__.py``, not on ``sys.path``, no ``test_`` prefix, no test function in it) so nothing
collects it by accident.

Modelled on ``scripts/validate_codex_runtime.py``: the same phases, the same mark vocabulary
(``PASS`` / ``FAIL`` / ``OBSERVED`` / ``NOT ARRANGED`` / ``ERROR``), the same rule that nothing here
ever invents a result, and the same paste-ready output for BEP §8.1 / §9. It exists because BEP §7
criteria 15-25 ask questions no fake can answer: whether a real ``claude`` CLI streams events for a
real model, whether its bash sandbox really denies a write on this host, whether the vendor really
reports ``aborted_tools`` / ``aborted_streaming`` — not Codex's ``interrupted`` — for a stop BOS
asked for. Tasks 1-10 proved everything that can be proved without a login (including the whole
confinement matrix, in CI, against the real CLI with a fake model); this collects the rest.

Four modes, three of which spend quota:

    uv run python scripts/validate_claude_code_runtime.py start --yes [WORKSPACE]
    uv run python scripts/validate_claude_code_runtime.py resume --yes     # prints the full table
    uv run python scripts/validate_claude_code_runtime.py auth-preflight --yes   # item 23, ZERO quota

``start`` prints the exact ``resume`` command to run next, because item 20 (§7.17) asks what
survives a process restart. ``resume`` merges ``start``'s recorded results with its own and prints
the paste-ready table for both.

Two more modes cost nothing and need no network or login at all::

    uv run python scripts/validate_claude_code_runtime.py self-check   # asserts the script's own wiring
    uv run python scripts/validate_claude_code_runtime.py plan --yes   # the banner and item list only

``auth-preflight`` also costs nothing: BEP 19 §3.10.3 says an inherited credential variable is
refused at *construction*, before any CLI is ever spawned, so proving that needs no login either —
it plants an obviously-fake ``ANTHROPIC_API_KEY`` in this process's own environment (never a real
key, never printed) and checks that building a ``claude-code`` agent under the default
``auth = "subscription"`` raises immediately.

**Nothing here ever invents a result.** An item the script could not arrange prints
``NOT ARRANGED`` with the reason — "needs the owner's own interactive trust decision", "no
enterprise managed-mcp.json on this host" — and stays out of the pass column. Several items
(the confinement matrix, the MCP call, the transcript read, the corrected ``request_stop()``
wording) are checked directly; a handful of live-login-only facts named in the Task 11 brief
(the ``ant`` profile store, macOS-only facts, an enterprise managed MCP file) are source-only or
host-conditional and are marked ``NOT ARRANGED`` on an ordinary Linux dev box by design — that is
not a gap in the script, it is the honest answer for a fact this host cannot exhibit.

Security, held to throughout: this script never prints, logs or writes a token or credential —
even the deliberate ``ANTHROPIC_BASE_URL`` probe (item 15) redacts every header value and reports
only header *names*; it never touches the owner's real ``~/.claude``, ``~/.claude.json`` or
``~/.config/anthropic`` — the one item that would ask about the last of those (item 16, the ``ant``
profile store) is answered from BEP source text instead, precisely so nothing here has to open that
directory; and its workspaces live under a temp directory it creates and reports, with a second,
clearly-labelled directory it creates *outside* that workspace on purpose, for the items that must
prove a write there is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from bos.sdk import BosApp, TurnEvent, Workspace, ep_tool

# ── The host tool items 10 and 11 need ──────────────────────────────────────
#
# Registered at import, in the process-global `ep_tool` registry, exactly as
# `scripts/validate_codex_runtime.py` registers its own pair — `BosApp.__aenter__` ->
# `bootstrap_platform()` has to see them before any agent asks for an MCP server.
# `EXPOSED` is the one `mcp_tools` grants; `WITHHELD` is registered and deliberately
# never granted, which is the half of §7.22 that says an unexposed tool stays uncallable.

EXPOSED_TOOL = "ValidateEcho"
WITHHELD_TOOL = "ValidateSecret"

TOOL_CALLS: list[tuple[str, str]] = []
"""Every host-tool invocation the child actually reached, in order — host-side state, mutated
over MCP from inside whatever the CLI's sandbox confines, which is exactly what item 11 measures
under `read-only`: the sandbox confines the filesystem, not BOS's own tool surface."""

_ONE_STRING = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


@ep_tool(name=EXPOSED_TOOL, description="Record a line of text with the BOS host.", parameters=_ONE_STRING)
async def _validate_echo(text: str) -> str:
    TOOL_CALLS.append((EXPOSED_TOOL, text))
    return f"BOS recorded: {text}"


@ep_tool(name=WITHHELD_TOOL, description="Never granted to any agent.", parameters=_ONE_STRING)
async def _validate_secret(text: str) -> str:
    TOOL_CALLS.append((WITHHELD_TOOL, text))
    return "THIS TOOL SHOULD NEVER HAVE BEEN REACHABLE"


# ── Result vocabulary ───────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
OBSERVED = "OBSERVED"  # recorded for the BEP; the checklist defines no pass/fail
NOT_ARRANGED = "NOT ARRANGED"  # deliberately unmarked — the setup was not possible
ERROR = "ERROR"  # the check itself blew up, which is not the same as a FAIL

MARKS = (PASS, FAIL, OBSERVED, NOT_ARRANGED, ERROR)

Outcome = tuple[str, str]


@dataclass
class Item:
    n: int
    criterion: str
    phase: str
    turns: int
    title: str
    fn: Callable[[Ctx], Awaitable[Outcome]]
    mark: str = ""
    note: str = ""


CHECKS: list[Item] = []


def check(n: int, criterion: str, phase: str, turns: int, title: str) -> Callable[..., Any]:
    """Register one checklist item. *turns* is the model turns it costs at most, and is what the
    up-front budget in the banner is summed from — an item that is normally ``NOT ARRANGED`` (item
    19's quota check, item 5's trust case) still declares its worst-case cost here."""

    def deco(fn: Callable[[Ctx], Awaitable[Outcome]]) -> Callable[[Ctx], Awaitable[Outcome]]:
        CHECKS.append(Item(n=n, criterion=criterion, phase=phase, turns=turns, title=title, fn=fn))
        return fn

    return deco


# ── Agent kinds ─────────────────────────────────────────────────────────────
#
# One `[agents.<kind>]` per distinct runtime configuration, since a built agent cannot be
# reconfigured (`BosApp.build_agent` refuses it). Each inherits `_parent = "claude-code"`
# (BEP 19 §3.4's recommended shape), which is what proves dispatch works off `external_runtime`,
# not off the name — the same reason `validate_codex_runtime.py` inherits from `"codex"`.
#
# `resume` declares exactly ONE claude-code agent on purpose: `BosApp.get_messages(source="native")`
# matches agents by `resolved_config["external_runtime"]` and raises when two declare the same
# runtime, so item 21 can only be observed through `BosApp` in a phase with a single one.

SYSPROMPT_PHRASE = "MOONLIGHT-42"
SYSPROMPT_INSTRUCTION = (
    f"Whenever asked to identify yourself, include the exact phrase {SYSPROMPT_PHRASE} somewhere in your answer."
)

AGENTS_START: dict[str, dict[str, Any]] = {
    "mcp": {"_parent": "claude-code", "permission": "workspace-write", "mcp_tools": [EXPOSED_TOOL]},
    "rw": {"_parent": "claude-code", "permission": "workspace-write"},
    # Item 7 gets an agent of its own because `request_stop()` is one-way: `_stop_requested` is an
    # `asyncio.Event` that is set and never cleared, so every later turn on the same instance would
    # end instantly. Sharing `rw` here would silently break every item that runs after it.
    "stop": {"_parent": "claude-code", "permission": "workspace-write"},
    "ro": {"_parent": "claude-code", "permission": "read-only", "mcp_tools": [EXPOSED_TOOL]},
    "full": {"_parent": "claude-code", "permission": "full-access"},
    "slow": {"_parent": "claude-code", "permission": "workspace-write", "timeout_seconds": 5.0},
    # setting_sources=["project"]: the CLI loads CLAUDE.md and the repo's own .claude/settings.json
    # itself (BEP 19 §3.4.1.4, §3.5.3) — items 5 and 13 both use it, item 5 only under --repo-trusted.
    "project": {"_parent": "claude-code", "permission": "workspace-write", "setting_sources": ["project"]},
    "sysprompt": {"_parent": "claude-code", "permission": "workspace-write", "system_prompt": SYSPROMPT_INSTRUCTION},
}

AGENTS_RESUME: dict[str, dict[str, Any]] = {
    "mcp": {"_parent": "claude-code", "permission": "workspace-write", "mcp_tools": [EXPOSED_TOOL]},
}

MAIN_CHAT = "validate-main"
CODEWORD = "STARFRUIT"
STATE_FILE = "validate-state.json"
# Item 3's "outside the workspace" target, and item 13's hook marker, live here, not under /tmp:
# Claude Code's bash sandbox does confine /tmp under workspace-write (unlike Codex's, BEP 19 §3.5),
# but the *file-tool* hook denial (§3.5.3) is what items 3, 5 and 13 exercise, and it should hold
# regardless of where the target sits — a directory under the operator's own home, created fresh per
# phase and removed when it ends, is the least ambiguous "outside" there is.
OUTSIDE_PARENT = Path.home() / ".cache" / "bos-claude-code-validate"

# Items that write outside the workspace, or need a directory to hold a repo hook's marker file —
# both live under one `outside` directory per phase (see `run_phase`).
_NEEDS_OUTSIDE = frozenset({3, 4, 5, 13})


def workspace_config(agents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`platform.extensions = []` rather than the default `["bos.exts", ...]`: nothing here needs a
    provider, a channel or a plugin, and the chat store below is a `bos.core.defaults` built-in that
    registers on import. `JsonlChatStore` is not a choice of convenience — item 20 recovers a Claude
    Code session id from it *after this process is gone*, which an in-memory store cannot offer."""
    return {"platform": {"extensions": []}, "harness": {"chat_store": "JsonlChatStore"}, "agents": agents}


# ── Context and turn plumbing ────────────────────────────────────────────────


class Capture:
    """A ``TurnEventSink`` that also times the gaps between events."""

    def __init__(self) -> None:
        self.events: list[TurnEvent] = []
        self.at: list[float] = []

    async def emit(self, event: TurnEvent) -> None:
        self.events.append(event)
        self.at.append(time.monotonic())

    def kinds(self) -> list[str]:
        return [f"{e.event_type}/{e.phase}" + (f"[{e.tool_name}]" if e.tool_name else "") for e in self.events]

    def max_gap(self) -> float:
        return max((b - a for a, b in zip(self.at, self.at[1:], strict=False)), default=0.0)


class WarningLog(logging.Handler):
    """Captures the runtime's own WARNING lines — the `setting_sources` opt-in notice (item 13) and
    any construction refusal — otherwise invisible to a caller."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle.lower() in line.lower()]


def claude_code_agent(app: Any, kind: str) -> Any:
    """One built agent, typed ``Any`` on purpose — the same reach `validate_codex_runtime.py`
    makes: `AgentPort` promises `name`/`request_stop`/`ask`/`run`, while `resolved_config` and
    `aclose` are real parts of what this script observes, reached duck-typed exactly as `BosApp`
    itself reaches them."""
    return app.agent(kind)


@dataclass
class Ctx:
    app: Any
    workspace: Path
    outside: Path
    hook_marks: Path
    warnings: WarningLog
    args: argparse.Namespace
    chat_seq: int = 0
    facts: dict[str, Any] = field(default_factory=dict)

    def agent(self, kind: str) -> Any:
        return claude_code_agent(self.app, kind)

    def chat(self, label: str) -> str:
        self.chat_seq += 1
        return f"validate-{label}-{self.chat_seq}"

    @property
    def store(self) -> Any:
        return self.app.harness.chat_store


async def turn(
    agent: Any, chat_id: str, prompt: str, **kwargs: Any
) -> tuple[Any | None, BaseException | None, Capture, float]:
    """One turn, never raising. Returns ``(result, error, capture, seconds)``. Swallowing the
    exception is the point: half this checklist is about *which* error a live CLI produces, so an
    error is data here, not a failure."""
    cap = Capture()
    started = time.monotonic()
    try:
        result = await agent.run(chat_id, prompt, event_sink=cap, **kwargs)
        return result, None, cap, time.monotonic() - started
    except BaseException as exc:  # noqa: BLE001 - the error is the observation
        return None, exc, cap, time.monotonic() - started


def describe(exc: BaseException | None) -> str:
    if exc is None:
        return "no error"
    text = str(exc).replace("\n", " ")
    return f"{type(exc).__name__}: {text[:400]}"


async def native_session_id_of(ctx: Ctx, chat_id: str) -> str | None:
    """The Claude Code session id BOS recorded for this chat (BEP 19 §3.6) — the same metadata
    `read_native_session_id` reads. The model's reply text proves nothing about session
    continuity; only this id does."""
    for message in reversed(await ctx.store.get_messages(chat_id, active_only=False)):
        if (message.metadata or {}).get("external_runtime") == "claude-code":
            return (message.metadata or {}).get("native_session_id")
    return None


def appeared(path: Path, within: float) -> float | None:
    """Seconds until *path* exists, or None. Used to watch for work the child went on doing after a
    stop (item 7)."""
    deadline = time.monotonic() + within
    started = time.monotonic()
    while time.monotonic() < deadline:
        if path.exists():
            return time.monotonic() - started
        time.sleep(0.25)
    return None


@dataclass
class GateTap:
    """What BOS's own `PreToolUse` hook and `can_use_tool` callback actually decided, for the one
    turn each was wrapped around — unlike Codex's vendor-private reach-in, these are BOS's own
    closures (`ClaudeCodeAgent._hook` / `_can_use_tool`), so wrapping them is not a private-API
    gamble; it only needs the wrapper to preserve the original's behaviour exactly."""

    hook_calls: list[tuple[str, bool]] = field(default_factory=list)  # (tool_name, denied)
    can_use_tool_calls: list[tuple[str, bool]] = field(default_factory=list)  # (tool_name, allowed)


def install_gate_tap(agent: Any) -> GateTap:
    """Wrap *agent*'s `_hook`/`_can_use_tool` methods, once, to record whether the CLI asked them
    and what they decided (BEP 19 §3.5.3). `_options()` calls both once per turn to build a fresh
    closure, so the wrap goes on the bound *method*, which every turn's `_options()` call reaches,
    not on one turn's own closure."""
    existing: GateTap | None = getattr(agent, "_gate_tap", None)
    if existing is not None:
        return existing
    tap = GateTap()
    orig_hook = agent._hook
    orig_can_use_tool = agent._can_use_tool

    def hook_factory() -> Callable[..., Any]:
        fn = orig_hook()

        async def wrapped(input_data: Any, tool_use_id: Any, context: Any) -> Any:
            decision = await fn(input_data, tool_use_id, context)
            denied = bool(decision) and decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            tap.hook_calls.append((dict(input_data).get("tool_name", "?"), denied))
            return decision

        return wrapped

    def can_use_tool_factory() -> Callable[..., Any] | None:
        fn = orig_can_use_tool()
        if fn is None:
            return None

        async def wrapped(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
            decision = await fn(tool_name, tool_input, context)
            tap.can_use_tool_calls.append((tool_name, type(decision).__name__ == "PermissionResultAllow"))
            return decision

        return wrapped

    agent._hook = hook_factory
    agent._can_use_tool = can_use_tool_factory
    agent._gate_tap = tap
    return tap


class _HeaderCapture(http.server.BaseHTTPRequestHandler):
    """Item 15's local double for "somewhere else": records every header of every request it gets,
    then answers with a benign, obviously-synthetic error — never proxies anywhere, never has
    internet access, and this process never sees the CLI's real request past its headers."""

    captured: list[dict[str, str]] = []

    def _capture(self) -> None:
        _HeaderCapture.captured.append(dict(self.headers.items()))
        body = b'{"type":"error","error":{"type":"api_error","message":"validation probe: not a real endpoint"}}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own naming
        self._capture()

    def do_GET(self) -> None:  # noqa: N802
        self._capture()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence stdlib's stderr logging
        pass


def start_header_capture_server() -> http.server.ThreadingHTTPServer:
    """An ephemeral, loopback-only HTTP server for item 15. Stdlib only — this is a local double,
    not a real endpoint, so nothing beyond `http.server` is warranted."""
    _HeaderCapture.captured = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HeaderCapture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── Phase `start` ────────────────────────────────────────────────────────────


@check(1, "§7.15", "start", 1, "A turn starts, streams events, returns non-empty usage")
async def check_01(ctx: Ctx) -> Outcome:
    result, exc, cap, secs = await turn(
        ctx.agent("mcp"), MAIN_CHAT, "Read NOTES.md in your working directory and reply with its first line only."
    )
    if result is None:
        return FAIL, f"the first turn did not complete — {describe(exc)}"
    ctx.facts["first_usage"] = dict(result.usage or {})
    mark = PASS if result.usage else FAIL
    return mark, (
        f"{secs:.1f}s, finish_reason={result.finish_reason!r}, {len(cap.events)} event(s), "
        f"usage={result.usage or '{} (EMPTY — §7.15 not met)'}"
    )


@check(2, "§7.16", "start", 1, "A second ask() on the same chat_id continues the same native session")
async def check_02(ctx: Ctx) -> Outcome:
    before = await native_session_id_of(ctx, MAIN_CHAT)
    _, exc, _, _ = await turn(ctx.agent("mcp"), MAIN_CHAT, "Reply with the single word CONTINUED.")
    after = await native_session_id_of(ctx, MAIN_CHAT)
    ctx.facts["session_id"] = after
    if exc is not None:
        return FAIL, f"the second turn did not complete — {describe(exc)}"
    if before and after and before == after:
        return PASS, f"same Claude Code session across both turns: {after!r} (asserted on the id, not the reply)"
    return FAIL, f"session id changed: {before!r} -> {after!r}"


@check(3, "§7.18", "start", 4, "File-tool confinement per level: in-root ok, out-of-root denied except full-access")
async def check_03(ctx: Ctx) -> Outcome:
    inside = ctx.workspace / "inside-write.txt"
    outside = ctx.outside / "outside-write.txt"
    outside_full = ctx.outside / "outside-write-full.txt"
    _, exc_ro, _, _ = await turn(
        ctx.agent("ro"),
        ctx.chat("confine-ro"),
        f"Create a file named readonly-write.txt in {ctx.workspace} containing OK. "
        "If you cannot, say exactly DENIED and the reason.",
    )
    ro_wrote = (ctx.workspace / "readonly-write.txt").exists()
    _, exc_in, _, _ = await turn(
        ctx.agent("rw"),
        ctx.chat("confine-rw-in"),
        f"Create a file named {inside.name} in your working directory containing OK.",
    )
    _, exc_out, _, _ = await turn(
        ctx.agent("rw"),
        ctx.chat("confine-rw-out"),
        f"Create a file at the absolute path {outside} containing OK. "
        "If you cannot, say exactly DENIED and the reason.",
    )
    _, exc_full, _, _ = await turn(
        ctx.agent("full"),
        ctx.chat("confine-full-out"),
        f"Create a file at the absolute path {outside_full} containing OK.",
    )
    good = not ro_wrote and inside.exists() and not outside.exists() and outside_full.exists()
    detail = (
        f"read-only wrote={ro_wrote} ({describe(exc_ro)}); "
        f"workspace-write in-root written={inside.exists()} ({describe(exc_in)}); "
        f"workspace-write out-of-root written={outside.exists()} at {outside} ({describe(exc_out)}); "
        f"full-access out-of-root written={outside_full.exists()} at {outside_full} ({describe(exc_full)})"
    )
    return (PASS if good else FAIL), detail


@check(4, "§3.5.3", "start", 1, "Bash sandbox confines workspace-write: in-root ok, out-of-root and /tmp denied")
async def check_04(ctx: Ctx) -> Outcome:
    inside = ctx.workspace / "bash-in.txt"
    outside = ctx.outside / "bash-out.txt"
    tmp_probe = Path(tempfile.mkdtemp(dir="/tmp", prefix="bos-claude-code-tmp-probe-"))
    tmp_target = tmp_probe / "bash-tmp.txt"
    try:
        _, exc, cap, _ = await turn(
            ctx.agent("rw"),
            ctx.chat("bash-confine"),
            "Use the Bash tool to run these three commands in order and report each result in one line: "
            f"(1) touch {inside} (2) touch {outside} (3) touch {tmp_target}. Report OK or the exact error "
            "for each.",
        )
        tmp_written = tmp_target.exists()
    finally:
        shutil.rmtree(tmp_probe, ignore_errors=True)
    good = inside.exists() and not outside.exists() and not tmp_written
    return (PASS if good else FAIL), (
        f"in-root written={inside.exists()}; out-of-root written={outside.exists()} at {outside}; "
        f"/tmp written={tmp_written} at {tmp_target} (Claude Code confines /tmp too, unlike Codex's sandbox — "
        "BEP 19 §3.5); "
        f"{describe(exc)}; tool events={[k for k in cap.kinds() if k.startswith('tool')] or 'none'}"
    )


@check(5, "§7.18", "start", 1, "Trusted-repo case: the hook still denies a write the repo's own allow rule approves")
async def check_05(ctx: Ctx) -> Outcome:
    """Fact 4 (§3.5.3) is already pinned in CI against the real CLI with a fake model. What only a
    real login and a genuinely trusted workspace can add is whether it still holds under a real
    model — but marking a workspace trusted means either writing the owner's real `~/.claude.json`
    or relocating `CLAUDE_CONFIG_DIR` (which would also relocate the login, breaking auth for the
    whole run). This script does neither. It runs only when the owner has trusted the workspace
    themselves, through the CLI's own normal interactive use, and says so with `--repo-trusted`."""
    if not ctx.args.repo_trusted:
        return NOT_ARRANGED, (
            f"needs {ctx.workspace} marked trusted by the owner's OWN interactive `claude` run first — this "
            "script never writes ~/.claude.json and never relocates CLAUDE_CONFIG_DIR (that would also move "
            "the login). To arrange: run `claude` by hand once inside that directory, accept its trust prompt, "
            "then re-run this phase with --repo-trusted."
        )
    tap = install_gate_tap(ctx.agent("project"))
    target = ctx.outside / "trusted-repo-write.txt"
    _, exc, _, _ = await turn(
        ctx.agent("project"),
        ctx.chat("trust-case"),
        f"Create a file at the absolute path {target} containing OK. If you cannot, say exactly DENIED and the reason.",
    )
    wrote = target.exists()
    return (FAIL if wrote else PASS), (
        f"out-of-root write under a (claimed) trusted repo whose .claude/settings.json allows Write: "
        f"wrote={wrote}; hook calls={tap.hook_calls[-3:]}; can_use_tool calls={tap.can_use_tool_calls[-3:]}; "
        f"{describe(exc)}"
    )


@check(6, "§3.5.3", "start", 0, "Bash sandbox dependency presence on this host (bwrap+socat / sandbox-exec)")
async def check_06(ctx: Ctx) -> Outcome:
    from bos.extensions.runtimes.claude_code import _bash_sandbox_unavailable

    reason = _bash_sandbox_unavailable(sys.platform)
    if reason is None:
        return OBSERVED, (
            "present — every workspace-write agent in this run should get real bash confinement "
            "(see the bash-sandbox item)"
        )
    return OBSERVED, (
        f"ABSENT: {reason}. "
        'permission="workspace-write" is refused at construction on this host, so this whole run is expected '
        "to fail loudly as soon as BosApp opens rather than degrade gracefully — that refusal IS the "
        "observation the Task 11 brief asks for on a host without the dependency."
    )


@check(7, "§7.19", "start", 1, "request_stop() mid-turn ends the turn with aborted_tools/aborted_streaming")
async def check_07(ctx: Ctx) -> Outcome:
    """The plan text this script was written against said `request_stop()` returns
    `finish_reason="interrupted"`. That is Codex's vocabulary. The runtime as built (BEP 19 §7.19,
    corrected) reports the CLI's own `terminal_reason`: `aborted_tools` while a tool was running,
    `aborted_streaming` while the model was answering — never `interrupted`. This checks for those
    two, not for Codex's word."""
    agent = ctx.agent("stop")
    marker = ctx.workspace / "stop-marker"
    marker.mkdir(exist_ok=True)
    prompt = (
        f"Use the Bash tool to run exactly this command: for i in $(seq -w 1 20); do touch {marker}/f$i.txt; "
        "sleep 2; done. Then reply DONE."
    )

    async def stop_soon() -> None:
        await asyncio.sleep(ctx.args.stop_after)
        agent.request_stop()

    task = asyncio.create_task(stop_soon())
    result, exc, _, secs = await turn(agent, ctx.chat("stop"), prompt)
    await task
    at_return = len(list(marker.glob("*.txt")))
    time.sleep(ctx.args.settle)
    after_settle = len(list(marker.glob("*.txt")))
    reason = getattr(result, "finish_reason", None)
    quiet = at_return == after_settle
    expected = reason in ("aborted_tools", "aborted_streaming")
    reason_note = (
        f"finish_reason={reason!r} — WRONG: that is Codex's vocabulary, not Claude Code's (BEP 19 §7.19)"
        if reason == "interrupted"
        else f"finish_reason={reason!r}"
    )
    return (PASS if (quiet and exc is None and expected) else FAIL), (
        f"returned after {secs:.1f}s, {reason_note}, {describe(exc)}; files at return={at_return}, after "
        f"{ctx.args.settle}s={after_settle} ({'quiet' if quiet else 'STILL WRITING'})"
    )


@check(8, "§7.20", "start", 1, "timeout_seconds expiry while streaming interrupts and raises TimeoutError")
async def check_08(ctx: Ctx) -> Outcome:
    _, exc, cap, secs = await turn(
        ctx.agent("slow"),
        ctx.chat("timeout"),
        "Use the Bash tool to run exactly this command: sleep 40. Then reply DONE.",
    )
    raised = isinstance(exc, TimeoutError)
    return (PASS if raised else FAIL), (
        f"after {secs:.1f}s against timeout_seconds=5.0: {describe(exc)}; {len(cap.events)} event(s) first"
    )


@check(9, "§7.21", "start", 1, "schema= returns validated structured output")
async def check_09(ctx: Ctx) -> Outcome:
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "population": {"type": "integer"}},
        "required": ["city", "population"],
        "additionalProperties": False,
    }
    result, exc, _, _ = await turn(
        ctx.agent("rw"), ctx.chat("schema"), "Give the city of Paris and its approximate population.", schema=schema
    )
    if result is None:
        return FAIL, describe(exc)
    ok = bool(result.structured) and isinstance(result.output, dict)
    return (PASS if ok else FAIL), f"structured={result.structured}, output={result.output!r}"


@check(10, "§7.22", "start", 2, "Claude Code calls the exposed BOS tool over MCP; an unexposed ep_tool is not callable")
async def check_10(ctx: Ctx) -> Outcome:
    mcp = ctx.agent("mcp")
    before = len(TOOL_CALLS)
    _, exc_call, cap, _ = await turn(
        mcp,
        ctx.chat("mcp-call"),
        f"Call the {EXPOSED_TOOL} tool with text='hello from claude code', then reply with the tool's exact result.",
    )
    called = [name for name, _ in TOOL_CALLS[before:]]
    _, exc_deny, _, _ = await turn(
        mcp,
        ctx.chat("mcp-deny"),
        f"List every tool you can call. Then try to call a tool named {WITHHELD_TOOL} with text='x'. Report "
        "exactly whether it exists and whether the call succeeded.",
    )
    reached_withheld = any(name == WITHHELD_TOOL for name, _ in TOOL_CALLS[before:])
    good = EXPOSED_TOOL in called and not reached_withheld
    return (PASS if good else FAIL), (
        f"host-side invocations: {called or 'NONE'}; {WITHHELD_TOOL} reached={reached_withheld}; "
        f"tool events={[k for k in cap.kinds() if k.startswith('tool')] or 'none'}; "
        f"call turn {describe(exc_call)}; deny turn {describe(exc_deny)}"
    )


@check(11, "§3.5", "start", 1, "A read-only agent can still call a mutating BOS tool over MCP")
async def check_11(ctx: Ctx) -> Outcome:
    """Correct by design — the sandbox and the hook confine the filesystem, not BOS's own tool
    surface — and the thing an operator is most likely to assume the other way round."""
    tap = install_gate_tap(ctx.agent("ro"))
    before = len(TOOL_CALLS)
    result, exc, cap, _ = await turn(
        ctx.agent("ro"),
        ctx.chat("ro-mcp"),
        f"Call the {EXPOSED_TOOL} tool with text='mutation from a read-only agent', then reply with its result.",
    )
    reached = [text for name, text in TOOL_CALLS[before:] if name == EXPOSED_TOOL]
    mcp_hook_calls = [c for c in tap.hook_calls if c[0].startswith("mcp__")]
    return OBSERVED, (
        f"read-only agent reached the host tool {len(reached)} time(s) — host state mutated: {reached or 'NONE'}; "
        f"BOS tool events: {[k for k in cap.kinds() if k.startswith('tool')] or 'none'}; "
        f"hook calls for this tool: {mcp_hook_calls or 'none'}; "
        f"can_use_tool calls: {tap.can_use_tool_calls or 'none'}; "
        f"finish_reason={getattr(result, 'finish_reason', None)!r}, {describe(exc)}"
    )


@check(12, "§7.25", "start", 1, "system_prompt is appended: the reply reflects it while tool guidance still works")
async def check_12(ctx: Ctx) -> Outcome:
    """§7.25's half no live run has exercised yet (BEP 19 §8.1): does `system_prompt` really land
    as an *append* — reflected in the reply — while the harness's own tool guidance still works,
    rather than one crowding out the other."""
    target = ctx.workspace / "sysprompt-ok.txt"
    result, exc, _, _ = await turn(
        ctx.agent("sysprompt"),
        ctx.chat("sysprompt"),
        f"Identify yourself as instructed, then create a file named {target.name} in your working directory "
        "containing OK.",
    )
    reply = str(getattr(result, "output", "")) or describe(exc)
    reflected = SYSPROMPT_PHRASE in reply
    wrote = target.exists()
    return (PASS if (reflected and wrote) else FAIL), (
        f"system_prompt reflected={reflected} (reply: {reply[:160]!r}); tool guidance intact (file written)="
        f"{wrote}; {describe(exc)}"
    )


@check(
    13,
    "§3.4.1.4/§3.5.3",
    "start",
    1,
    "Under setting_sources=['project'] the CLI loads CLAUDE.md and runs the repo's own hook",
)
async def check_13(ctx: Ctx) -> Outcome:
    marker = ctx.hook_marks / "session-start-ran"
    result, exc, _, _ = await turn(
        ctx.agent("project"),
        ctx.chat("project-settings"),
        "What is the project codeword? Reply with the single word only.",
    )
    reply = str(getattr(result, "output", "")) or describe(exc)
    reflected = CODEWORD in reply.upper()
    hook_ran = marker.exists()
    warned = bool(ctx.warnings.matching("setting_sources"))
    good = reflected and hook_ran and warned
    return (PASS if good else FAIL), (
        f"construction WARNING logged={warned}; CLAUDE.md reflected via the CLI's own loading={reflected} "
        f"(reply: {reply[:120]!r}); the repository's own SessionStart hook ran on the host={hook_ran} "
        f"(marker {marker}); {describe(exc)}"
    )


@check(
    14, "§3.12", "start", 1, "No deferred tool-search placeholder hides BOS's tools; no claude.ai/Chrome tool appears"
)
async def check_14(ctx: Ctx) -> Outcome:
    """Carried items 3 and 4: under a real first-party login the CLI turns tool search on by
    default and would sync the account's claude.ai connectors and Claude-in-Chrome tools — BOS
    overrides all three off (§3.12) unconditionally. This is a model self-report, not a wire
    capture (a live run has no fake proxy to inspect the request payload the way CI does), so it is
    corroborating evidence, not proof — recorded as OBSERVED either way."""
    result, exc, _, _ = await turn(
        ctx.agent("mcp"),
        ctx.chat("tool-enum"),
        "List the literal names of every tool you currently have access to, one per line. Include any MCP "
        "tool whose name starts with mcp__, and include any tool related to deferred tool search, Chrome, or "
        "a connector if one is present. Do not omit any.",
    )
    if result is None:
        return NOT_ARRANGED, f"the enumeration turn did not complete — {describe(exc)}"
    text = str(result.output)
    lowered = text.lower()
    flagged = [w for w in ("deferredtoolplaceholder", "toolsearch", "chrome", "connector") if w in lowered]
    return OBSERVED, (
        "model's self-reported tool list (self-report, not a wire capture): "
        f"{text[:500]!r}; flagged substrings found={flagged or 'none'}; "
        f"{EXPOSED_TOOL} mentioned={EXPOSED_TOOL.lower() in lowered}"
    )


@check(15, "§3.10.3", "start", 1, "Whether the CLI sends the subscription token to a non-Anthropic ANTHROPIC_BASE_URL")
async def check_15(ctx: Ctx) -> Outcome:
    """Carried item 2. `ANTHROPIC_BASE_URL` is examined and *not* refused at construction (BEP 19
    §3.10.3): it changes where requests go but is not one of the CLI's own bypass-login checks. So
    it can be redirected to a local double while `auth = "subscription"` stays in force, and
    whatever header the CLI sends is exactly what would otherwise go to Anthropic. Only header
    *names* are ever reported — never values."""
    server = start_header_capture_server()
    prior = os.environ.get("ANTHROPIC_BASE_URL")
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
    try:
        _, exc, _, secs = await turn(ctx.agent("rw"), ctx.chat("base-url-probe"), "Reply with the word PING.")
        captured = list(_HeaderCapture.captured)
    finally:
        if prior is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = prior
        server.shutdown()
    if not captured:
        return NOT_ARRANGED, (
            f"no HTTP request reached the local probe server in {secs:.1f}s, so token exposure under a "
            f"redirected ANTHROPIC_BASE_URL was not observed this run. Turn ended: {describe(exc)}"
        )
    credential_headers = sorted({k for h in captured for k in h if k.lower() in ("authorization", "x-api-key")})
    return OBSERVED, (
        f"{len(captured)} request(s) reached the redirected ANTHROPIC_BASE_URL; header name(s) carrying a "
        f"credential: {credential_headers or 'NONE'} (values redacted, never printed); turn ended: {describe(exc)}"
    )


@check(
    16, "§3.10.3", "start", 0, "Whether an `ant` profile store at ~/.config/anthropic outranks the subscription login"
)
async def check_16(ctx: Ctx) -> Outcome:
    """Carried item 1. Deliberately source-only: settling precedence would mean creating or
    inspecting a real profile store, and this script refuses to touch `~/.config/anthropic` at
    all — a profile that already lives there is not this script's to read or disturb."""
    return NOT_ARRANGED, (
        "source-only (BEP 19 §3.10.3): determining precedence would mean creating a profile in, or reading, "
        "~/.config/anthropic, which this script never touches — a project security rule, not a missing "
        "feature. Read from the CLI source: a profile there can be used in place of the login, but which "
        "wins was not measured."
    )


@check(17, "§3.4.1.4/§8.1", "start", 0, "macOS-only: F_GETPATH containment for CLAUDE.md, and /usr/bin/sandbox-exec")
async def check_17(ctx: Ctx) -> Outcome:
    """Carried item 5: mark NOT ARRANGED on Linux."""
    if sys.platform != "darwin":
        return NOT_ARRANGED, f"this host is {sys.platform!r}, not macOS."
    return OBSERVED, (
        "sandbox-exec presence is covered by the bash-sandbox-dependency item above; the F_GETPATH "
        "symlink-swap race itself is already pinned by CI (test_the_file_actually_opened_is_checked_again) "
        "and is not independently re-verified live here."
    )


_MANAGED_MCP_PATHS: dict[str, Path] = {
    "darwin": Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
    "linux": Path("/etc/claude-code/managed-mcp.json"),
}


@check(18, "§3.8/§8.2", "start", 0, "An enterprise managed-mcp.json would make the CLI refuse --strict-mcp-config")
async def check_18(ctx: Ctx) -> Outcome:
    """Carried item 6. BOS does not guard this yet (§8.2) — it is read here only to say whether
    this host can even exercise it, never to work around it."""
    path = next((p for plat, p in _MANAGED_MCP_PATHS.items() if sys.platform.startswith(plat)), None)
    if path is None or not path.exists():
        return NOT_ARRANGED, (
            "no enterprise managed-mcp.json at the conventional path"
            f"{f' ({path})' if path else ' for this platform'} — the normal case outside a managed fleet."
        )
    return OBSERVED, (
        f"a readable {path} exists; BOS sends strict_mcp_config on every client and does not yet guard this "
        "(§8.2), so every Claude Code turn on this host is expected to be refused at CLI startup with "
        "'You cannot use --strict-mcp-config when an enterprise MCP config is present'."
    )


@check(
    19,
    "§7.24",
    "start",
    1,
    "Quota exhaustion and an expired login each surface as a distinct error, never an API-key fallback",
)
async def check_19(ctx: Ctx) -> Outcome:
    state = ctx.args.account_state
    if state not in ("exhausted", "expired"):
        return NOT_ARRANGED, (
            "could not arrange: this needs a Claude subscription that is already out of quota or logged out, "
            "and the script will not induce either. Re-run `start --account-state=exhausted` (or `=expired`) "
            "once the account is actually in that state; nothing else in this run is affected."
        )
    result, exc, _, _ = await turn(ctx.agent("rw"), ctx.chat(f"quota-{state}"), "Reply with the word PING.")
    if exc is None:
        return FAIL, (
            f"--account-state={state} was declared but the turn succeeded "
            f"({str(getattr(result, 'output', ''))[:80]!r}) — the account is not in that state, so nothing "
            "was measured"
        )
    text = str(exc).lower()
    billed = any(word in text for word in ("api key", "api_key", "anthropic_api_key", "falling back"))
    return (PASS if not billed else FAIL), (
        f"account-state={state} -> {describe(exc)}; no API-key fallback mentioned={not billed}"
    )


# ── Phase `resume` — a second process, so item 20 has a real restart ────────


@check(20, "§7.17", "resume", 1, "After a process restart, a third ask() resumes the session from ChatStore alone")
async def check_20(ctx: Ctx) -> Outcome:
    """This process never saw `start`'s client or in-memory state. The only thing crossing the
    boundary is the Claude Code session id in JsonlChatStore's metadata, which is exactly §7.17's
    claim."""
    expected = ctx.facts.get("session_id_from_start")
    before = await native_session_id_of(ctx, MAIN_CHAT)
    if before is None:
        return NOT_ARRANGED, f"no Claude Code session recorded for {MAIN_CHAT!r} — run the `start` phase first"
    result, exc, _, _ = await turn(
        ctx.agent("mcp"), MAIN_CHAT, "In one short sentence, what have we discussed in this thread so far?"
    )
    after = await native_session_id_of(ctx, MAIN_CHAT)
    if result is None:
        return FAIL, f"resume failed: {describe(exc)} (session on record was {before!r})"
    same = before == after and (expected is None or expected == after)
    return (PASS if same else FAIL), (
        f"recovered session {before!r} from the store in a fresh process and resumed it; after the turn "
        f"{after!r}; start recorded {expected!r}. Reply: {str(result.output)[:140]!r}"
    )


@check(21, "§7.23", "resume", 0, "get_messages returns the native transcript; source='bos' the two-message record")
async def check_21(ctx: Ctx) -> Outcome:
    """Reads item 20's turn; spends nothing. Claude Code's own §3.7 rules, unlike Codex's: no
    per-entry turn id (`native_turn_id` is always None) and no commentary/final-answer phase to
    drop."""
    try:
        native = await ctx.app.get_messages(MAIN_CHAT, source="native")
    except Exception as exc:  # noqa: BLE001 - the error is the observation
        return FAIL, f"source='native' raised: {describe(exc)}"
    bos = await ctx.app.get_messages(MAIN_CHAT, source="bos")
    roles = [str((m.llm_message or {}).get("role")) for m in native]
    turn_ids_native = [(m.metadata or {}).get("native_turn_id") for m in native]
    all_none = all(t is None for t in turn_ids_native)
    turns_committed = len({m.turn_id for m in bos if m.turn_id})
    consistent = len(bos) == 2 * turns_committed
    good = bool(native) and consistent and all_none and set(roles) <= {"user", "assistant"}
    return (PASS if good else FAIL), (
        f"native: {len(native)} message(s) {roles}, native_turn_id always None={all_none} (Claude Code has no "
        "per-entry turn id, §3.7); "
        f"bos: {len(bos)} message(s) over {turns_committed} turn(s) "
        f"({'two per turn as promised' if consistent else 'NOT two per turn'})"
    )


@check(22, "§7.22", "resume", 1, "Does a RESUMED chat still get the MCP tools?")
async def check_22(ctx: Ctx) -> Outcome:
    before = len(TOOL_CALLS)
    _, exc, cap, _ = await turn(
        ctx.agent("mcp"),
        MAIN_CHAT,
        f"Call the {EXPOSED_TOOL} tool with text='after resume', then reply with the tool's exact result.",
    )
    called = [name for name, _ in TOOL_CALLS[before:]]
    return (PASS if EXPOSED_TOOL in called else FAIL), (
        f"on a chat resumed from the store: host-side invocations {called or 'NONE'}; "
        f"tool events {[k for k in cap.kinds() if k.startswith('tool')] or 'none'}; {describe(exc)}"
    )


# ── Versions, banner, report ─────────────────────────────────────────────────


def sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("claude-agent-sdk")
    except PackageNotFoundError:
        return "not installed"


def cli_version() -> str:
    """The bundled `claude` binary the SDK actually spawns (`claude_agent_sdk/_bundled/claude`),
    asked the same way `_find_bundled_cli` resolves it, falling back to `PATH` — so the recorded
    version is the one that ran, the same intent as the sibling script's `cli_version`."""
    try:
        import claude_agent_sdk

        name = "claude.exe" if sys.platform == "win32" else "claude"
        binary = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        if not binary.exists():
            found = shutil.which("claude")
            if not found:
                return "claude CLI not found (bundled or on PATH)"
            binary = Path(found)
    except Exception as exc:  # noqa: BLE001
        return f"could not locate the CLI: {exc}"
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip() or "no version output"
    except Exception as exc:  # noqa: BLE001
        return f"could not ask the binary: {exc}"


def banner(phase: str, workspace: Path, outside: Path, bos_dir: Path, items: list[Item]) -> str:
    budget = sum(i.turns for i in items)
    from bos.extensions.runtimes.claude_code import _bash_sandbox_unavailable

    sandbox = _bash_sandbox_unavailable(sys.platform)
    lines = [
        "=" * 78,
        f"BEP 19 Layer 4b live validation — phase {phase!r} — {date.today().isoformat()}",
        "=" * 78,
        "",
        "THIS SPENDS YOUR CLAUDE SUBSCRIPTION QUOTA AND WRITES TO DISK."
        if budget
        else "This phase spends no quota — every turn in it is expected to fail before a model is reached.",
        "",
        f"  model turns budgeted for this phase : ~{budget} (plus retries; the real cost can exceed this)",
        f"  claude-agent-sdk                    : {sdk_version()}",
        f"  claude CLI                          : {cli_version()}",
        "  bash sandbox on this host            : "
        + ("present — workspace-write should enforce it" if sandbox is None else f"ABSENT: {sandbox}"),
        "",
        "  Every path this phase may write to:",
        f"    workspace / agent cwd             : {workspace}",
        "      seeded by this script           : NOTES.md, CLAUDE.md, .claude/settings.json",
        "      written by the agent            : inside-write.txt, bash-in.txt, stop-marker/, "
        "sysprompt-ok.txt, and whatever else a turn decides to create",
        f"    BOS state (chat store, JSONL)     : {bos_dir}",
        *(
            [
                f"    OUTSIDE the workspace, on purpose : {outside}",
                "      items 3, 4, 5 and 13 ask an agent to write there, or seed a hook marker there; the "
                "hook and the sandbox are supposed to refuse every write. A fresh directory this script "
                "created under your home — removed when the phase ends.",
                "    /tmp, on purpose                  : /tmp/bos-claude-code-tmp-probe-*/bash-tmp.txt",
                "      item 4 records whether a workspace-write agent's Bash can write there; removed afterwards.",
            ]
            if any(i.n in _NEEDS_OUTSIDE for i in items)
            else []
        ),
        "",
        f"  Items in this phase, in execution order: {', '.join(str(i.n) for i in items)}",
        "",
    ]
    return "\n".join(lines)


def render(items: list[Item], phases: list[str]) -> str:
    """The paste-ready block. Its job is to become BEP §8.1's readiness table and a §9 revision
    entry with light editing, carrying the date, the SDK version and every NOT ARRANGED line
    intact."""
    done = sorted((i for i in items if i.mark), key=lambda i: i.n)
    counts = {m: sum(1 for i in done if i.mark == m) for m in MARKS}
    out = [
        "",
        "=" * 78,
        "PASTE-READY RESULTS",
        "=" * 78,
        "",
        f"Run on {date.today().isoformat()} — claude-agent-sdk {sdk_version()}, claude CLI {cli_version()}",
        f"Phases: {', '.join(phases)}",
        "Tally: " + ", ".join(f"{m} {counts[m]}" for m in MARKS if counts[m]),
        "",
        "--- for BEP §8.1 (readiness by track) ---",
        "",
        "| # | BEP | What was checked | Result | Observed |",
        "|---|---|---|---|---|",
    ]
    for item in done:
        note = item.note.replace("|", "\\|").replace("\n", " ")
        out.append(f"| {item.n} | {item.criterion} | {item.title} | **{item.mark}** | {note} |")
    unarranged = [i for i in done if i.mark == NOT_ARRANGED]
    out += [
        "",
        "--- for BEP §9 (revision history) ---",
        "",
        f"- {date.today().isoformat()} — Live validation of Layer 4b Claude Code against a real Claude "
        f"subscription (claude-agent-sdk {sdk_version()}, claude CLI {cli_version()}), phases "
        f"{'+'.join(phases)}, via `scripts/validate_claude_code_runtime.py`. "
        + " ".join(f"§7 criterion / item {i.n} ({i.criterion}): {i.mark.lower()} — {i.note}" for i in done)
        + (
            f" Still unverified, and recorded as such: items {', '.join(str(i.n) for i in unarranged)} — each "
            "could not be arranged, not merely not observed."
            if unarranged
            else " Nothing in this run was left unarranged."
        ),
        "",
    ]
    if unarranged:
        out += ["--- NOT ARRANGED (do not write these up as passes) ---", ""]
        out += [f"  item {i.n} ({i.criterion}): {i.note}" for i in unarranged]
        out.append("")
    return "\n".join(out)


# ── Drivers ──────────────────────────────────────────────────────────────────


def items_for(phase: str) -> list[Item]:
    return [i for i in CHECKS if i.phase == phase]


def read_state(bos_dir: Path) -> dict[str, Any]:
    path = bos_dir / STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(bos_dir: Path, state: dict[str, Any]) -> None:
    bos_dir.mkdir(parents=True, exist_ok=True)
    (bos_dir / STATE_FILE).write_text(json.dumps(state, indent=2, default=str))


def seed_workspace(workspace: Path, hook_marks: Path | None) -> dict[str, bool]:
    """Seed the files items 1, 12 and 13 read back. Returns which of them were ours to write — an
    existing CLAUDE.md or .claude/settings.json is never overwritten, mirroring the sibling
    script's care around AGENTS.md."""
    workspace.mkdir(parents=True, exist_ok=True)
    notes = workspace / "NOTES.md"
    if not notes.exists():
        notes.write_text("This workspace exists only to validate the BOS Claude Code runtime.\nSecond line.\n")
    seeded = {"claude_md": False, "claude_dir": False}
    claude_md = workspace / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            f"# Project instructions\n\nWhen anyone asks for the project codeword, answer exactly {CODEWORD}.\n"
        )
        seeded["claude_md"] = True
    claude_dir = workspace / ".claude"
    if not claude_dir.exists() and hook_marks is not None:
        claude_dir.mkdir()
        marker_cmd = f"touch {hook_marks / 'session-start-ran'}"
        hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": marker_cmd}]}]}
        settings = {"hooks": hooks, "permissions": {"allow": ["Write"]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        seeded["claude_dir"] = True
    return seeded


async def run_phase(phase: str, args: argparse.Namespace) -> list[Item]:
    workspace = Path(args.workspace).expanduser().resolve()
    bos_dir = workspace / ".bos"
    state = read_state(bos_dir)
    items = items_for(phase)
    needs_outside = any(i.n in _NEEDS_OUTSIDE for i in items)
    if needs_outside:
        OUTSIDE_PARENT.mkdir(parents=True, exist_ok=True)
        outside = Path(tempfile.mkdtemp(prefix="outside-", dir=OUTSIDE_PARENT))
        hook_marks = outside / "hookmarks"
        hook_marks.mkdir()
    else:
        outside = Path("<not used by this phase>")
        hook_marks = Path("<not used by this phase>")

    print(banner(phase, workspace, outside, bos_dir, items))
    if not args.yes:
        print("Refusing to run without --yes. Re-read the paths above first.")
        remove_outside(outside, needs_outside)
        return []
    try:
        return await _run_items(phase, args, workspace, bos_dir, state, items, outside, hook_marks)
    finally:
        remove_outside(outside, needs_outside)


def remove_outside(outside: Path, created: bool) -> None:
    if not created:
        return
    shutil.rmtree(outside, ignore_errors=True)
    try:
        OUTSIDE_PARENT.rmdir()  # only if nothing else is left in it
    except OSError:
        pass


async def _run_items(
    phase: str,
    args: argparse.Namespace,
    workspace: Path,
    bos_dir: Path,
    state: dict[str, Any],
    items: list[Item],
    outside: Path,
    hook_marks: Path,
) -> list[Item]:
    seed_workspace(workspace, hook_marks if phase == "start" else None)
    agents = AGENTS_RESUME if phase != "start" else AGENTS_START
    ws = Workspace(workspace=workspace, bos_dir=bos_dir, config=workspace_config(agents))

    warnings = WarningLog()
    runtime_log = logging.getLogger("bos.extensions.runtimes")
    runtime_log.addHandler(warnings)

    async with BosApp(ws) as app:
        ctx = Ctx(app=app, workspace=workspace, outside=outside, hook_marks=hook_marks, warnings=warnings, args=args)
        ctx.facts["session_id_from_start"] = state.get("session_id")
        for item in items:
            print(f"\n-- item {item.n} ({item.criterion}): {item.title}")
            try:
                item.mark, item.note = await item.fn(ctx)
            except Exception as exc:  # noqa: BLE001 - one broken check must not lose the rest
                item.mark, item.note = ERROR, f"the check itself raised — {describe(exc)}"
            print(f"   {item.mark}: {item.note}")

    runtime_log.removeHandler(warnings)

    state.update({
        "workspace": str(workspace),
        "outside": str(outside),
        "session_id": ctx.facts.get("session_id") or state.get("session_id"),
        "sdk_version": sdk_version(),
        f"phase_{phase}": {
            "date": date.today().isoformat(),
            "items": [
                {"n": i.n, "criterion": i.criterion, "title": i.title, "mark": i.mark, "note": i.note} for i in items
            ],
        },
    })
    write_state(bos_dir, state)
    return items


def restore_items(state: dict[str, Any], phase: str) -> list[Item]:
    """Rebuild an earlier phase's recorded items so the final table is whole. Only what that phase
    actually printed — never a mark this process invented."""
    recorded = (state.get(f"phase_{phase}") or {}).get("items", [])
    return [
        Item(
            n=raw["n"],
            criterion=raw["criterion"],
            phase=phase,
            turns=0,
            title=raw["title"],
            fn=_unrunnable,
            mark=raw["mark"],
            note=raw["note"],
        )
        for raw in recorded
    ]


async def _unrunnable(ctx: Ctx) -> Outcome:
    raise AssertionError("a restored item from an earlier phase is a record, not a check to re-run")


# ── The `auth-preflight` phase — item 23, offline, zero quota ───────────────

_FAKE_API_KEY = "sk-ant-validate-placeholder-not-a-real-key"


async def run_auth_preflight_phase(workspace: Path) -> Item:
    """BEP 19 §3.10.3: under the default `auth = "subscription"`, `ClaudeCodeAgent.__init__`
    refuses at construction when an inherited credential variable (`ANTHROPIC_API_KEY` among them)
    would take the run off the subscription login — synchronously, before any CLI is spawned and
    before any network call. Proving that needs no login and spends nothing: this plants an
    obviously-fake key in this process's own environment, tries to build the agent, and expects
    the refusal. The key is never a real credential and is never printed."""
    workspace.mkdir(parents=True, exist_ok=True)
    ws = Workspace(
        workspace=workspace,
        bos_dir=workspace / ".bos",
        config=workspace_config({"probe": {"_parent": "claude-code", "permission": "read-only"}}),
    )
    prior = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = _FAKE_API_KEY
    try:
        async with BosApp(ws):
            pass
    except ValueError as exc:
        text = str(exc)
        good = "ANTHROPIC_API_KEY" in text and 'auth = "api_key"' in text
        mark, note = (
            (PASS if good else FAIL),
            (f"construction raised ValueError naming the variable and the escape hatch={good}. {describe(exc)}"),
        )
    except Exception as exc:  # noqa: BLE001 - the wrong exception type is itself the finding
        mark, note = FAIL, f"construction raised the wrong exception type — {describe(exc)}"
    else:
        mark, note = FAIL, "construction succeeded with ANTHROPIC_API_KEY set — the refusal did not fire"
    finally:
        if prior is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = prior
    return Item(
        n=23,
        criterion="§3.10.3",
        phase="auth-preflight",
        turns=0,
        title='Construction refuses auth="subscription" when an API key is present in the environment',
        fn=_unrunnable,
        mark=mark,
        note=note,
    )


def auth_preflight_banner(workspace: Path) -> str:
    return "\n".join([
        "=" * 78,
        f"BEP 19 Layer 4b live validation — phase 'auth-preflight' — {date.today().isoformat()}",
        "=" * 78,
        "",
        "This phase spends NO quota and needs NO login: it sets a throwaway, obviously-fake",
        'ANTHROPIC_API_KEY ("sk-ant-validate-placeholder-not-a-real-key") in this process\'s own',
        "environment — never a real credential, never printed — and confirms BOS refuses",
        '`auth = "subscription"` at construction, before any CLI is ever spawned.',
        "",
        f"  claude-agent-sdk    : {sdk_version()}",
        f"  claude CLI          : {cli_version()}",
        f"  throwaway workspace : {workspace}",
        "",
    ])


# ── The offline self-check ───────────────────────────────────────────────────


async def _check_configs_build() -> None:
    """Open a real ``BosApp`` over ``AGENTS_START`` and assert what every agent resolved to.
    Construction is lazy (BEP 19 §3.1) — no CLI child, no login — so this costs nothing and still
    catches a bad ``_parent``, a rejected ``system_prompt``/``setting_sources`` combination, a
    permission BOS refuses, or an ``mcp_tools`` name the host has no ``ep_tool`` for."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ws"
        root.mkdir()
        ws = Workspace(workspace=root, bos_dir=root / ".bos", config=workspace_config(AGENTS_START))
        async with BosApp(ws) as app:
            for kind, wanted in AGENTS_START.items():
                cfg = dict(claude_code_agent(app, kind).resolved_config)
                assert cfg["external_runtime"] == "claude-code", f"{kind} did not dispatch to the Claude Code runtime"
                assert cfg["permission"] == wanted["permission"], kind
                assert cfg["cwd"] == str(root), f"{kind} cwd resolved to {cfg['cwd']}, not the workspace"
                assert cfg["auth"] == "subscription", f"{kind} must use the login, not an API key"
                assert not cfg["mcp_tools_unavailable"], (
                    f"{kind} asks for {cfg['mcp_tools_unavailable']}, which no ep_tool in this script registers"
                )
                assert claude_code_agent(app, kind)._in_flight == {}, (
                    f"building {kind} started a turn (§3.1 forbids it)"
                )
            assert claude_code_agent(app, "mcp").resolved_config["mcp_tools"] == [EXPOSED_TOOL]
            assert WITHHELD_TOOL not in claude_code_agent(app, "mcp").resolved_config["mcp_tools"], (
                "the withheld tool must never be granted, or item 10 proves nothing"
            )
            assert claude_code_agent(app, "project").resolved_config["setting_sources"] == ["project"]
            assert claude_code_agent(app, "sysprompt").resolved_config["system_prompt"] == SYSPROMPT_INSTRUCTION
            assert type(app.harness.chat_store).__name__ == "JsonlChatStore", (
                "item 20 needs a store that outlives the process"
            )


def self_check() -> int:
    """Everything here that does not need the network, asserted. Nobody can run this script end to
    end without a Claude login, so its correctness has to come from somewhere: the registry's
    shape, the report renderer, the banner, and the gate-tap wiring items 5 and 11 read. Deliberately
    NOT a pytest file — this script must stay uncollectable."""
    numbers = sorted(i.n for i in CHECKS) + [23]
    assert sorted(numbers) == list(range(1, 24)), f"checklist 1-23 must each appear once, got {sorted(numbers)}"
    assert {i.phase for i in CHECKS} == {"start", "resume"}, "unknown phase on some item (23 is handled separately)"
    for item in CHECKS:
        assert item.criterion.startswith("§"), f"item {item.n} has no BEP reference"
        assert item.turns >= 0 and item.title, f"item {item.n} is missing metadata"
    assert sum(i.turns for i in CHECKS) > 0, "the turn budget must not be zero"

    # The renderer is what becomes the BEP text, so exercise every mark through it.
    sample = [
        Item(n=1, criterion="§7.15", phase="start", turns=1, title="t", fn=_unrunnable, mark=PASS, note="ok"),
        Item(
            n=19,
            criterion="§7.24",
            phase="start",
            turns=1,
            title="t",
            fn=_unrunnable,
            mark=NOT_ARRANGED,
            note="could not arrange: no exhausted account",
        ),
        Item(
            n=14, criterion="§3.12", phase="start", turns=1, title="t|piped", fn=_unrunnable, mark=OBSERVED, note="a|b"
        ),
    ]
    text = render(sample, ["start"])
    assert "NOT ARRANGED" in text and "could not arrange" in text, "unarranged items must survive into the report"
    assert "\\|" in text, "a pipe in a note must be escaped or it breaks the markdown table"
    assert text.count("\n|") >= 5, "the readiness table lost rows"
    assert "§9" in text and date.today().isoformat() in text, "the revision entry needs a date"
    assert "**PASS**" in text and "**OBSERVED**" in text

    clean = render([sample[0]], ["start"])
    assert "Nothing in this run was left unarranged." in clean
    assert "NOT ARRANGED" not in clean

    text = banner("start", Path("/ws"), Path("/outside"), Path("/ws/.bos"), items_for("start"))
    for needle in ("/ws", "/outside", "/ws/.bos", "QUOTA", "model turns budgeted"):
        assert needle in text, f"the up-front banner never mentions {needle!r}"

    for kind, cfg in AGENTS_START.items():
        assert cfg["_parent"] == "claude-code", f"{kind} must inherit the reserved kind"
        assert cfg["permission"] in ("read-only", "workspace-write", "full-access"), kind
        assert "external_runtime" not in cfg, "BOS writes external_runtime; config must not"
    assert len(AGENTS_RESUME) == 1, "item 21 needs exactly one claude-code agent for get_messages to route"
    assert {i.n for i in items_for("resume")} == {20, 21, 22}
    assert {i.n for i in items_for("start")} == set(range(1, 20))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ws"
        marks = Path(tmp) / "marks"
        marks.mkdir()
        seeded = seed_workspace(root, marks)
        assert seeded == {"claude_md": True, "claude_dir": True}
        assert CODEWORD in (root / "CLAUDE.md").read_text()
        assert json.loads((root / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"]
        assert seed_workspace(root, marks) == {"claude_md": False, "claude_dir": False}, (
            "an existing CLAUDE.md or .claude/settings.json must never be overwritten"
        )
        write_state(root / ".bos", {"session_id": "sess_1"})
        assert read_state(root / ".bos")["session_id"] == "sess_1"
        assert read_state(root / "nope") == {}
        assert appeared(root / "never.txt", 0.3) is None
        (root / "there.txt").write_text("x")
        assert appeared(root / "there.txt", 1.0) is not None
        restored = restore_items(
            {"phase_start": {"items": [{"n": 1, "criterion": "§7.15", "title": "t", "mark": PASS, "note": "n"}]}},
            "start",
        )
        assert [i.mark for i in restored] == [PASS]
        assert restore_items({}, "start") == []

    # `GateTap` must not change the hook's or `can_use_tool`'s decisions, only record them — checked
    # against the real classes' shape rather than by driving a CLI, which self-check must not do.
    from claude_agent_sdk import PermissionResultAllow

    class _FakeAgent:
        def _hook(self) -> Callable[..., Awaitable[Any]]:
            async def hook(input_data: Any, tool_use_id: Any, context: Any) -> Any:
                return (
                    {}
                    if input_data.get("tool_name") == "Read"
                    else {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no"}}
                )

            return hook

        def _can_use_tool(self) -> Callable[..., Awaitable[Any]]:
            async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
                return PermissionResultAllow()

            return can_use_tool

    fake = _FakeAgent()
    tap = install_gate_tap(fake)
    assert install_gate_tap(fake) is tap, "install_gate_tap must be idempotent per agent"
    hook = fake._hook()
    assert asyncio.run(hook({"tool_name": "Read"}, "id", None)) == {}
    assert tap.hook_calls == [("Read", False)]
    denied = asyncio.run(hook({"tool_name": "Write"}, "id", None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert tap.hook_calls[-1] == ("Write", True)
    can_use_tool = fake._can_use_tool()
    assert type(asyncio.run(can_use_tool("Read", {}, None))).__name__ == "PermissionResultAllow"
    assert tap.can_use_tool_calls == [("Read", True)]

    # The one thing worth more than all of the above: that the config in this file really does
    # build every agent through a real BosApp.
    asyncio.run(_check_configs_build())

    assert describe(None) == "no error"
    assert describe(ValueError("boom")).startswith("ValueError: boom")

    cap = Capture()
    assert cap.max_gap() == 0.0 and cap.kinds() == []
    event = TurnEvent(event_type="response", phase="finish", chat_id="c", turn_id="t", agent_name="a", content="hi")
    asyncio.run(cap.emit(event))
    assert cap.kinds() == ["response/finish"]

    # Item 15 must never leak a header's value into the report — only its name.
    server = start_header_capture_server()
    try:
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{server.server_port}/"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer sekrit"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError:
            pass
        assert _HeaderCapture.captured and _HeaderCapture.captured[0].get("Authorization") == "Bearer sekrit"
    finally:
        server.shutdown()

    print(f"self-check OK — {len(CHECKS) + 1} items, ~{sum(i.turns for i in CHECKS)} model turns across all phases")
    print(f"claude-agent-sdk {sdk_version()}; claude CLI {cli_version()}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="validate_claude_code_runtime.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("phase", choices=("start", "resume", "auth-preflight", "plan", "self-check"))
    p.add_argument("workspace", nargs="?", default=None, help="agent cwd; defaults to a fresh temp directory")
    p.add_argument("--yes", action="store_true", help="required: you have read the paths and accept the quota cost")
    p.add_argument(
        "--account-state",
        choices=("normal", "exhausted", "expired"),
        default="normal",
        help="item 19 only: declare that the logged-in account is already in that state",
    )
    p.add_argument(
        "--repo-trusted",
        action="store_true",
        help="item 5 only: you have already trusted the workspace yourself via an interactive `claude` run",
    )
    p.add_argument("--stop-after", type=float, default=8.0, help="item 7: seconds before request_stop()")
    p.add_argument("--settle", type=float, default=15.0, help="seconds to watch for writes after a turn ends")
    return p


def resolve_workspace(args: argparse.Namespace) -> None:
    """`start` picks the workspace; `resume` inherits it, because item 20 recovers a session id
    from that directory's chat store and a different one would silently start a fresh session
    instead."""
    if args.phase == "plan":
        args.workspace = args.workspace or str(Path(tempfile.gettempdir()) / "<a fresh bos-claude-code-ws-* directory>")
        return
    if args.phase == "start":
        args.workspace = args.workspace or tempfile.mkdtemp(prefix="bos-claude-code-ws-")
        return
    if args.phase == "auth-preflight":
        # Needs no earlier run and touches no real login either way; its own prefix so a later bare
        # `resume` (which globs bos-claude-code-ws-*) never picks this one up instead.
        args.workspace = args.workspace or tempfile.mkdtemp(prefix="bos-claude-code-authpreflight-ws-")
        return
    if args.workspace:
        return
    for candidate in sorted(Path(tempfile.gettempdir()).glob("bos-claude-code-ws-*"), key=lambda p: -p.stat().st_mtime):
        if (candidate / ".bos" / STATE_FILE).exists():
            args.workspace = str(candidate)
            return
    raise SystemExit(
        f"No workspace given and no earlier run found. Pass the same directory the `start` phase printed, "
        f"which also holds .bos/{STATE_FILE}."
    )


def guard_repo(workspace: Path) -> None:
    """One cheap refusal. A turn in here writes files and, for items 3-5, tries to write outside —
    none of which belongs in a source checkout."""
    if (workspace / "pyproject.toml").exists() and (workspace / "src" / "bos").is_dir():
        raise SystemExit(f"{workspace} looks like the bos-ai checkout. Point this at a scratch directory instead.")


async def amain(args: argparse.Namespace) -> int:
    items = await run_phase(args.phase, args)
    if not items:
        return 1

    phases = [args.phase]
    if args.phase == "resume":
        state = read_state(Path(args.workspace) / ".bos")
        earlier = restore_items(state, "start")
        if earlier:
            items = earlier + items
            phases = ["start", "resume"]
        else:
            print("\nNOTE: no recorded `start` phase found, so the table below covers `resume` only.")
    print(render(items, phases))
    print(
        "Next: paste the table into BEP 19 §8.1 and the bullet into §9. Leave every NOT ARRANGED line as it is."
        if args.phase != "start"
        else f"\nNext, in a FRESH process (item 20 needs a real restart):\n\n"
        f"    uv run python scripts/validate_claude_code_runtime.py resume --yes {args.workspace}\n\n"
        f"That phase prints the combined, paste-ready table for both phases."
    )
    return 0


async def amain_auth_preflight(args: argparse.Namespace, workspace: Path) -> int:
    print(auth_preflight_banner(workspace))
    if not args.yes:
        print("Refusing to run without --yes. Re-read the paths above first.")
        return 1
    item = await run_auth_preflight_phase(workspace)
    print(f"\n-- item {item.n} ({item.criterion}): {item.title}")
    print(f"   {item.mark}: {item.note}")
    print(render([item], ["auth-preflight"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.phase == "self-check":
        return self_check()
    resolve_workspace(args)
    workspace = Path(args.workspace).expanduser().resolve()
    guard_repo(workspace)
    if args.phase == "plan":
        outside = OUTSIDE_PARENT / "<a fresh outside-* directory>"
        for phase in ("start", "resume"):
            print(banner(phase, workspace, outside, workspace / ".bos", items_for(phase)))
        placeholder = Path(tempfile.gettempdir()) / "<a fresh bos-claude-code-authpreflight-ws-* directory>"
        print(auth_preflight_banner(placeholder))
        return 0
    if args.phase == "auth-preflight":
        return asyncio.run(amain_auth_preflight(args, workspace))
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
