#!/usr/bin/env python
"""Live validation of BEP 19 Layer 4 (``CodexAgent``) against a real ChatGPT login.

**This is not a test and must never run in CI or be imported from a test path.**
It signs in as whoever owns the machine, spends that person's ChatGPT
subscription quota on roughly twenty real model turns, and writes files both
inside and — deliberately, as part of item 4 — outside the agent's working
directory. A green CI run must never cost a human money or touch their account,
so the only thing that starts this script is a person typing its name. It lives
under ``scripts/`` (no ``__init__.py``, not on ``sys.path``, no ``test_``
prefix, no test function in it) so nothing collects it by accident.

It exists because BEP §7 criteria 15-26 ask questions no fake can answer:
whether a real ``codex app-server`` accepts BOS's refusal values, whether a
real sandbox actually denies a write, whether the vendor reports
``interrupted`` for an interrupt BOS asked for. Tasks 1-10 proved everything
that can be proved without a login; this collects the rest.

Run it in two phases, because item 3 asks what survives a process restart::

    uv run python scripts/validate_codex_runtime.py start --yes [WORKSPACE]
    uv run python scripts/validate_codex_runtime.py resume --yes     # prints the full table
    uv run python scripts/validate_codex_runtime.py nologin --yes    # item 12, no quota spent

``start`` prints the exact ``resume`` command to run next. ``resume`` merges
``start``'s recorded results with its own and prints the paste-ready output.

Two more modes cost nothing and need no network::

    uv run python scripts/validate_codex_runtime.py self-check   # asserts the script's own wiring
    uv run python scripts/validate_codex_runtime.py plan --yes   # the banner and item list only

**Nothing here ever invents a result.** An item the script could not arrange
prints ``NOT ARRANGED`` with the reason — "no exhausted account", "AGENTS.md
already exists in the workspace" — and stays out of the pass column. A line
saying what could not be set up is worth more than a pass that was assumed.
Items 10 and 12 need the account in a particular state and are ``NOT ARRANGED``
by default; see ``--account-state``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from bos.sdk import BosApp, TurnEvent, Workspace, ep_tool

# ── The two host tools items 8, 18, 20 and 22 need ──────────────────────────
#
# Registered at import, in the process-global `ep_tool` registry, because
# `BosApp.__aenter__` -> `bootstrap_platform()` has to see them before any
# agent asks for an MCP server. `EXPOSED` is the one `mcp_tools` grants;
# `WITHHELD` is registered and deliberately never granted, which is the half of
# §7.22 that says an unexposed tool stays uncallable.

EXPOSED_TOOL = "ValidateEcho"
WITHHELD_TOOL = "ValidateSecret"

TOOL_CALLS: list[tuple[str, str]] = []
"""Every host-tool invocation the child actually reached, in order.

Host-side state, mutated over MCP from inside the sandbox — which is exactly
what item 20 measures under `read-only`: the sandbox confines the filesystem,
not BOS's own tool surface.
"""

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
    """Register one checklist item. *turns* is the model turns it costs, and is
    what the up-front budget in the banner is summed from — so an item that
    grows a turn has to say so here."""

    def deco(fn: Callable[[Ctx], Awaitable[Outcome]]) -> Callable[[Ctx], Awaitable[Outcome]]:
        CHECKS.append(Item(n=n, criterion=criterion, phase=phase, turns=turns, title=title, fn=fn))
        return fn

    return deco


# ── Agent kinds ─────────────────────────────────────────────────────────────
#
# One `[agents.<kind>]` per distinct runtime configuration, since a built agent
# cannot be reconfigured (BosApp.build_agent refuses it, and rightly). Each
# inherits `_parent = "codex"`, which is BEP 19 §3.4's recommended shape and the
# one that proves dispatch works off `external_runtime`, not off the name.
#
# `resume` declares exactly ONE codex agent on purpose: `BosApp.get_messages(
# source="native")` matches agents by `resolved_config["external_runtime"]` and
# raises when two declare the same runtime, so item 9 can only be observed
# through BosApp in a phase with a single one.

AGENTS_START: dict[str, dict[str, Any]] = {
    "mcp": {"_parent": "codex", "permission": "workspace-write", "mcp_tools": [EXPOSED_TOOL]},
    "rw": {"_parent": "codex", "permission": "workspace-write"},
    # Item 5 gets an agent of its own because `request_stop()` is one-way:
    # `_stop_requested` is an asyncio.Event that is set and never cleared, so
    # every later turn on the same instance would end instantly. Sharing `rw`
    # here silently broke items 7, 10 and 13.
    "stop": {"_parent": "codex", "permission": "workspace-write"},
    "ro": {"_parent": "codex", "permission": "read-only", "mcp_tools": [EXPOSED_TOOL]},
    "slow": {"_parent": "codex", "permission": "workspace-write", "timeout_seconds": 25.0},
    "tiny": {"_parent": "codex", "permission": "workspace-write", "timeout_seconds": 2.0},
    "native": {
        "_parent": "codex",
        "permission": "workspace-write",
        "native_options": {"personality": "concise", "config": {"project_doc_max_bytes": 0}},
    },
    "typo": {
        "_parent": "codex",
        "permission": "read-only",
        "native_options": {"definitely_not_a_codex_keyword": 1},
    },
}

AGENTS_RESUME: dict[str, dict[str, Any]] = {
    "mcp": {"_parent": "codex", "permission": "workspace-write", "mcp_tools": [EXPOSED_TOOL]},
}

MAIN_CHAT = "validate-main"
CODEWORD = "PINEAPPLE"
STATE_FILE = "validate-state.json"


def workspace_config(agents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`platform.extensions = []` rather than the default `["bos.exts", ...]`:
    nothing here needs a provider, a channel or a plugin, and the chat store and
    mail route below are `bos.core.defaults` built-ins that register on import.
    JsonlChatStore is not a choice of convenience — item 3 recovers a Codex
    thread id from it *after this process is gone*, which an in-memory store
    cannot offer."""
    return {"platform": {"extensions": []}, "harness": {"chat_store": "JsonlChatStore"}, "agents": agents}


# ── Context and turn plumbing ───────────────────────────────────────────────


class Capture:
    """A ``TurnEventSink`` that also times the gaps between events.

    The timing is item 19's whole question: a refusal handled on the vendor's
    single stdout reader thread would show up as a stall between one
    notification and the next, not as an error.
    """

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
    """Captures the runtime's own WARNING lines — ``_deny_approval``'s refusal
    line (item 19) and ``aclose``'s drain line, which are otherwise invisible to
    a caller."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle.lower() in line.lower()]


def codex_agent(app: Any, kind: str) -> Any:
    """One built agent, typed ``Any`` on purpose.

    ``BosApp.agent`` is declared to return ``AgentPort``, which promises
    ``name``/``request_stop``/``ask``/``run`` and nothing else — while
    ``resolved_config``, ``native_messages`` and ``aclose`` are real parts of
    what this script observes, reached duck-typed exactly as ``BosApp`` itself
    reaches them. Widening the port to keep a type checker happy would change
    BOS's contract to suit a script, which is the wrong way round.
    """
    return app.agent(kind)


@dataclass
class Ctx:
    app: Any
    workspace: Path
    outside: Path
    warnings: WarningLog
    args: argparse.Namespace
    chat_seq: int = 0
    facts: dict[str, Any] = field(default_factory=dict)

    def agent(self, kind: str) -> Any:
        return codex_agent(self.app, kind)

    def chat(self, label: str) -> str:
        self.chat_seq += 1
        return f"validate-{label}-{self.chat_seq}"

    @property
    def store(self) -> Any:
        return self.app.harness.chat_store


async def turn(
    agent: Any, chat_id: str, prompt: str, **kwargs: Any
) -> tuple[Any | None, BaseException | None, Capture, float]:
    """One turn, never raising. Returns ``(result, error, capture, seconds)``.

    Swallowing the exception is the point: half this checklist is about *which*
    error a live server produces, so an error is data here, not a failure.
    """
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


async def thread_id_of(ctx: Ctx, chat_id: str) -> str | None:
    """The Codex thread id BOS recorded for this chat — the same metadata
    ``_shared.read_native_session_id`` reads, and the only thing item 2 and
    item 3 are allowed to assert on. The model's reply text proves nothing
    about session continuity."""
    for message in reversed(await ctx.store.get_messages(chat_id, active_only=False)):
        if (message.metadata or {}).get("external_runtime") == "codex":
            return (message.metadata or {}).get("native_session_id")
    return None


def appeared(path: Path, within: float) -> float | None:
    """Seconds until *path* exists, or None. Used to watch for work the child
    went on doing after BOS gave up on it (item 11) or was told to stop (5)."""
    deadline = time.monotonic() + within
    started = time.monotonic()
    while time.monotonic() < deadline:
        if path.exists():
            return time.monotonic() - started
        time.sleep(0.25)
    return None


def child_pid(agent: Any) -> int | None:
    """The ``codex app-server`` pid, down the vendor's private attribute chain.

    Verified against openai-codex 0.156.1: ``AsyncCodex._client`` is an
    ``AsyncCodexClient``, whose ``_sync`` is the ``CodexClient`` that owns
    ``_proc`` (``None`` until the child is spawned). Private, unstable, and the
    only way to ask — so it is read defensively and item 17 reports "could not
    read the pid" rather than a pass if the chain ever moves.
    """
    try:
        proc = agent._client._client._sync._proc
    except AttributeError:
        return None
    return getattr(proc, "pid", None)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── Phase `start` ───────────────────────────────────────────────────────────


@check(1, "§7.15", "start", 1, "A turn starts, streams events, returns non-empty usage")
async def check_01(ctx: Ctx) -> Outcome:
    result, exc, cap, secs = await turn(
        ctx.agent("mcp"), MAIN_CHAT, "Read NOTES.md in your working directory and reply with its first line only."
    )
    # Narrowed on `result`, not on `exc`: `turn()` returns exactly one of the
    # two, and this is the spelling a type checker can follow.
    if result is None:
        return FAIL, f"the first turn did not complete — {describe(exc)}"
    ctx.facts["first_events"] = cap.kinds()
    ctx.facts["first_usage"] = dict(result.usage or {})
    ctx.facts["first_reply"] = str(result.output)[:200]
    mark = PASS if result.usage else FAIL
    return mark, (
        f"{secs:.1f}s, finish_reason={result.finish_reason!r}, {len(cap.events)} event(s), "
        f"usage={result.usage or '{} (EMPTY — §7.15 not met)'}"
    )


@check(2, "§7.16", "start", 1, "A second ask() on the same chat_id continues the same thread")
async def check_02(ctx: Ctx) -> Outcome:
    before = await thread_id_of(ctx, MAIN_CHAT)
    _, exc, _, _ = await turn(ctx.agent("mcp"), MAIN_CHAT, "Reply with the single word CONTINUED.")
    after = await thread_id_of(ctx, MAIN_CHAT)
    ctx.facts["thread_id"] = after
    if exc is not None:
        return FAIL, f"the second turn did not complete — {describe(exc)}"
    if before and after and before == after:
        return PASS, f"same Codex thread across both turns: {after!r} (asserted on the id, not the reply)"
    return FAIL, f"thread id changed: {before!r} -> {after!r}"


@check(4, "§7.18", "start", 3, "workspace-write allows a write in cwd and denies one outside; read-only denies both")
async def check_04(ctx: Ctx) -> Outcome:
    inside = ctx.workspace / "inside-write.txt"
    outside = ctx.outside / "outside-write.txt"
    rw = ctx.agent("rw")

    _, exc_in, _, _ = await turn(
        rw, ctx.chat("sandbox-in"), f"Create a file named {inside.name} in your working directory containing OK."
    )
    _, exc_out, _, _ = await turn(
        rw,
        ctx.chat("sandbox-out"),
        f"Create a file at the absolute path {outside} containing OK. "
        "If you cannot, say exactly DENIED and the reason.",
    )
    _, exc_ro, _, _ = await turn(
        ctx.agent("ro"),
        ctx.chat("sandbox-ro"),
        f"Create a file named readonly-write.txt in {ctx.workspace} containing OK. "
        "If you cannot, say exactly DENIED and the reason.",
    )
    ro_wrote = (ctx.workspace / "readonly-write.txt").exists()
    good = inside.exists() and not outside.exists() and not ro_wrote
    detail = (
        f"inside cwd written={inside.exists()} ({describe(exc_in)}); "
        f"outside cwd written={outside.exists()} at {outside} ({describe(exc_out)}); "
        f"read-only wrote={ro_wrote} ({describe(exc_ro)})"
    )
    return (PASS if good else FAIL), detail


@check(5, "§7.19", "start", 1, "request_stop() mid-turn ends the turn; the workspace stops changing")
async def check_05(ctx: Ctx) -> Outcome:
    agent = ctx.agent("stop")
    marker = ctx.workspace / "stop-marker"
    marker.mkdir(exist_ok=True)
    prompt = (
        f"One at a time, create twenty files {marker}/f01.txt ... {marker}/f20.txt, each containing its own "
        "number, pausing about two seconds between files. Then reply DONE."
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
    ctx.facts["stop_finish_reason"] = getattr(result, "finish_reason", None)
    # request_stop() only sets the flag each in-flight turn races; reaping the
    # child is aclose()'s job (§7.19, and item 17 below). So "nothing writes
    # afterwards" is what is measured here, not a dead process.
    quiet = at_return == after_settle
    return (PASS if quiet and exc is None else FAIL), (
        f"returned after {secs:.1f}s, finish_reason={getattr(result, 'finish_reason', None)!r}, "
        f"{describe(exc)}; files at return={at_return}, after {ctx.args.settle}s={after_settle} "
        f"({'quiet' if quiet else 'STILL WRITING'})"
    )


@check(14, "§7.19", "start", 0, "A request_stop() turn reports finish_reason='interrupted'")
async def check_14(ctx: Ctx) -> Outcome:
    """Reads item 5's turn rather than spending another. `run()`'s
    `interrupted_by_host` branch keeps the partial answer *only* when the vendor
    reports `interrupted`; a real interrupted turn coming back `completed`
    fails §7.19 on a real difference, not on a naming quibble."""
    reason = ctx.facts.get("stop_finish_reason")
    if reason is None:
        return NOT_ARRANGED, "item 5 did not return a result to read a finish_reason from"
    return (PASS if reason == "interrupted" else FAIL), f"the vendor reported finish_reason={reason!r}"


@check(6, "§7.20", "start", 1, "timeout_seconds expiry while streaming interrupts and raises")
async def check_06(ctx: Ctx) -> Outcome:
    _, exc, cap, secs = await turn(
        ctx.agent("slow"),
        ctx.chat("timeout"),
        "Count slowly from 1 to 400, writing each number on its own line, pausing between each.",
    )
    raised = isinstance(exc, TimeoutError)
    return (PASS if raised else FAIL), (
        f"after {secs:.1f}s against timeout_seconds=25.0: {describe(exc)}; {len(cap.events)} event(s) first"
    )


@check(11, "§8.2", "start", 1, "What a thread.turn setup timeout leaves behind (the orphan)")
async def check_11(ctx: Ctx) -> Outcome:
    """§8.2 reasons, from the vendor's cancel path, that the child goes on
    working after BOS abandons the turn request — `_start_turn` closes the
    orphaned subscription and never cancels the submitted work. This is the only
    place that can be seen. "No observable orphan" is a real answer and is
    recorded as one."""
    tiny = ctx.agent("tiny")
    orphan = ctx.workspace / "orphan.txt"
    _, exc, _, secs = await turn(
        tiny,
        ctx.chat("orphan"),
        f"Wait about ten seconds, then create {orphan} containing ORPHAN, then reply DONE.",
    )
    phase = "the turn request (thread.turn)" if "thread.turn" in str(exc) else str(exc)[:120]
    if not isinstance(exc, TimeoutError):
        return NOT_ARRANGED, f"timeout_seconds=2.0 did not produce a setup timeout — got {describe(exc)}"
    seen = appeared(orphan, ctx.args.orphan_window)
    closed_at = time.monotonic()
    await tiny.aclose()
    close_secs = time.monotonic() - closed_at
    after_close = appeared(orphan, ctx.args.settle) if seen is None else None
    verdict = (
        f"orphan file appeared {seen:.1f}s after the timeout" if seen is not None
        else f"no orphan file within {ctx.args.orphan_window}s"
    )
    if seen is None and after_close is not None:
        verdict += f"; it appeared {after_close:.1f}s after aclose() instead"
    return OBSERVED, (
        f"raised at {secs:.1f}s in {phase}; {verdict}; aclose() took {close_secs:.1f}s to end the turn"
    )


@check(7, "§7.21", "start", 1, "schema= returns validated structured output")
async def check_07(ctx: Ctx) -> Outcome:
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


@check(8, "§7.22", "start", 2, "Codex CALLS the exposed BOS tool; an unexposed ep_tool is not callable")
async def check_08(ctx: Ctx) -> Outcome:
    """Narrowed by Task 10. CI already proves the *listing* half against a real
    `codex app-server` — `test_the_real_codex_child_reads_the_override_and_lists
    _the_tool` needs no login, so the child demonstrably reads
    `thread_start(config=…)`, authenticates with BOS's own bearer token and sees
    exactly the granted tool, even against a colliding operator entry in
    `config.toml`. Only the third that needs a model is left: the invocation
    itself, and the unexposed tool staying out of reach."""
    mcp = ctx.agent("mcp")
    before = len(TOOL_CALLS)
    _, exc_call, cap, _ = await turn(
        mcp,
        ctx.chat("mcp-call"),
        f"Call the {EXPOSED_TOOL} tool with text='hello from codex', then reply with the tool's exact result.",
    )
    called = [name for name, _ in TOOL_CALLS[before:]]
    _, exc_deny, _, _ = await turn(
        mcp,
        ctx.chat("mcp-deny"),
        f"List every tool you can call. Then try to call a tool named {WITHHELD_TOOL} with text='x'. "
        "Report exactly whether it exists and whether the call succeeded.",
    )
    reached_withheld = any(name == WITHHELD_TOOL for name, _ in TOOL_CALLS[before:])
    good = EXPOSED_TOOL in called and not reached_withheld
    return (PASS if good else FAIL), (
        f"host-side invocations: {called or 'NONE'}; {WITHHELD_TOOL} reached={reached_withheld}; "
        f"tool events={[k for k in cap.kinds() if k.startswith('tool')] or 'none'}; "
        f"call turn {describe(exc_call)}; deny turn {describe(exc_deny)}"
    )


@check(19, "§7.18", "start", 1, "A refused escalation does not stall the stream")
async def check_19(ctx: Ctx) -> Outcome:
    """`_deny_approval` runs on the vendor's single stdout reader thread and
    does a `logger.warning`. If that thread were blocked by the refusal, the gap
    between notifications around it is where it would show."""
    before = len(ctx.warnings.lines)
    result, exc, cap, secs = await turn(
        ctx.agent("ro"),
        ctx.chat("escalate"),
        f"Try to install a package, or write to {ctx.outside}. If a permission escalation is refused, "
        "continue the turn and report in one sentence what was refused.",
    )
    refusals = [line for line in ctx.warnings.lines[before:] if "refus" in line.lower() or "escalat" in line.lower()]
    if not refusals:
        return NOT_ARRANGED, (
            f"no escalation request reached _deny_approval in {secs:.1f}s — the model declined to ask, so there "
            f"was nothing to refuse. Turn finished: {getattr(result, 'finish_reason', None)!r}, {describe(exc)}"
        )
    return OBSERVED, (
        f"{len(refusals)} refusal warning(s): {refusals[0][:160]!r}; largest gap between notifications "
        f"{cap.max_gap():.1f}s over {len(cap.events)} event(s); turn ended "
        f"{getattr(result, 'finish_reason', None)!r} after {secs:.1f}s"
    )


@check(20, "§3.5", "start", 1, "A read-only agent can still call a mutating BOS tool over MCP")
async def check_20(ctx: Ctx) -> Outcome:
    """Correct by design — the sandbox confines the filesystem, not BOS's own
    tool surface — and the thing an operator is most likely to assume the other
    way round. Better measured and written into the BEP than discovered."""
    before = len(TOOL_CALLS)
    _, exc, _, _ = await turn(
        ctx.agent("ro"),
        ctx.chat("ro-mcp"),
        f"Call the {EXPOSED_TOOL} tool with text='mutation from a read-only agent', then reply with its result.",
    )
    reached = [text for name, text in TOOL_CALLS[before:] if name == EXPOSED_TOOL]
    return OBSERVED, (
        f"read-only agent reached the host tool {len(reached)} time(s) — host state mutated: {reached or 'NONE'}; "
        f"{describe(exc)}"
    )


@check(22, "§3.8", "start", 1, "Can the agent read BOS's MCP bearer token out of its own environment?")
async def check_22(ctx: Ctx) -> Outcome:
    """The token reaches the child through `CodexConfig(env=…)`. Whether a
    *shell command the agent runs* inherits it is Codex's own env policy, which
    CI cannot reach. It grants no more than that agent's own `mcp_tools`
    allowlist either way — but a credential landing in a transcript is worth
    knowing about."""
    result, exc, _, _ = await turn(
        ctx.agent("mcp"),
        ctx.chat("env"),
        "Run the shell command `env` and reply with the NAMES ONLY (no values) of any environment "
        "variables whose name starts with BOS_MCP_BEARER. If there are none, reply exactly NONE.",
    )
    reply = str(getattr(result, "output", "")) or describe(exc)
    visible = "BOS_MCP_BEARER" in reply
    return OBSERVED, (
        f"{'VISIBLE to the agent' if visible else 'not visible in the reply'} — agent said: {reply[:220]!r}"
    )


@check(13, "§7.25", "start", 3, "native_options reach the child AND work: a keyword and a config key")
async def check_13(ctx: Ctx) -> Outcome:
    """CI asserts both shapes *arrive*; only a live turn shows either *works*.
    Do not record "the knob does not suppress AGENTS.md" without this."""
    agents_md = ctx.workspace / "AGENTS.md"
    if not ctx.facts.get("seeded_agents_md"):
        return NOT_ARRANGED, f"{agents_md} was already present and was not overwritten, so there is no baseline"
    question = "What is the project codeword? Reply with the single word only."
    base, exc_base, _, _ = await turn(ctx.agent("rw"), ctx.chat("agentsmd-on"), question)
    off, exc_off, _, _ = await turn(ctx.agent("native"), ctx.chat("agentsmd-off"), question)
    base_text = str(getattr(base, "output", "")) or describe(exc_base)
    off_text = str(getattr(off, "output", "")) or describe(exc_off)

    # A keyword the vendor does not have is a TypeError out of thread_start, not
    # a silent drop. This costs no quota: it fails before any model call.
    _, exc_typo, _, _ = await turn(ctx.agent("typo"), ctx.chat("typo"), "hello")
    typo = "TypeError" if isinstance(exc_typo, TypeError) else describe(exc_typo)

    baseline_ok = CODEWORD in base_text.upper()
    suppressed = CODEWORD not in off_text.upper()
    if not baseline_ok:
        return NOT_ARRANGED, (
            f"no baseline: the default agent did not reflect AGENTS.md either (said {base_text[:120]!r}), so a "
            f"suppressed reply would prove nothing. project_doc_max_bytes=0 agent said {off_text[:120]!r}. "
            f"typo'd native_options keyword -> {typo}"
        )
    return (PASS if suppressed else FAIL), (
        f"baseline reflects AGENTS.md ({base_text[:80]!r}); with native_options.config.project_doc_max_bytes=0 "
        f"the reply is {off_text[:80]!r} ({'suppressed' if suppressed else 'STILL REFLECTS AGENTS.md'}); "
        f"personality='concise' was accepted by thread_start; typo'd keyword -> {typo}"
    )


@check(16, "§3.9", "start", 0, "Which TurnEvents an ordinary turn actually emits")
async def check_16(ctx: Ctx) -> Outcome:
    """Only command-execution and MCP-tool items map to `tool` events, so a turn
    that only reads and edits files should emit just `response` and
    `turn`/`finish`. Recorded so §3.9's mapping can be widened from evidence
    rather than guessed at. Reads item 1's turn; spends nothing."""
    events = ctx.facts.get("first_events")
    if events is None:
        return NOT_ARRANGED, "item 1 produced no turn to read events from"
    return OBSERVED, f"an ordinary turn (read a file, reply) emitted: {events or 'NOTHING'}"


@check(15, "§7.15", "start", 0, "Is usage non-empty, and does thread/tokenUsage/updated arrive?")
async def check_15(ctx: Ctx) -> Outcome:
    """Only ever exercised against an armed `ThreadTokenUsage` in CI. Reads item
    1's result; spends nothing."""
    usage = ctx.facts.get("first_usage")
    if usage is None:
        return NOT_ARRANGED, "item 1 produced no result to read usage from"
    return (PASS if usage else FAIL), (
        f"AgentResult.usage = {usage or '{} — the server sent no thread/tokenUsage/updated, or .last was empty'}"
    )


@check(10, "§7.24", "start", 1, "Quota exhaustion and an expired login each surface as a distinct error")
async def check_10(ctx: Ctx) -> Outcome:
    """The one item the script genuinely cannot arrange: it needs the account in
    a state only the owner can put it in, and inducing either would be worse
    than leaving the line blank. Re-run with --account-state=exhausted or
    --account-state=expired once the account is actually in that state."""
    state = ctx.args.account_state
    if state not in ("exhausted", "expired"):
        return NOT_ARRANGED, (
            "could not arrange: this needs a ChatGPT account with an exhausted quota or an expired login, and "
            "the script will not induce either. Re-run `start --account-state=exhausted` (or `=expired`) when "
            "the account is in that state; nothing else in this run is affected."
        )
    result, exc, _, _ = await turn(ctx.agent("rw"), ctx.chat(f"quota-{state}"), "Reply with the word PING.")
    if exc is None:
        return FAIL, (
            f"--account-state={state} was declared but the turn succeeded ({str(getattr(result, 'output', ''))[:80]!r})"
            " — the account is not in that state, so nothing was measured"
        )
    text = str(exc).lower()
    billed = any(word in text for word in ("api key", "api_key", "openai_api_key", "falling back"))
    return (PASS if not billed else FAIL), (
        f"account-state={state} -> {describe(exc)}; no API-key fallback mentioned={not billed}"
    )


@check(17, "§7.7 / §7.27", "start", 0, "Is the codex app-server child reaped after the harness exits?")
async def check_17(ctx: Ctx) -> Outcome:
    """The open half of §7.7/§7.27: CI now spawns a real child but asserts
    nothing about its death. Run last in the phase — it only records the pids
    here; the liveness check happens after ``BosApp.__aexit__``, in
    ``run_phase``."""
    pids = {}
    for kind in ctx.app.workspace.config.agents or {}:
        pid = child_pid(ctx.agent(kind))
        if pid is not None:
            pids[kind] = pid
    ctx.facts["child_pids"] = pids
    if not pids:
        return NOT_ARRANGED, "no agent had spawned a codex app-server child, so there was nothing to reap"
    return OBSERVED, f"pids before teardown: {pids} (liveness re-checked after BosApp.__aexit__, below)"


# ── Phase `resume` — a second process, so item 3 has a real restart ─────────


@check(3, "§7.17", "resume", 1, "After a process restart, a third ask() resumes the thread from ChatStore alone")
async def check_03(ctx: Ctx) -> Outcome:
    """This process never saw `start`'s client, thread object or in-memory
    state. The only thing crossing the boundary is the Codex thread id in
    JsonlChatStore's metadata, which is exactly §7.17's claim."""
    expected = ctx.facts.get("thread_id_from_start")
    before = await thread_id_of(ctx, MAIN_CHAT)
    if before is None:
        return NOT_ARRANGED, f"no Codex thread recorded for {MAIN_CHAT!r} — run the `start` phase first"
    result, exc, _, _ = await turn(
        ctx.agent("mcp"), MAIN_CHAT, "In one short sentence, what have we discussed in this thread so far?"
    )
    after = await thread_id_of(ctx, MAIN_CHAT)
    if result is None:
        return FAIL, f"resume failed: {describe(exc)} (thread on record was {before!r})"
    same = before == after and (expected is None or expected == after)
    return (PASS if same else FAIL), (
        f"recovered thread {before!r} from the store in a fresh process and resumed it; after the turn "
        f"{after!r}; start recorded {expected!r}. Reply: {str(result.output)[:140]!r}"
    )


@check(9, "§7.23", "resume", 0, "get_messages returns the native transcript; source='bos' the two-message record")
async def check_09(ctx: Ctx) -> Outcome:
    """This phase declares exactly one codex agent so `BosApp.get_messages(
    source="native")` can route — with two, it raises by design rather than
    guessing which agent served the chat. Three of §7.23's preconditions bite
    here: messages only (never tool activity), commentary-phase agent messages
    dropped, and a not-fully-loaded turn showing as one gap marker."""
    try:
        native = await ctx.app.get_messages(MAIN_CHAT, source="native")
    except Exception as exc:  # noqa: BLE001 - the error is the observation
        return FAIL, f"source='native' raised: {describe(exc)}"
    bos = await ctx.app.get_messages(MAIN_CHAT, source="bos")
    gaps = [m for m in native if (m.metadata or {}).get("items_view")]
    roles = [str((m.llm_message or {}).get("role")) for m in native]
    turns_committed = len({m.turn_id for m in bos if m.turn_id})
    # BOS commits exactly two messages per turn for an externally-backed chat.
    consistent = len(bos) == 2 * turns_committed
    return (PASS if native and consistent else FAIL), (
        f"native: {len(native)} message(s) {roles}, {len(gaps)} unloaded-turn gap marker(s); "
        f"bos: {len(bos)} message(s) over {turns_committed} turn(s) "
        f"({'two per turn as promised' if consistent else 'NOT two per turn'})"
    )


@check(18, "§7.22", "resume", 1, "Does a RESUMED chat still get the MCP tools?")
async def check_18(ctx: Ctx) -> Outcome:
    """`thread_resume` takes the same `config=` kwarg from the same dict as
    `thread_start`, but CI has only ever watched a *started* thread list
    `bos-tools`. This is a resumed one, in a process that did not start it."""
    before = len(TOOL_CALLS)
    _, exc, cap, _ = await turn(
        ctx.agent("mcp"),
        MAIN_CHAT,
        f"Call the {EXPOSED_TOOL} tool with text='after resume', then reply with the tool's exact result.",
    )
    called = [name for name, _ in TOOL_CALLS[before:]]
    return (PASS if EXPOSED_TOOL in called else FAIL), (
        f"on a thread resumed from the store: host-side invocations {called or 'NONE'}; "
        f"tool events {[k for k in cap.kinds() if k.startswith('tool')] or 'none'}; {describe(exc)}"
    )


@check(21, "§8.2", "resume", 1, "A second ask() after aclose() — what a host that reuses the agent gets")
async def check_21(ctx: Ctx) -> Outcome:
    """`aclose()` does not reset `self._client`, so the agent keeps a closed
    one. One line of observed behaviour — run last, because it closes the client
    this phase was using."""
    agent = ctx.agent("mcp")
    await agent.aclose()
    _, exc, _, _ = await turn(agent, ctx.chat("after-close"), "Reply with the word AFTER.")
    return OBSERVED, f"ask() after aclose() -> {describe(exc)}"


# ── Phase `nologin` — item 12, and no quota spent ───────────────────────────


@check(12, "§3.10.3 / §4.3", "nologin", 0, "An absent login surfaces at the first ask(), not at build_agent()")
async def check_12(ctx: Ctx) -> Outcome:
    """§3.10.3 and §4.3 were *corrected* from this shape rather than to it, so
    the point is to confirm the correction: construction succeeds, the preflight
    in `_ensure_client` is what fails, and it fails once rather than per turn.

    Arranged without touching the owner's real login: `CODEX_HOME` points at an
    empty throwaway directory for this process only, which is the same trick
    `test_the_real_codex_child_reads_the_override_and_lists_the_tool` uses to
    keep the machine's own `~/.codex` out of a test.
    """
    built = ctx.agent("mcp")  # BosApp.__aenter__ already built it — that IS the observation
    cfg = dict(built.resolved_config)
    _, exc, _, _ = await turn(built, ctx.chat("nologin"), "Reply with the word PING.")
    preflight = isinstance(exc, RuntimeError) and "no Codex account is logged in" in str(exc)
    return (PASS if preflight else FAIL), (
        f"build succeeded (auth={cfg['auth']!r}, permission={cfg['permission']!r}) with CODEX_HOME="
        f"{os.environ.get('CODEX_HOME')!r}; the first ask() -> {describe(exc)}"
    )


# ── Versions, banner, report ────────────────────────────────────────────────


def sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("openai-codex")
    except PackageNotFoundError:
        return "not installed"


def cli_version() -> str:
    """The `codex` binary the SDK would actually spawn, asked the same way the
    SDK resolves it, so the recorded version is the one that ran."""
    try:
        from openai_codex.client import CodexConfig, _resolve_codex_bin

        binary = str(_resolve_codex_bin(CodexConfig()))
    except Exception:
        binary = shutil.which("codex") or ""
    if not binary or not Path(binary).exists():
        return "codex binary not found"
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip() or "no version output"
    except Exception as exc:  # noqa: BLE001
        return f"could not ask the binary: {exc}"


def banner(phase: str, workspace: Path, outside: Path, bos_dir: Path, items: list[Item]) -> str:
    budget = sum(i.turns for i in items)
    lines = [
        "=" * 78,
        f"BEP 19 Layer 4 live validation — phase {phase!r} — {date.today().isoformat()}",
        "=" * 78,
        "",
        "THIS SPENDS YOUR CHATGPT SUBSCRIPTION QUOTA AND WRITES TO DISK."
        if budget
        else "This phase spends no quota — every turn in it is expected to fail before a model is reached.",
        "",
        f"  model turns budgeted for this phase : ~{budget} (plus retries; the real cost can exceed this)",
        f"  openai-codex                        : {sdk_version()}",
        f"  codex CLI                           : {cli_version()}",
        "",
        "  Every path this phase may write to:",
        f"    workspace / agent cwd             : {workspace}",
        "      seeded by this script           : NOTES.md, AGENTS.md",
        "      written by the agent            : inside-write.txt, stop-marker/, orphan.txt, and whatever",
        "                                        else a turn decides to create",
        f"    BOS state (chat store, JSONL)     : {bos_dir}",
        *(
            [
                f"    OUTSIDE the workspace, on purpose : {outside}",
                "      item 4 asks a workspace-write agent to write there; the sandbox is supposed to refuse.",
                "      It is a fresh temp directory this script created — never your repo.",
            ]
            if any(i.n == 4 for i in items)
            else []
        ),
        "",
        f"  Items in this phase, in execution order: {', '.join(str(i.n) for i in items)}",
        "",
    ]
    return "\n".join(lines)


def render(items: list[Item], phases: list[str]) -> str:
    """The paste-ready block. Its job is to become BEP §8.1's readiness table and
    a §9 revision entry with light editing — so it carries the date, the SDK
    version, and every NOT ARRANGED line intact."""
    done = sorted((i for i in items if i.mark), key=lambda i: i.n)
    counts = {m: sum(1 for i in done if i.mark == m) for m in MARKS}
    out = [
        "",
        "=" * 78,
        "PASTE-READY RESULTS",
        "=" * 78,
        "",
        f"Run on {date.today().isoformat()} — openai-codex {sdk_version()}, codex CLI {cli_version()}",
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
        f"- {date.today().isoformat()} — Live validation of Layer 4 Codex against a real ChatGPT subscription "
        f"(openai-codex {sdk_version()}, codex CLI {cli_version()}), phases {'+'.join(phases)}, via "
        f"`scripts/validate_codex_runtime.py`. "
        + " ".join(f"§7 criterion / item {i.n} ({i.criterion}): {i.mark.lower()} — {i.note}" for i in done)
        + (
            f" Still unverified, and recorded as such: items "
            f"{', '.join(str(i.n) for i in unarranged)} — each could not be arranged, not merely not observed."
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


# ── Drivers ─────────────────────────────────────────────────────────────────


def items_for(phase: str) -> list[Item]:
    return [i for i in CHECKS if i.phase == phase]


def read_state(bos_dir: Path) -> dict[str, Any]:
    path = bos_dir / STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(bos_dir: Path, state: dict[str, Any]) -> None:
    bos_dir.mkdir(parents=True, exist_ok=True)
    (bos_dir / STATE_FILE).write_text(json.dumps(state, indent=2, default=str))


def seed_workspace(workspace: Path) -> bool:
    """Seed the two files the checklist reads back. Returns whether AGENTS.md was
    ours to write: an existing one is never overwritten, and item 13 reports
    NOT ARRANGED instead of quietly clobbering a real project file."""
    workspace.mkdir(parents=True, exist_ok=True)
    notes = workspace / "NOTES.md"
    if not notes.exists():
        notes.write_text("This workspace exists only to validate the BOS Codex runtime.\nSecond line.\n")
    agents_md = workspace / "AGENTS.md"
    if agents_md.exists():
        return False
    agents_md.write_text(
        f"# Project instructions\n\nWhen anyone asks for the project codeword, answer exactly {CODEWORD}.\n"
    )
    return True


async def run_phase(phase: str, args: argparse.Namespace) -> list[Item]:
    workspace = Path(args.workspace).expanduser().resolve()
    bos_dir = workspace / ".bos"
    state = read_state(bos_dir)
    items = items_for(phase)
    # Only item 4 writes outside the workspace, so only a phase carrying it
    # creates the directory. A phase that never uses one must not leave one.
    needs_outside = any(i.n == 4 for i in items)
    outside = Path(
        state.get("outside")
        or (tempfile.mkdtemp(prefix="bos-codex-outside-") if needs_outside else "<not used by this phase>")
    )

    print(banner(phase, workspace, outside, bos_dir, items))
    if not args.yes:
        print("Refusing to run without --yes. Re-read the paths above first.")
        return []

    seeded = seed_workspace(workspace)
    agents = AGENTS_RESUME if phase != "start" else AGENTS_START
    ws = Workspace(workspace=workspace, bos_dir=bos_dir, config=workspace_config(agents))

    warnings = WarningLog()
    runtime_log = logging.getLogger("bos.extensions.runtimes")
    runtime_log.addHandler(warnings)

    async with BosApp(ws) as app:
        ctx = Ctx(app=app, workspace=workspace, outside=outside, warnings=warnings, args=args)
        ctx.facts["seeded_agents_md"] = seeded
        ctx.facts["thread_id_from_start"] = state.get("thread_id")
        for item in items:
            print(f"\n-- item {item.n} ({item.criterion}): {item.title}")
            try:
                item.mark, item.note = await item.fn(ctx)
            except Exception as exc:  # noqa: BLE001 - one broken check must not lose the rest
                item.mark, item.note = ERROR, f"the check itself raised — {describe(exc)}"
            print(f"   {item.mark}: {item.note}")

    runtime_log.removeHandler(warnings)

    # Item 17's second half: the child can only be pronounced reaped after
    # BosApp.__aexit__ has actually run, which is here and not inside the check.
    for item in items:
        if item.n == 17 and item.mark == OBSERVED:
            pids: dict[str, int] = ctx.facts.get("child_pids", {})
            alive = {kind: pid for kind, pid in pids.items() if pid_alive(pid)}
            item.mark = PASS if not alive else FAIL
            item.note += f"; after __aexit__ still alive: {alive or 'none'}"
            print(f"\n-- item 17 re-checked after teardown: {item.mark}: {item.note}")

    state.update(
        {
            "workspace": str(workspace),
            "outside": str(outside),
            "thread_id": ctx.facts.get("thread_id") or state.get("thread_id"),
            "sdk_version": sdk_version(),
            f"phase_{phase}": {
                "date": date.today().isoformat(),
                "items": [{"n": i.n, "criterion": i.criterion, "title": i.title, "mark": i.mark, "note": i.note}
                          for i in items],
            },
        }
    )
    write_state(bos_dir, state)
    return items


def restore_items(state: dict[str, Any], phase: str) -> list[Item]:
    """Rebuild an earlier phase's recorded items so the final table is whole.
    Only what that phase actually printed — never a mark this process invented."""
    recorded = (state.get(f"phase_{phase}") or {}).get("items", [])
    out = []
    for raw in recorded:
        stub = Item(
            n=raw["n"], criterion=raw["criterion"], phase=phase, turns=0, title=raw["title"],
            fn=_unrunnable, mark=raw["mark"], note=raw["note"],
        )
        out.append(stub)
    return out


async def _unrunnable(ctx: Ctx) -> Outcome:
    raise AssertionError("a restored item from an earlier phase is a record, not a check to re-run")


# ── The offline self-check ──────────────────────────────────────────────────


async def _check_configs_build() -> None:
    """Open a real ``BosApp`` over ``AGENTS_START`` and assert what every agent
    resolved to. Only the `start` set: it is a superset of `resume`'s, and
    ``BosApp`` refuses a second live instance per process."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ws"
        root.mkdir()
        ws = Workspace(workspace=root, bos_dir=root / ".bos", config=workspace_config(AGENTS_START))
        async with BosApp(ws) as app:
            for kind, wanted in AGENTS_START.items():
                cfg = dict(codex_agent(app, kind).resolved_config)
                assert cfg["external_runtime"] == "codex", f"{kind} did not dispatch to the Codex runtime"
                assert cfg["permission"] == wanted["permission"], kind
                assert cfg["cwd"] == str(root), f"{kind} cwd resolved to {cfg['cwd']}, not the workspace"
                assert cfg["auth"] == "subscription", f"{kind} must use the login, not an API key"
                assert not cfg["mcp_tools_unavailable"], (
                    f"{kind} asks for {cfg['mcp_tools_unavailable']}, which no ep_tool in this script registers"
                )
                assert child_pid(codex_agent(app, kind)) is None, f"building {kind} spawned a child (§3.1 forbids it)"
            assert codex_agent(app, "mcp").resolved_config["mcp_tools"] == [EXPOSED_TOOL]
            assert WITHHELD_TOOL not in codex_agent(app, "mcp").resolved_config["mcp_tools"], (
                "the withheld tool must never be granted, or item 8 proves nothing"
            )
            assert codex_agent(app, "native").resolved_config["native_options"]["config"]["project_doc_max_bytes"] == 0
            assert type(app.harness.chat_store).__name__ == "JsonlChatStore", (
                "item 3 needs a store that outlives the process"
            )


def self_check() -> int:
    """Everything here that does not need the network, asserted.

    Nobody can run this script end to end without a ChatGPT login, so its
    correctness has to come from somewhere: the registry's shape, the report
    renderer, the banner, and the private vendor attribute chain item 17 reads.
    Deliberately NOT a pytest file — this script must stay uncollectable.
    """
    numbers = [i.n for i in CHECKS]
    assert sorted(numbers) == list(range(1, 23)), f"checklist 1-22 must each appear once, got {sorted(numbers)}"
    assert {i.phase for i in CHECKS} == {"start", "resume", "nologin"}, "unknown phase on some item"
    for item in CHECKS:
        assert item.criterion.startswith("§"), f"item {item.n} has no BEP reference"
        assert item.turns >= 0 and item.title, f"item {item.n} is missing metadata"
    assert sum(i.turns for i in CHECKS) > 0, "the turn budget must not be zero"

    # Item 17 names a private vendor chain. Assert the spelling against the real
    # classes, not an instance, so a vendor rename is caught here rather than in
    # the middle of a paid run.
    from openai_codex import AsyncCodex, CodexConfig

    probe = AsyncCodex(CodexConfig())
    assert child_pid(probe) is None, "a fresh client must have no child process yet"
    sync = probe._client._sync
    for attribute in ("_proc", "_approval_handler"):
        assert hasattr(sync, attribute), f"openai-codex moved {attribute}; item 17 / §3.5.4 need it"

    class _Stub:
        pass

    assert child_pid(_Stub()) is None, "child_pid must survive a moved attribute chain"

    # The renderer is what becomes the BEP text, so exercise every mark through it.
    sample = [
        Item(n=1, criterion="§7.15", phase="start", turns=1, title="t", fn=_unrunnable, mark=PASS, note="ok"),
        Item(n=10, criterion="§7.24", phase="start", turns=1, title="t", fn=_unrunnable,
             mark=NOT_ARRANGED, note="could not arrange: no exhausted account"),
        Item(n=16, criterion="§3.9", phase="start", turns=0, title="t|piped", fn=_unrunnable,
             mark=OBSERVED, note="a|b"),
    ]
    text = render(sample, ["start"])
    assert "NOT ARRANGED" in text and "could not arrange" in text, "unarranged items must survive into the report"
    assert "\\|" in text, "a pipe in a note must be escaped or it breaks the markdown table"
    assert text.count("\n|") >= 5, "the readiness table lost rows"
    assert "§9" in text and date.today().isoformat() in text, "the revision entry needs a date"
    assert "**PASS**" in text and "**OBSERVED**" in text

    # A report with nothing unarranged must not claim there was something.
    clean = render([sample[0]], ["start"])
    assert "Nothing in this run was left unarranged." in clean
    assert "NOT ARRANGED" not in clean

    # The banner must name every destructive path and a non-zero budget.
    text = banner("start", Path("/ws"), Path("/outside"), Path("/ws/.bos"), items_for("start"))
    for needle in ("/ws", "/outside", "/ws/.bos", "QUOTA", "model turns budgeted"):
        assert needle in text, f"the up-front banner never mentions {needle!r}"

    # Config shapes: every agent inherits the reserved kind and sets a permission
    # explicitly, because there is no default and there must not be one.
    for agents in (AGENTS_START, AGENTS_RESUME):
        for kind, cfg in agents.items():
            assert cfg["_parent"] == "codex", f"{kind} must inherit the reserved kind"
            assert cfg["permission"] in ("read-only", "workspace-write", "full-access"), kind
            assert "external_runtime" not in cfg, "BOS writes external_runtime; config must not"
    assert len(AGENTS_RESUME) == 1, "item 9 needs exactly one codex agent for get_messages to route"
    assert {i.n for i in items_for("resume")} == {3, 9, 18, 21}
    assert {i.n for i in items_for("nologin")} == {12}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ws"
        assert seed_workspace(root) is True
        assert CODEWORD in (root / "AGENTS.md").read_text()
        assert seed_workspace(root) is False, "an existing AGENTS.md must never be overwritten"
        write_state(root / ".bos", {"thread_id": "th_1"})
        assert read_state(root / ".bos")["thread_id"] == "th_1"
        assert read_state(root / "nope") == {}
        assert appeared(root / "never.txt", 0.3) is None
        (root / "there.txt").write_text("x")
        assert appeared(root / "there.txt", 1.0) is not None
        restored = restore_items({"phase_start": {"items": [
            {"n": 1, "criterion": "§7.15", "title": "t", "mark": PASS, "note": "n"}]}}, "start")
        assert [i.mark for i in restored] == [PASS]
        assert restore_items({}, "start") == []

    # The one thing worth more than all of the above: that the config in this
    # file really does build every agent through a real BosApp. Construction is
    # lazy (BEP 19 §3.1) — no client, no `codex app-server` child, no login — so
    # this costs nothing and still catches a bad `_parent`, a rejected
    # `native_options` key, a permission BOS refuses, or an `mcp_tools` name the
    # host has no `ep_tool` for. Every one of those would otherwise surface
    # halfway through a paid run.
    asyncio.run(_check_configs_build())

    assert describe(None) == "no error"
    assert describe(ValueError("boom")).startswith("ValueError: boom")
    assert pid_alive(os.getpid()) is True

    cap = Capture()
    assert cap.max_gap() == 0.0 and cap.kinds() == []

    print(f"self-check OK — {len(CHECKS)} items, ~{sum(i.turns for i in CHECKS)} model turns across all phases")
    print(f"openai-codex {sdk_version()}; codex CLI {cli_version()}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="validate_codex_runtime.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("phase", choices=("start", "resume", "nologin", "plan", "self-check"))
    p.add_argument("workspace", nargs="?", default=None, help="agent cwd; defaults to a fresh temp directory")
    p.add_argument("--yes", action="store_true", help="required: you have read the paths and accept the quota cost")
    p.add_argument(
        "--account-state",
        choices=("normal", "exhausted", "expired"),
        default="normal",
        help="item 10 only: declare that the logged-in account is already in that state",
    )
    p.add_argument("--stop-after", type=float, default=8.0, help="item 5: seconds before request_stop()")
    p.add_argument("--settle", type=float, default=15.0, help="seconds to watch for writes after a turn ends")
    p.add_argument("--orphan-window", type=float, default=60.0, help="item 11: how long to watch for an orphan")
    return p


def resolve_workspace(args: argparse.Namespace) -> None:
    """`start` picks the workspace; the later phases inherit it, because item 3
    recovers a thread id from that directory's chat store and a different one
    would silently start a fresh session instead."""
    if args.phase == "plan":
        # A placeholder, not a mkdtemp: `plan` only prints, and a mode that
        # promises to touch nothing must not leave a directory behind.
        args.workspace = args.workspace or str(Path(tempfile.gettempdir()) / "<a fresh bos-codex-ws-* directory>")
        return
    if args.phase == "start":
        args.workspace = args.workspace or tempfile.mkdtemp(prefix="bos-codex-ws-")
        return
    if args.phase == "nologin":
        # Needs no earlier run: it answers item 12 on its own. Its own prefix, so a
        # later bare `resume` (which globs bos-codex-ws-*) never picks this up
        # instead of the `start` workspace — run_phase writes state here too.
        args.workspace = args.workspace or tempfile.mkdtemp(prefix="bos-codex-nologin-ws-")
        return
    if args.workspace:
        return
    for candidate in sorted(Path(tempfile.gettempdir()).glob("bos-codex-ws-*"), key=lambda p: -p.stat().st_mtime):
        if (candidate / ".bos" / STATE_FILE).exists():
            args.workspace = str(candidate)
            return
    raise SystemExit(
        f"No workspace given and no earlier run found. Pass the same directory the `start` phase printed, "
        f"which also holds .bos/{STATE_FILE}."
    )


def guard_repo(workspace: Path) -> None:
    """One cheap refusal. A turn in here writes files and, for item 4, tries to
    write outside — none of which belongs in a source checkout."""
    if (workspace / "pyproject.toml").exists() and (workspace / "src" / "bos").is_dir():
        raise SystemExit(f"{workspace} looks like the bos-ai checkout. Point this at a scratch directory instead.")


async def amain(args: argparse.Namespace) -> int:
    if args.phase == "nologin":
        # Before BosApp opens, because the child inherits the environment at
        # spawn time and `_preflight_auth` runs on the very first turn.
        home = tempfile.mkdtemp(prefix="bos-codex-nologin-home-")
        os.environ["CODEX_HOME"] = home
        print(f"CODEX_HOME redirected to the empty {home} — your real ~/.codex login is untouched.\n")

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
        else f"\nNext, in a FRESH process (item 3 needs a real restart):\n\n"
        f"    uv run python scripts/validate_codex_runtime.py resume --yes {args.workspace}\n\n"
        f"That phase prints the combined, paste-ready table for both phases."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.phase == "self-check":
        return self_check()
    resolve_workspace(args)
    workspace = Path(args.workspace).expanduser().resolve()
    guard_repo(workspace)
    if args.phase == "plan":
        outside = Path(tempfile.gettempdir()) / "<a fresh bos-codex-outside-* directory>"
        for phase in ("start", "resume", "nologin"):
            print(banner(phase, workspace, outside, workspace / ".bos", items_for(phase)))
        return 0
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
