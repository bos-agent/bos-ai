"""The Claude Code vendor runtime (BEP 19 Layer 4b).

``ClaudeCodeAgent`` is BOS's ``ExternalRuntime`` adapter over ``claude_agent_sdk``,
the vendor SDK that drives the ``claude`` CLI bundled in its wheel, one CLI child per
turn (BEP 19 §3.10.1). It implements ``AgentPort`` without being a
``bos.core.agent.Agent``: it holds no ``LLM`` and runs none of BOS's turn loop.

Unlike Codex, the CLI does not confine itself on BOS's say-so: ``permission_mode`` is
an approval policy, not a sandbox. BOS builds the confinement from mechanisms the CLI
offers (BEP 19 §3.5.3) — a ``PreToolUse`` hook for file tools and the CLI's OS sandbox
for bash — and gives ``can_use_tool`` only the job of answering what ``permission``
already decided.

This module imports ``claude_agent_sdk`` at module scope, which is safe for the reason
``codex.py`` gives for its own vendor import: it is reached only through ``importlib``,
from ``bos.core.harness._load_external_runtime``. It must also stay the first
third-party import here. The harness recognises a missing ``bos-ai[claude-code]`` by
the module name on the ``ModuleNotFoundError``, so importing another package the extra
brings, such as ``mcp``, ahead of it would report a missing extra as a broken runtime
module. ``test_a_missing_extra_is_named_by_the_real_runtime_module`` pins the order
against ``mcp``.

What exists so far is construction — the config, the permission mapping, the per-client
settings nonce, no repository settings unless a host opts in, the ``native_options``
allowlist, and the fail-closed preflights; every check that reads the host runs there, and
none starts the CLI — a turn: ``run()`` resumes the chat's native session, runs one CLI
child to the end of the turn and commits the two-message record (BEP 19 §3.6, §3.7, §3.9) —
structured output: a ``schema`` turn validates the CLI's answer locally and retries with a
correction message on failure, up to ``max_schema_retries`` (§3.9, BEP 12) — the turn
streamed live: ``receive_response()`` is translated into ``TurnEvent``s for ``event_sink`` as it
arrives, one attempt (a schema retry included) at a time (§3.9) — and the control surface: a
message the ``interrupt`` poll returns is delivered into the running turn, ``AbortTurn`` and
``request_stop()`` interrupt it, ``timeout_seconds`` bounds the CLI's start and each attempt, and
``aclose()`` stops every turn, drains them within a bound and closes their clients (§3.9, §3.10.2;
the audit of every wait that crosses to the CLI sits above ``_Turn``) — and the confinement
(§3.5.3): a deny-by-default ``tools=`` allowlist per ``permission`` (``_TOOL_LEVELS``), a
``PreToolUse`` hook that path-checks the file tools to ``cwd`` and denies anything outside the
level's allowlist (``_hook``), a ``can_use_tool`` backstop for the prompts the hook lets through
(``_can_use_tool``), and a stderr tripwire that ends a ``workspace-write`` turn if the CLI reports
its bash sandbox disabled (``_sandbox_tripwire``) — and the MCP egress (§3.8): when ``mcp_tools``
names a tool the host has, each client names BOS's loopback MCP server, its bearer token only in the
client's environment (``_mcp_egress``, ``_options``). The exact names of the tools that server
granted pass the hook at every level, and ``can_use_tool`` allows them where the mode asks it (not at
``full-access``, where nothing does); any other MCP tool the hook denies. A turn whose CLI reports
BOS's server as not connected logs a WARNING (``_warn_unless_mcp_connected``).

Beside that turn path — not a stage of it — sits ``native_messages`` (Task 9, BEP 19 §3.7): the
read that projects Claude Code's *own* transcript back into BOS ``Message``s, so a host can render
a session BOS did not author. Unlike ``CodexAgent.native_messages`` it starts no vendor process at
all — ``get_session_messages`` (``claude_agent_sdk``) is a plain filesystem read of the session's
own JSONL, run in a thread since it is blocking I/O, not a call to a client this class owns. It is
also why ``CLAUDE_CODE_PROJECT_DIR_NAME`` joined ``_INHERITED_ENV_OVERRIDES`` here: that variable
renames the project folder the CLI writes a transcript under, and ``get_session_messages`` does not
read it, so a value inherited from BOS's own environment would make this read look in the wrong
place for a transcript the CLI just wrote.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast, get_args

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    McpServerConfig,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultError,
    ResultMessage,
    SandboxSettings,
    SettingSource,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_messages,
)
from claude_agent_sdk.types import (
    CanUseTool,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    SystemPromptPreset,
)

from bos.core.agent import (
    ABORTED_TURN_CONTENT,
    SHUTDOWN_CONTENT,
    AbortTurn,
    AgentEventType,
    AgentResult,
    ChatStore,
    Message,
    MessageContent,
    StructuredOutputError,
    StructuredValidator,
    TurnEvent,
    TurnEventDetail,
    TurnEventPhase,
    TurnEventSink,
    TurnEventStage,
    _apply_async,
    _compact,
    content_as_parts,
    image_source_to_model_url,
)
from bos.core.agent.agent import MAX_ITERATION_CONTENT
from bos.extensions.runtimes._shared import (
    ExternalAgentConfig,
    commit_external_turn,
    external_agent_result,
    parse_external_config,
    read_native_session_id,
)

# Safe at module scope: `mcp_egress` defers every third-party import into the
# method that needs it, so naming it here costs nothing and requires no extra.
from bos.extensions.runtimes.mcp_egress import unregistered_tools

logger = logging.getLogger(__name__)

_RUNTIME = "claude-code"

# Patched in tests to inject a double — the only seam one needs.
_CLIENT_FACTORY: Callable[..., Any] = ClaudeSDKClient

# BEP 19 §3.5: `permission` -> the CLI's approval policy. The policy is not the
# confinement (§3.5.3: the PreToolUse hook and the bash sandbox are); what a mode decides
# is which calls reach a permission prompt, and so `can_use_tool`. Three of the SDK's six
# modes are never used: `plan` is not a write guard (fact 5), `dontAsk` refuses an in-root
# Write too (measured, not pinned), so `workspace-write` could not write, and under `auto`
# a classifier model decides each call — an LLM deciding what `permission` already has
# (§3.5.4).
_PERMISSION_MODES: dict[str, PermissionMode] = {
    "read-only": "default",
    "workspace-write": "acceptEdits",
    "full-access": "bypassPermissions",
}

# BEP 19 §3.5.3: the bash sandbox, under `workspace-write` only. `read-only` needs none
# because its hook is to deny Bash outright (§3.5; the hook is not built yet), and
# `full-access` confines nothing. With `allowUnsandboxedCommands: False`, per the SDK's
# docstring, every command must run sandboxed or be in `excludedCommands`, whatever the
# model asks (read, not measured). `failIfUnavailable` makes the CLI refuse to start,
# rather than run bash unsandboxed, when the sandbox cannot start (fact 6c) — without it
# the sandbox fails open (fact 6). CLI 2.1.281 honours that key, but the SDK's
# `SandboxSettings` TypedDict does not declare it: a third-party stub gap. The SDK copies
# the dict into `--settings` verbatim (subprocess_cli.py:519), hence a plain dict here and
# a cast where it is sent.
_WORKSPACE_WRITE_SANDBOX: dict[str, Any] = {
    "enabled": True,
    "allowUnsandboxedCommands": False,
    "failIfUnavailable": True,
}

# BEP 19 §3.5.3: the confinement is deny-by-default per permission level, over the CLI's own
# offered tool list — not an enumeration of "mutating tools". CLI 2.1.281 offers twenty tools with
# no permission handler and three more (the interactive trio below) when one is set; the whole set is
# classified here into the levels at which BOS offers it, pinned by
# test_the_offered_tools_are_all_classified (from the `tools` of the first model request). A CLI
# release that adds, renames or drops a tool fails that test until someone classifies it — the same
# pattern as `native_options`' allowlist.
#
# Two mechanisms enforce it: `tools=` is the CLI-level allowlist, so an excluded tool is never
# offered and is not callable even when the model asks ("No such tool available … disabled for this
# session", pinned) — and not re-addable through a trusted repo's `permissions.allow` either
# (measured while building the hook, not pinned); and the PreToolUse hook is the per-call gate
# (`_hook`), which denies anything outside the level's allowlist as a backstop and path-checks the
# file tools, and whose deny holds against a trusted repo's own allow rule (fact 4, pinned). The
# classification, per level:
#
# - Read is offered at every level; the hook path-checks it to `cwd` (reads outside are denied,
#   BEP §3.5.5). Write/Edit/NotebookEdit (the file-mutating tools) and Bash are offered at
#   `workspace-write` and `full-access` only — Bash is confined by the OS sandbox at
#   `workspace-write` and off at `full-access`; the file writers are path-checked to `cwd`. Under
#   `read-only` none of the four is offered, which is how "read-only writes nothing" holds.
# - ListAgents and SendMessage reach "other local Claude sessions on this machine" (the tools' own
#   descriptions to the model), which no `permission` level grants — `permission` bounds the
#   filesystem, not the operator's other sessions — so they are excluded at EVERY level,
#   `full-access` included (R10).
# - The tools that persist beyond the turn or move/branch the session are excluded below
#   `full-access`, because BOS has not measured them safe under a permission bound (deny-by-default):
#   CronCreate/CronDelete/CronList (a durable CronCreate persists to `.claude/scheduled_tasks.json`),
#   EnterWorktree/ExitWorktree (create a git worktree and switch the session into it),
#   Workflow (persists its script under the session directory), Agent (launches subagents, and can
#   create a worktree), ScheduleWakeup and TaskStop. Whether the hook sees a subagent's own tool
#   calls is not measured (§8.2), but it does not need to be: Agent is excluded below full-access on
#   the deny-by-default ground above, so no confined agent spawns a subagent in the first place.
# - WebFetch and WebSearch are network egress (WebFetch reaches the local network too). Both reach
#   the hook and `can_use_tool` (measured while building the hook, not pinned), so BOS could gate them
#   either way; their policy is an open question (§8.2(a)), so under deny-by-default they are excluded
#   below `full-access`
#   until it is decided.
# - Skill (loads packaged instructions into the turn) and ReportFindings (reports to a host review
#   UI) are neither confinement-bounded nor needed by a confined agent, so deny-by-default excludes
#   them below `full-access` too.
# - AskUserQuestion, EnterPlanMode and ExitPlanMode are excluded at EVERY level: BOS drives turns
#   programmatically and has no user to prompt (§2.2.2) and never enters the CLI's plan-mode UX, so a
#   tool that asks the user or toggles plan mode can only hang or misfire, at any level. The CLI
#   offers these three only when a permission handler (`can_use_tool`) is present — which BOS sets at
#   read-only and workspace-write (where `tools=` excludes them) and not at full-access (where the CLI
#   does not offer them at all), so `test_the_offered_tools_are_all_classified` measures them at
#   read-only.
#
# `full-access` confines nothing on the filesystem, so it offers every tool EXCEPT the cross-session
# pair and the interactive trio, none of which any level grants.
_TOOL_LEVELS: Mapping[str, frozenset[str]] = MappingProxyType({
    "Read": frozenset({"read-only", "workspace-write", "full-access"}),
    "Write": frozenset({"workspace-write", "full-access"}),
    "Edit": frozenset({"workspace-write", "full-access"}),
    "NotebookEdit": frozenset({"workspace-write", "full-access"}),
    "Bash": frozenset({"workspace-write", "full-access"}),
    "ListAgents": frozenset(),
    "SendMessage": frozenset(),
    "AskUserQuestion": frozenset(),
    "EnterPlanMode": frozenset(),
    "ExitPlanMode": frozenset(),
    "Agent": frozenset({"full-access"}),
    "CronCreate": frozenset({"full-access"}),
    "CronDelete": frozenset({"full-access"}),
    "CronList": frozenset({"full-access"}),
    "EnterWorktree": frozenset({"full-access"}),
    "ExitWorktree": frozenset({"full-access"}),
    "Workflow": frozenset({"full-access"}),
    "ScheduleWakeup": frozenset({"full-access"}),
    "TaskStop": frozenset({"full-access"}),
    "WebFetch": frozenset({"full-access"}),
    "WebSearch": frozenset({"full-access"}),
    "Skill": frozenset({"full-access"}),
    "ReportFindings": frozenset({"full-access"}),
})

# BEP 19 §3.5.3: the offered tools that take a path argument, and which key names it. Enumerated
# from what the CLI offers the model (the param names are `file_path` for Read/Write/Edit and
# `notebook_path` for NotebookEdit; the offered set itself is pinned by
# test_the_offered_tools_are_all_classified). MultiEdit/Glob/Grep — named by an earlier draft — are
# not offered by CLI 2.1.281, so they are absent here. EnterWorktree (`path`) and
# Workflow (`scriptPath`) also take a path, but both are offered only at `full-access`, where the
# hook path-checks nothing, so neither needs an entry. A tool not in this map is not path-checked.
_FILE_TOOL_PATH_ARG: Mapping[str, str] = MappingProxyType({
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
})

# BEP 19 §3.8: the name BOS's loopback MCP server takes in `mcp_servers` — Codex's name for it too, and
# the one the server reports for itself (`Server("bos-tools", …)` in mcp_egress.py). The CLI names each
# of a server's tools `mcp__<server>__<tool>`, each part rewritten by its `wn` (`Fa` in the CLI 2.1.281
# source; `_cli_tool_name` replicates it), which leaves this name as it is: BOS's tools are
# `mcp__bos-tools__<tool>` (measured; pinned by test_bos_computes_the_names_the_cli_gives_its_tools_
# against_the_real_cli).
#
# That prefix does not say which server a tool is from. `wn` turns other server names into it too:
# `bos-tools_` (and `bos-tools.`, rewritten to it) into `mcp__bos-tools___…`, `bos-tools__evil` into
# `mcp__bos-tools__evil__…` and `bos-tools..x` into `mcp__bos-tools__x__…` — all but `bos-tools.` pinned by
# the no-strict control of test_another_mcp_servers_tools_are_not_reachable_against_the_real_cli. The
# CLI itself goes by the server name it recorded for each tool (`mcpInfo`, in `qc`), falling back to
# the prefix only when that record is missing. So the hook and `can_use_tool` allow only the exact
# names of the tools BOS granted (`_mcp_egress`), not the prefix. What stays ambiguous is a granted
# name that, rewritten, starts with `_` or contains `__`: BOS's `a__b` is also what a server named
# `bos-tools__a` would call its tool `b`. Only a server that got past `strict_mcp_config` could use that.
_MCP_SERVER_NAME = "bos-tools"
_MCP_TOOL_PREFIX = f"mcp__{_MCP_SERVER_NAME}__"


def _cli_tool_name(tool: str) -> str:
    """The name the CLI gives *tool* of BOS's MCP server: ``_MCP_TOOL_PREFIX`` plus the tool's name
    rewritten as the CLI's ``wn`` rewrites it (CLI 2.1.281 source) — every character outside
    ``[a-zA-Z0-9_-]`` becomes ``_``, one per UTF-16 code unit as JavaScript counts them, so a
    character beyond U+FFFF becomes ``__``; and for a name starting ``claude.ai `` runs of ``_`` are
    then collapsed and one at either end trimmed. Pinned against the real CLI, both of those
    included, by ``test_bos_computes_the_names_the_cli_gives_its_tools_against_the_real_cli``."""
    name = re.sub(r"[^a-zA-Z0-9_-]", lambda m: "__" if ord(m.group()) > 0xFFFF else "_", tool)
    if tool.startswith("claude.ai "):
        name = re.sub(r"_+", "_", name).strip("_")
    return _MCP_TOOL_PREFIX + name


# BEP 19 §3.5.3, fact 6 (§3.5.3's numbering): the CLI's own "Sandbox disabled" warning on stderr,
# the substring the tripwire watches for under `workspace-write` (`_sandbox_tripwire`).
_SANDBOX_DISABLED = "Sandbox disabled"


class _SandboxDisabledError(RuntimeError):
    """Raised when the tripwire sees the CLI's "Sandbox disabled" warning under ``workspace-write``
    (BEP 19 §3.5.3). Its own type, so ``run()`` reports it as the confinement failure it is rather
    than wrapping it as a generic vendor failure."""


# BEP 19 §3.5.3: the variable that makes each client's settings its own. The CLI's bash
# sandbox guards the path of the inline `--settings` against writes: <temp dir>/claude-
# <uid>/claude-settings-<hash of the settings>.json. Read from the CLI 2.1.281 source:
# when that path is missing, the CLI has bwrap mount /dev/null there, which leaves an
# empty file on the host, and removes the file once its own sandboxed commands are done;
# when the path exists, the CLI binds the file that is there and leaves it alone.
# Measured: the empty file is there while a sandboxed command runs and gone once that CLI
# has exited, and it outlives another CLI that used it. So CLIs sent byte-identical
# settings share one file, and when its creator removes it just as another CLI's bwrap
# starts, that command fails with bwrap's "Can't find source path". The failure is per
# command and intermittent — a later command, finding the path missing, re-creates it —
# but real under concurrency: with identical settings, 4 of 128 sandboxed commands failed
# across four runs of four concurrent CLIs, and fact 6b failed in 5 and 7 of 24 runs under
# 4-way parallel load; with a nonce, none did. BOS starts a CLI per turn and every
# `workspace-write` agent sends the same sandbox dict, so each client's settings carry a
# fresh value of this variable in their `env`, which the CLI sets for its session and
# nothing reads. test_each_clients_cli_binds_a_settings_file_of_its_own pins the file and
# its name against the real CLI. The nonce covers only this path: the sandbox's other mount
# points, under `<cwd>/.claude/`, are named by the repository and shared by agents that share
# a cwd, and they race the same way (3 of 180 commands with six CLIs in one cwd). That is
# documented and accepted, not locked around (BEP 19 §3.5.3).
_SETTINGS_NONCE_VAR = "BOS_SETTINGS_NONCE"

# BEP 19 §3.5.3: which settings files the CLI loads. BOS always sends the list — left at
# None the SDK sends no `--setting-sources` and every source loads (fact 9) — and by default
# it is empty, the SDK's own "isolation mode": no user, project or local settings file. Not
# no settings at all: the CLI always loads managed settings (the machine administrator's
# policy, which outranks BOS's own `--settings` and can override its sandbox or carry an
# apiKeyHelper) and BOS's own flag settings. That policy sits legitimately above BOS.
#
# Deny by default, because a settings file is code. Measured against CLI 2.1.281 with a
# hostile repo, in a workspace nobody had trusted: under `project` the repo's command hooks
# (SessionStart and PreToolUse) and its apiKeyHelper ran on the host outside the bash
# sandbox at every permission level, and `local` (.claude/settings.local.json) did the same
# (test_a_host_that_opts_into_repo_settings_runs_the_repos_commands); the settings `env`
# took effect too (measured, not pinned). None of them is a tool call, so neither
# `permission` nor the sandbox reaches them. Hooks and apiKeyHelper are only the measured
# part of a longer list of settings that run commands, and what a settings file can do is
# the CLI's to extend; loading no repo settings is the one answer that does not depend on
# keeping that list current. Under the default none of it ran, and the CLI loaded no
# CLAUDE.md (test_by_default_nothing_the_repo_authors_runs_or_reaches_the_model).
#
# A host may still opt in; construction then logs one WARNING naming the consequence. The
# PreToolUse hook (not built yet) must then deny the agent's own writes under `.claude/`,
# or an agent could plant settings for its next turn: the Write tool reached `can_use_tool`
# for .claude/settings.json and created it when allowed (measured, not pinned). A repo's
# .mcp.json stays inert either way, because `strict_mcp_config` is always sent.
_SETTING_SOURCES: tuple[str, ...] = get_args(SettingSource)
_DEFAULT_SETTING_SOURCES: tuple[SettingSource, ...] = ()
# The sources that load settings from the workspace itself, and so from the repo.
_REPO_SETTING_SOURCES = {"project": ".claude/settings.json", "local": ".claude/settings.local.json"}

# BEP 19 §3.12: the environment BOS inherits is trusted operator configuration — the SDK
# hands the CLI BOS's whole os.environ and cannot unset a variable — except where a variable
# would load what BOS's default leaves out (§3.5.3). Those are overridden in every client's
# `env`, with the value that switches each off. Enumerated from the CLI 2.1.281 source (the
# version test_what_was_read_from_the_cli_source_is_pinned_to_its_version pins): the
# variables it reads whose names put them on its plugin, hook, settings, MCP, skill or agent
# loading paths, each then read where it is used. Each value is checked against that
# variable's parser there, since an override can only switch a variable off if the CLI reads
# the value as off: `M.bool` counts a value as set only when, trimmed and lower-cased, it is
# 1, true, yes or on; `M.triBool` reads 0, false, no and off as an explicit false; `M.str`
# reads an empty value as unset; a few are read raw, by the same rule as `M.bool`. None of
# these variables is read as set by its mere presence, which an override could not undo —
# the SDK cannot unset a variable — and BOS would have to refuse instead.
_INHERITED_ENV_OVERRIDES: Mapping[str, str] = MappingProxyType({
    # Measured against the real CLI with BOS's options; tests pin each one:
    # loads plugin folders as inline plugins, whose hooks then ran at `read-only`.
    "CLAUDE_CODE_PLUGIN_DIRS": "",
    # with CLAUDE_CODE_SESSION_KIND=bg, adds allow and deny rules and working directories:
    # an out-of-root Write then ran at `read-only` without `can_use_tool` being asked.
    "CLAUDE_BG_SESSION_PERMISSION_RULES": "",
    # adds working directories: a Read outside `cwd` then went unasked.
    "CLAUDE_RELAUNCH_SESSION_ADD_DIRS": "",
    # loads CLAUDE.md and rules files from those added directories.
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "",
    # an opt-out, and on unless a settings file says otherwise: the CLI loads the operator's
    # auto-memory index from <CLAUDE_CONFIG_DIR>/projects/<slug>/memory/ into every turn, as
    # instructions, and tells the model to write there. This switches the loading and the
    # instructions off; the CLI still lets a `read-only` agent's Write into that directory
    # through unasked, which BOS's PreToolUse hook is to deny (BEP 19 §3.5.3). A settings
    # file cannot turn it back on over this: the variable is read first.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    # Read from the source and not measured, each overridden because BOS loads no plugins,
    # skills or other MCP servers by default:
    # extra roots the CLI searches for installed plugins (whether a plugin found there loads
    # without a settings file enabling it was not measured).
    "CLAUDE_CODE_PLUGIN_SEED_DIR": "",
    # fetch the account's plugins and skills at startup (needs an account to measure); the
    # third is a second trigger for both.
    "CLAUDE_CODE_SYNC_PLUGINS": "",
    "CLAUDE_CODE_SYNC_SKILLS": "",
    "CLAUDE_CODE_SYNC_SESSION_REFS": "",
    # opt-out: the claude.ai account's MCP connectors load unless this is false (needs a
    # claude.ai login to measure). The CLI's own confined-evaluation environment sets it so.
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    # under the subscription login, wires in the Claude in Chrome MCP server, which drives the
    # operator's browser, past `strict_mcp_config` and with its tools pre-allowed (needs a
    # claude.ai login and the extension to measure). An explicit false is read before the
    # global config's `claudeInChromeDefaultEnabled`, so that cannot turn it back on either.
    "CLAUDE_CODE_ENABLE_CFC": "0",
    # Read from the source and not measured, found by checking this list against the CLI's
    # whole environment catalog (see the closing note below). Each decides something BOS
    # owns: what a resumed session does with a stopped turn, the tools, agents and sessions
    # beyond the twenty tools the confinement was measured against, what BOS reads back.
    # when a session is resumed, carries on a turn that was interrupted in it instead of the
    # turn BOS asked for, and BOS interrupts turns on purpose, on timeout and on stop (BEP 19
    # §3.10.2); with it, a permission left pending is adopted without asking.
    "CLAUDE_CODE_RESUME_INTERRUPTED_TURN": "",
    "CLAUDE_CODE_ADOPT_UNDERIVABLE_PARKED_PERMISSION": "",
    # offer tools beyond the twenty: one that proposes and saves skills, and the advisor.
    "CLAUDE_CODE_SKILL_PROPOSALS": "",
    "CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL": "",
    # start agents or sessions of their own: agent teams, observer agents and forked
    # subagents (an explicit false, since that parser is three-way).
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "",
    "CLAUDE_CODE_EXPERIMENTAL_OBSERVER_AGENTS": "",
    "CLAUDE_CODE_FORK_SUBAGENT": "0",
    # the variables behind two fields `native_options` refuses, `forward_subagent_text` and
    # `enable_file_checkpointing`, for the reasons given there.
    "CLAUDE_CODE_FORWARD_SUBAGENT_TEXT": "",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "",
    # upgrades a package manager's Claude Code install on the host — a change outside
    # `permission`, to a CLI BOS does not run (it runs the one bundled with the SDK).
    "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE": "",
    # a second gate for memory context in the CLI's reminders; auto-memory is off above.
    "SYSTEM_REMINDER_MEMORY_CONTEXT": "",
    # Found directly (Task 9, BEP 19 §3.7), not by the catalog check above: renaming a
    # directory trips none of that check's loading/triggering verbs. Read from source, the
    # project-dir resolver `Ce(e, s)`: `(s.CLAUDE_CONFIG_DIR ? m$r(s.CLAUDE_CODE_PROJECT_DIR_NAME)
    # : void 0) ?? uC(r)` — when `CLAUDE_CONFIG_DIR` is set, a `CLAUDE_CODE_PROJECT_DIR_NAME`
    # matching `^[A-Za-z0-9_-]{1,64}$` (and not a reserved Windows device name) renames the
    # project folder the CLI writes its transcript under, from the sanitized `cwd` to this
    # value; `m$r` reads an empty value as unset, same as `M.str` elsewhere in this table.
    # `get_session_messages()`'s own project-dir lookup (`_get_project_dir`, claude-agent-sdk's
    # `_internal/sessions.py`) never reads it — only `CLAUDE_CONFIG_DIR` and `cwd` — so a
    # transcript the CLI wrote under an inherited value would sit in a directory
    # `native_messages()` (§3.7) never looks in. Switched off so the CLI's write and BOS's
    # read agree on the project directory's name.
    "CLAUDE_CODE_PROJECT_DIR_NAME": "",
    # Not an inherited loader: this pins the tool surface BOS's confinement (§3.5.3, `_TOOL_LEVELS`)
    # and MCP egress (§3.8) were measured against — every tool offered up front. The CLI turns tool
    # search ON by default on a first-party Anthropic host (i.e. in production under the subscription
    # login) and OFF when `ANTHROPIC_BASE_URL` points elsewhere (read from source: `Cot()`, `Qg()`),
    # which is why every test (fake base URL) ran with it off. With it on, the CLI offers a
    # `DeferredToolPlaceholder` the classification does not know (the hook denies it, so confinement
    # still holds); BOS's own MCP tools were still offered directly under BOS's `tools=` (measured,
    # not pinned). `false` yields the standard full surface: measured (20 tools, no placeholder, no
    # warning), and it overrides an inherited `true`. Read raw / `M.str`; the CLI's `Cot()` maps it to
    # "standard". Measured against CLI 2.1.281; pinned by
    # test_bos_pins_tool_search_off_so_the_full_tool_surface_is_offered.
    "ENABLE_TOOL_SEARCH": "false",
})
# Read on those paths and left alone, because under BOS's default each is inert or loads
# nothing from outside the session:
# - compiled out of this build, their readers returning nothing: CLAUDE_CODE_MANAGED_
#   SETTINGS_PATH and CLAUDE_CODE_REMOTE_SETTINGS_PATH; with no reader at all:
#   CLAUDE_CODE_MOCK_REMOTE_SETTINGS, ALLOW_ANT_COMPUTER_USE_MCP, CLAUDE_CODE_ENABLE_DESIGN_
#   SYNC and CLAUDE_CODE_AUTO_MODE_EXTERNAL_PERMISSIONS.
# - where the CLI keeps its own login and global config: CLAUDE_CONFIG_DIR and
#   CLAUDE_SECURESTORAGE_CONFIG_DIR. What they hold that the default excludes stays excluded.
# - CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER, which runs a proxy-auth helper only a settings file
#   names — under the default, only managed settings, the administrator's policy.
# - the operator's shaping of each API request, which loads nothing and reaches nothing on
#   the host: CLAUDE_CODE_EXTRA_BODY and CLAUDE_CODE_EXTRA_METADATA.
# - git's own variables (GIT_CONFIG_GLOBAL, GIT_CONFIG_COUNT and the rest): the operator's git
#   configuration, which the git the CLI runs honours like any other.
# - relocations of what only a settings file enables: CLAUDE_CODE_PLUGIN_CACHE_DIR, and
#   CLAUDE_CODE_USE_COWORK_PLUGINS, which renames the user settings file and plugin cache.
# - acting only on plugins a settings file enables — how and when those install, refresh,
#   update or are watched: CLAUDE_CODE_SYNC_PLUGIN_INSTALL, CLAUDE_CODE_ENABLE_BACKGROUND_
#   PLUGIN_REFRESH, FORCE_AUTOUPDATE_PLUGINS and CLAUDE_CODE_PLUGIN_DIR_WATCH.
# - background-session mode and trust: CLAUDE_CODE_SESSION_KIND and CLAUDE_BG_WORKSPACE_
#   TRUSTED. Under the default no repo settings load for trust to gate.
# - feature switches, which change what the CLI offers rather than load anything:
#   CLAUDE_CODE_ENABLE_FUNCTION_HOOKS, CLAUDE_CODE_WORKFLOWS, CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT,
#   CLAUDE_CODE_COORDINATOR_EXTRA_TOOLS (coordinator mode only), and CLAUDE_CODE_WEB_FETCH_
#   AGENT, which changes how the WebFetch the CLI already offers does its work.
# - CLAUDE_CODE_BRIDGE_CHILD_MACHINE_SETTINGS, read in an ordinary session too, but only by
#   Claude in Chrome's gate, after CLAUDE_CODE_ENABLE_CFC's explicit false has closed it.
# - read only by modes BOS never starts: the other bridge-child variables, the
#   self-hosted runner (SELF_HOSTED_RUNNER_*, its lifecycle hooks included), the
#   environment-delivered workflow subcommand (CLAUDE_REMOTE_WORKFLOW_SCRIPT and _ARGS), and
#   the `claude agents` view (CLAUDE_AGENTS_SELECT, CLAUDE_CODE_AGENT); and the Remote
#   Control worker, whose early hydrate, agent proxy and directory sync (CLAUDE_CODE_REMOTE,
#   the CCR_* and CLAUDE_CODE_DIR_SYNC_* families) start only under `--sdk-url`, a flag the
#   SDK never sends. Inherited alone, CLAUDE_CODE_REMOTE loads nothing the default excludes.
# - the rest of the CLI's memory features, which relocate or extend the auto-memory switched
#   off above: CLAUDE_MEMORY_STORES, CLAUDE_CODE_REMOTE_MEMORY_DIR, CLAUDE_COWORK_MEMORY_*
#   and CLAUDE_CODE_POST_TURN_MEMORY*.
# - CLAUDE_AGENT_SDK_MCP_NO_PREFIX, which renames the tools of in-process SDK MCP servers
#   only, and BOS's MCP server is an HTTP one (§3.8).
# - restrictions only: CLAUDE_CODE_DISABLE_* and CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS*.
# The list was then checked against the CLI's whole environment catalog, the 1030 names its
# environment module declares: each of the 225 whose names put them on a loading path or
# read as a trigger (sync, install, update, fetch, enable, load) was classified, its use
# sites read wherever it could load, widen, fetch or bill. Those that matter are named
# above; the rest are timeouts, sizes, caches, telemetry, display settings, TLS and tool
# paths, further restrictions, and internals of the modes named above.

# BEP 19 §3.10.3: under `auth = "subscription"`, each of these in BOS's environment makes the
# CLI stop using the subscription login — the CLI inherits that environment whole (§3.12) —
# in one of three ways, which each message names: it moves the run onto another credential or
# provider, which bills that credential or provider; it switches the login off; or it moves
# where the CLI looks for credentials. Enumerated from the CLI 2.1.281 source, from the two
# functions that decide it: the provider resolver (`He()`) and the predicate for whether the
# claude.ai login is used at all (`Ec()`). Refused whenever set, which is conservative where
# the CLI is stricter: it reads the provider flags as booleans, starts OIDC federation only
# with both of its variables, and uses a profile only if the profile store holds one.
# Examined and not refused: CLAUDE_CODE_OAUTH_TOKEN, a subscription token (what `claude
# setup-token` makes for a headless login); ANTHROPIC_BASE_URL, which changes where requests
# go but is not among `Ec()`'s inputs; and the per-provider credentials and targets
# (AWS_BEARER_TOKEN_BEDROCK, ANTHROPIC_VERTEX_PROJECT_ID and the like), which the CLI reads
# only once a flag below has chosen that provider.
_OTHER_CREDENTIAL = "bills another credential in place of the subscription login"
_SUBSCRIPTION_BYPASS_VARS: Mapping[str, str] = MappingProxyType({
    # `Ec()`: a first-party credential used instead of the login.
    "ANTHROPIC_API_KEY": _OTHER_CREDENTIAL,
    "ANTHROPIC_AUTH_TOKEN": _OTHER_CREDENTIAL,
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": _OTHER_CREDENTIAL,
    # `Ec()`: an `ant` profile, or OIDC federation, in place of the login.
    "ANTHROPIC_PROFILE": "selects an `ant` profile, whose credentials the CLI then bills in place of the login",
    "ANTHROPIC_CONFIG_DIR": "moves where the CLI looks for `ant` profiles, whose credentials it can use in place "
    "of the login",
    "ANTHROPIC_FEDERATION_RULE_ID": "with ANTHROPIC_ORGANIZATION_ID, starts OIDC federation, billed to that "
    "organization in place of the login",
    "ANTHROPIC_ORGANIZATION_ID": "with ANTHROPIC_FEDERATION_RULE_ID, starts OIDC federation, billed to that "
    "organization in place of the login",
    # `Ec()`: requests through a local socket, and bare mode, which ignores the login.
    "ANTHROPIC_UNIX_SOCKET": "sends requests through a local socket, billed to whatever credential serves it",
    "CLAUDE_CODE_SIMPLE": "puts the CLI in bare mode, which does not use the login at all",
    # `He()`: another provider, billed by that provider; the names are the CLI's own labels
    # (its `FR` map).
    "CLAUDE_CODE_USE_BEDROCK": "moves the run to Amazon Bedrock, billed there instead",
    "CLAUDE_CODE_USE_VERTEX": "moves the run to Google Vertex AI, billed there instead",
    "CLAUDE_CODE_USE_FOUNDRY": "moves the run to Microsoft Foundry, billed there instead",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "moves the run to Claude Platform on AWS, billed there instead",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "moves the run to Claude Platform on Google Cloud, billed there instead",
    "CLAUDE_CODE_USE_MANTLE": "moves the run to Amazon Bedrock (Mantle), billed there instead",
    "CLAUDE_CODE_USE_GATEWAY": "moves the run to a Cloud gateway, billed to the gateway's credential",
})

# BEP 19 §3.10.3: a route that is a file, not a variable. The CLI reads an API key from this
# path whenever CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR is unset, with nothing gating the read,
# and bills it in place of the login (read from the CLI 2.1.281 source: `y()`, `M_()`). It
# exists on Claude Code's own remote hosts, whose user is `claude`; anywhere else it should
# not. Its sibling `.oauth_token` is a subscription token, excluded for the reason
# CLAUDE_CODE_OAUTH_TOKEN is.
_WELL_KNOWN_API_KEY_FILE = Path("/home/claude/.claude/remote/.api_key")

# BEP 19 §8.2: the administrator's enterprise MCP config, beside managed settings. While a valid
# one exists the CLI refuses `--strict-mcp-config`, which BOS sends on every client, and while a
# broken one exists it drops every server BOS passes (read from the CLI 2.1.281 source: `Jdr`,
# `CYt`, and `_f` for the directory; not measured — that means writing the administrator's
# directory).
_MANAGED_SETTINGS_DIRS = {"darwin": "/Library/Application Support/ClaudeCode", "win32": r"C:\Program Files\ClaudeCode"}
_MANAGED_MCP_FILE = Path(_MANAGED_SETTINGS_DIRS.get(sys.platform, "/etc/claude-code")) / "managed-mcp.json"

# BEP 19 §3.4.1.4: the root CLAUDE.md BOS reads and appends when the CLI would not load it —
# that is, when `setting_sources` has no `project`. The cap is where Claude Code itself calls
# a memory file large: it warns once one passes about 5% of the context window, with a floor
# of 40,000 characters (read from the CLI 2.1.281 source). BOS truncates there rather than
# warn and send it all, because it pays for the file in the system prompt of every turn.
# Counted in bytes, so it never admits more than 40,000 characters.
_CLAUDE_MD_MAX_BYTES = 40_000
_CLAUDE_MD_HEADING = "# CLAUDE.md in the working directory"
_CLAUDE_MD_TRUNCATED = f"[BOS truncated this CLAUDE.md at {_CLAUDE_MD_MAX_BYTES} bytes.]"
# The CLI's own switches for the memory loading BOS's read stands in for, so BOS's read obeys
# them too: its memory gate (`aH()` in the CLI 2.1.281 source) is off under the first, under
# safe mode, and under bare mode when no directory is added — and `add_dirs` is refused below,
# as is the `--bare` flag, through `extra_args`. The first is also how a host keeps repository
# text out of the prompt, with no BOS key of its own.
_CLAUDE_MD_SWITCHES = ("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE")
# The CLI's rule for each (`Oe`): set only when the value, lower-cased and trimmed, is 1, true,
# yes or on. Trimmed as JavaScript's `trim()` trims, which is not Python's `strip()`.
_CLI_TRUE = frozenset({"1", "true", "yes", "on"})
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

# macOS: the binary the CLI runs every sandboxed command through (read from the CLI
# 2.1.281 source; no macOS host has measured it).
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Linux: the commands the CLI's dependency check looks up on PATH (unless a settings file
# names `sandbox.bwrapPath` or `sandbox.socatPath`, which BOS does not send), and the
# package that provides each — the CLI's own message says `apt install bubblewrap socat`.
_LINUX_SANDBOX_TOOLS = {"bwrap": "bubblewrap", "socat": "socat"}

# BEP 19 §3.4: every field of `ClaudeAgentOptions`, in exactly one of the three sets
# below. `ClaudeAgentOptions` is a dataclass, so unlike Codex's untyped config surface its
# fields can be enumerated, and `native_options` is an allowlist over them rather than a
# probed denylist. test_native_options_classification_partitions_the_vendor_fields is the
# enumeration's source: it partitions `dataclasses.fields(ClaudeAgentOptions)`, so an SDK
# release that adds a field fails CI until someone classifies it here.
#
# Set by BOS, or reserved for what BOS is to set: a host value would take BOS's place.
# Which fields `native_options` may carry is decided here, not when BOS starts setting one.
_BOS_OWNED: Mapping[str, str] = MappingProxyType({
    "cwd": "set by BOS from `cwd`, the confinement root (BEP 19 §3.5.1)",
    "permission_mode": "set by BOS from `permission` (BEP 19 §3.5)",
    "sandbox": "set by BOS from `permission` (BEP 19 §3.5.3)",
    "settings": "set by BOS: each client's settings carry a nonce that keeps its sandbox settings file its own "
    "(BEP 19 §3.5.3)",
    "setting_sources": "set by BOS from the `setting_sources` key (BEP 19 §3.5.3)",
    "system_prompt": "set by BOS from `system_prompt` or `base_instructions` (BEP 19 §3.4.1)",
    "max_turns": "set by BOS from `max_iterations` (BEP 19 §3.9)",
    "model": "set by BOS from `model` (BEP 19 §3.4)",
    "hooks": "set by BOS: the PreToolUse hook that gates every tool call per `permission` (BEP 19 §3.5.3)",
    "can_use_tool": "set by BOS: the backstop answer to a permission prompt (BEP 19 §3.5.3)",
    "stderr": "set by BOS per turn: the sandbox tripwire on the CLI's stderr under workspace-write (BEP 19 §3.5.3)",
    "tools": "set by BOS: the deny-by-default allowlist of tools offered to the model per `permission` (BEP 19 §3.5.3)",
    "disallowed_tools": "reserved for BOS: `tools` is the allowlist BOS uses instead (BEP 19 §3.5.3)",
    "allowed_tools": "reserved for BOS: an entry pre-approves a tool before `can_use_tool` is asked "
    "(BEP 19 §3.5.3); BOS sends none, since `can_use_tool` answers for its own MCP tools (§3.8)",
    "mcp_servers": "set by BOS: its own loopback MCP server, when `mcp_tools` names a tool the host has (BEP 19 §3.8)",
    "strict_mcp_config": "set by BOS, always True, so the CLI loads only the MCP servers BOS passes — never a "
    "repo's .mcp.json or the operator's own servers, even under a `setting_sources` opt-in (BEP 19 §3.5.3, §3.8)",
    "env": "set by BOS, to switch off inherited variables that would load what its default leaves out "
    "(BEP 19 §3.12), and to carry the bearer token of its MCP server (§3.8)",
    "effort": "reserved for BOS, to set per turn from `llm_args['reasoning_effort']` (BEP 19 §3.9)",
    "resume": "reserved for BOS, to resume the chat's own native session (BEP 19 §3.6)",
    "output_format": "reserved for BOS, to set per turn from `schema=` (BEP 19 §3.9)",
    "extra_args": "set by BOS, to have the CLI echo each message BOS sends it (`--replay-user-messages`, BEP 19 "
    "§3.9); a host's flags would ride on the same CLI command, `--dangerously-skip-permissions` among them",
})

_SESSION = "decides which native session a turn continues, or its id, and BOS owns that mapping (BEP 19 §3.6)"
_STORE = "mirrors transcripts to an external store that `resume` can read back from, which is deferred (BEP 19 §8.2)"
_STREAM = "changes what BOS reads back from the CLI, which is BOS's end of the connection, not a model setting"

# Neither set by BOS nor safe to forward.
_REFUSED: Mapping[str, str] = MappingProxyType({
    "cli_path": "replaces the bundled CLI 2.1.281, the version BOS's confinement was measured against (BEP 19 §3.5.3)",
    "add_dirs": "widens what the agent may reach beyond `cwd` (BEP 19 §3.5.3)",
    "plugins": "loads commands, agents, skills and hooks into the session",
    "agents": "defines subagents with their own tools, MCP servers and permission mode",
    "permission_prompt_tool_name": "routes permission prompts to an MCP tool instead of `can_use_tool`, which the "
    "SDK wires through this same field",
    "skills": "pre-approves the Skill tool through `allowed_tools`, before `can_use_tool` is asked",
    "user": "is not a session label: the SDK hands it to `anyio.open_process(user=...)`, so it is the OS user the "
    "CLI runs as",
    "verbatim_prompts": "decides whether an `@path` in a prompt makes the CLI read that file itself, which is "
    "confinement's business, not a tuning knob",
    "enable_file_checkpointing": "backs files up for `ClaudeSDKClient.rewind_files()`, which BOS never calls",
    "debug_stderr": "is deprecated, and the SDK no longer reads it",
    "continue_conversation": _SESSION,
    "session_id": _SESSION,
    "fork_session": _SESSION,
    "resume_session_at": _SESSION,
    "resume_drops_turn": _SESSION,
    "session_store": _STORE,
    "session_store_flush": _STORE,
    "load_timeout_ms": _STORE,
    "include_partial_messages": _STREAM,
    "include_hook_events": _STREAM,
    "forward_subagent_text": _STREAM,
    "max_buffer_size": _STREAM,
})

# Forwarded verbatim: settings about the model and its budget, none of which reaches
# what the agent may do.
_ALLOWED = frozenset({"fallback_model", "max_budget_usd", "betas", "thinking", "max_thinking_tokens", "task_budget"})


def _bash_sandbox_unavailable(platform: str) -> str | None:
    """Why the CLI's bash sandbox cannot start on *platform* (this host's
    ``sys.platform``), naming what to install — or None.

    It mirrors the part of the CLI's own dependency check that depends on what is
    installed, read from the CLI 2.1.281 source: on Linux, ``bwrap`` and ``socat`` looked
    up on PATH (fact 6 pins that a missing ``socat`` is named; a settings file can point
    the CLI at other paths, and BOS sends none). The CLI also refuses WSL 1
    and needs ripgrep, which it supplies itself; this check leaves both to it. On macOS the
    CLI checks nothing; BOS looks for ``/usr/bin/sandbox-exec``, which the CLI runs every
    sandboxed command through. Windows is refused as policy, not measurement (BEP 19 §8.1).

    This is the early, friendly error. It can only defer a failure to the CLI, never permit
    one: under ``failIfUnavailable`` the CLI refuses to start wherever its own check finds
    the sandbox unavailable (fact 6c), whatever this one concluded — so if the CLI's list of
    dependencies grows, a host this passes is refused at its first turn instead of here. It
    checks presence, as the CLI does, not usability: a ``bwrap`` installed but unable to
    create a sandbox passes both, and what the CLI then does is not measured (BEP 19 §8.2).
    The stderr tripwire BEP 19 §3.5.3 describes, for a degrade path the key does not cover,
    is not built yet.
    """
    if platform == "win32":
        return (
            "the SDK documents Claude Code's bash sandbox for macOS and Linux only, and BOS refuses "
            "`workspace-write` on Windows until there is a measured enforcement story (BEP 19 §8.1)"
        )
    if platform == "darwin":
        return None if os.access(_SANDBOX_EXEC, os.X_OK) else f"{_SANDBOX_EXEC} is missing"
    if platform.startswith("linux"):
        missing = [tool for tool in _LINUX_SANDBOX_TOOLS if shutil.which(tool) is None]
        if not missing:
            return None
        packages = " and ".join(_LINUX_SANDBOX_TOOLS[tool] for tool in missing)
        return f"{' and '.join(missing)} not found on PATH — install {packages} (e.g. `apt install {packages}`)"
    return f"the SDK documents Claude Code's bash sandbox for macOS and Linux only, not {platform!r}"


def _refuse_native_options(native_options: Mapping[str, Any]) -> None:
    """Raise unless every key of *native_options* is in ``_ALLOWED``, naming each other
    key and why it is refused (BEP 19 §3.4; Review Focus 4)."""
    refused = [
        f"{key!r} {_BOS_OWNED.get(key) or _REFUSED.get(key) or 'is not a field of ClaudeAgentOptions'}"
        for key in sorted(set(native_options) - _ALLOWED, key=str)
    ]
    if refused:
        raise ValueError(
            f"`native_options` for the {_RUNTIME!r} runtime may carry only {sorted(_ALLOWED)}, an allowlist over "
            f"the SDK's own ClaudeAgentOptions fields. Refused: {'; '.join(refused)}."
        )


def _within(path: Path, root: Path) -> bool:
    """True when *path* is *root* itself or sits under it. Both must already be resolved."""
    return path == root or root in path.parents


def _cli_config_dir() -> Path:
    """The CLI's own config directory, resolved, as the child will compute it: ``CLAUDE_CONFIG_DIR``
    or ``<HOME>/.claude`` (BEP 19 §3.12; read from the CLI 2.1.281 source, `ko`). BOS never
    overrides either variable (both are on §3.12's left-alone list), so the child inherits this
    process's environment for them, which is what the hook reads. Its ``projects/<slug>/memory/``
    is the directory the CLI carves out of the permission check — a `read-only` agent's `Write`
    there landed unasked when it was offered (measured in review; BEP 19 §3.5.3, §8.2(g)) — so the
    hook denies the whole directory, which matters when it sits inside `cwd` (e.g. `cwd` is ``$HOME``);
    when it sits outside `cwd`, the out-of-root check already denies a write there. Pinned by
    ``test_the_hook_denies_the_config_dir_even_inside_cwd``."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config) if config else Path(os.environ.get("HOME") or Path.home()) / ".claude"
    return base.resolve()


def _pretooluse_deny(reason: str) -> HookJSONOutput:
    """A PreToolUse hook result that denies the call and shows the model *reason* (BEP 19 §3.5.3).
    Never an *allow* decision: an allow would skip ``can_use_tool``, so a call the hook does not
    deny returns ``{}`` (no decision) instead (the SDK's ``types.py`` documents that an allow
    shadows ``can_use_tool``)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _system_prompt(config: ExternalAgentConfig, claude_md: str | None) -> str | SystemPromptPreset:
    """BEP 19 §3.4.1: ``system_prompt`` is appended to Claude Code's own prompt and
    ``base_instructions`` replaces it. With neither, the preset goes out bare — never
    ``None``, which the SDK turns into an *empty* prompt (§3.4.1.3, fact 8).

    *claude_md* is the working directory's CLAUDE.md as BOS read it (``_root_claude_md``),
    appended after the agent's own instructions under a heading of its own. Never to
    ``base_instructions``: that key means the host owns the whole prompt, and BOS adding the
    repository's text to it would break that (§3.4.1.4)."""
    if config.base_instructions is not None:
        return config.base_instructions
    parts = [config.system_prompt] if config.system_prompt is not None else []
    if claude_md is not None:
        parts.append(f"{_CLAUDE_MD_HEADING}\n\n{claude_md}")
    if not parts:
        return {"type": "preset", "preset": "claude_code"}
    return {"type": "preset", "preset": "claude_code", "append": "\n\n".join(parts)}


def _root_claude_md(cwd: Path, warned: set[tuple[Path, str]]) -> str | None:
    """The agent's own ``<cwd>/CLAUDE.md`` as text, for BOS to append to its instructions — or
    None when there is none, or it is refused (BEP 19 §3.4.1.4).

    Only that one file. Not Claude Code's memory loading: no ``@import`` is expanded, and no
    CLAUDE.md in a subdirectory, no CLAUDE.local.md and no user-level file is read.

    Contained, because BOS reads this in its own process, outside every confinement, and sends
    it to the model provider: a repository whose CLAUDE.md is a symlink to ~/.ssh/id_rsa must
    not get the key read and shipped. The path is resolved, symlinks included, and must be a
    regular file inside *cwd*, which ``parse_external_config`` has already resolved. On Linux
    and macOS the check runs again on the file actually opened, since a process in the same
    directory could swap a directory for a symlink in between; elsewhere only the first check
    runs. The open follows no final symlink and does not block, so a FIFO swapped in cannot
    hang the turn. Refused with a WARNING that names the path.

    Each WARNING is logged once per agent for each path and reason — *warned* is the agent's
    record of those — since the file is read every turn, and a misconfigured one should not
    log a line per turn forever (BEP 19 §4.3).

    Read per turn, because each turn starts its own CLI, which reads memory afresh when it
    starts; an agent that edits the file changes what its next turn sees.
    """
    candidate = cwd / "CLAUDE.md"

    def warn(reason: str, message: str, *args: object) -> None:
        if (candidate, reason) not in warned:
            warned.add((candidate, reason))
            logger.warning(message, *args)

    if not os.path.lexists(candidate):
        return None
    resolved = candidate.resolve()
    if cwd not in resolved.parents or not resolved.is_file():
        warn(
            "refused",
            "%s resolves to %s, which is not a regular file inside %s; BOS did not read it",
            candidate,
            resolved,
            cwd,
        )
        return None
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        warn("unopenable", "%s could not be opened (%s); BOS did not read it", candidate, exc)
        return None
    try:
        # Checked on the bare descriptor: `os.fdopen` itself raises for a directory.
        opened = _opened_path(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or (opened is not None and cwd not in opened.parents):
            warn(
                "swapped",
                "%s was no longer a regular file inside %s once BOS opened it (it opened %s); BOS did not read it",
                candidate,
                cwd,
                opened,
            )
            return None
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(_CLAUDE_MD_MAX_BYTES + 1)
    finally:
        os.close(fd)
    text = data[:_CLAUDE_MD_MAX_BYTES].decode("utf-8", errors="replace")
    if len(data) > _CLAUDE_MD_MAX_BYTES:
        warn(
            "oversized",
            "%s is over %d bytes; BOS appended only the first %d",
            candidate,
            _CLAUDE_MD_MAX_BYTES,
            _CLAUDE_MD_MAX_BYTES,
        )
        text += f"\n\n{_CLAUDE_MD_TRUNCATED}"
    return text


def _opened_path(fd: int) -> Path | None:
    """Where the kernel says open file *fd* is — on Linux and macOS — or None elsewhere."""
    try:
        if sys.platform.startswith("linux"):
            return Path(os.readlink(f"/proc/self/fd/{fd}"))
        if sys.platform == "darwin":
            import fcntl

            return Path(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode())
    except OSError:
        return None
    return None


def _content_to_claude_prompt(content: MessageContent) -> str | list[dict[str, Any]]:
    """BOS ``MessageContent`` -> the content of the user message a turn sends the CLI
    (BEP 19 §3.9).

    A plain string passes straight through: ``ClaudeSDKClient.query`` wraps it in a user
    message itself. A list of BOS parts becomes a list of Messages API content blocks, part by
    part:

    - ``TextPart`` -> a text block.
    - ``ImagePart`` -> an image block (:func:`_image_source`).
    - ``FilePart`` -> its path, or url, in a text block — ``[attachment: <value> (<mime_type>)]``,
      the text BOS's own default provider sends for one — so the model reads the file with its
      own tools, as it reads any other (BEP 19 §3.5.3 bounds where those reach).

    ``content_as_parts`` validates as well as normalizes, so every part reaching the loop below
    is one of exactly ``text``/``image``/``file``.
    """
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content_as_parts(content):
        if part["type"] == "text":
            blocks.append({"type": "text", "text": part["text"]})
        elif part["type"] == "image":
            blocks.append({"type": "image", "source": _image_source(part["source"])})
        elif part["type"] == "file":
            blocks.append({"type": "text", "text": f"[attachment: {part['source']['value']} ({part['mime_type']})]"})
    return blocks


def _image_source(source: Mapping[str, Any]) -> dict[str, Any]:
    """An ``ImagePart``'s source as a Messages API image source.

    A url goes as a url, except a ``data:`` url, which the API takes only as base64. A path is
    read and base64-encoded by BOS in its own process, as BOS's own default provider reads one
    (``image_source_to_model_url``, which also refuses a file that is missing or not an image
    by its name): the path comes from the host, and an image block is what BEP 19 §3.9 sends,
    not a path for the model to open with file tools that §3.5.3 bounds. Measured against the
    CLI 2.1.281, not pinned: both forms reach the model unchanged, and for a base64 image the
    CLI also saves a copy under its own temporary directory and adds a text block naming that
    path.
    """
    url = image_source_to_model_url(source)
    if not url.startswith("data:"):
        return {"type": "url", "url": url}
    header, _, data = url.partition(",")
    if not header.endswith(";base64"):
        raise ValueError(
            f"{_RUNTIME} runtime: an image sent as a data: URL must be base64-encoded, which is the only form the "
            f"Messages API takes; got one whose header is {header!r}."
        )
    return {"type": "base64", "media_type": header.removeprefix("data:").split(";")[0], "data": data}


async def _user_message(
    content: str | list[dict[str, Any]], message_uuid: str | None = None
) -> AsyncIterator[dict[str, Any]]:
    """*content* as one user message, shaped as ``ClaudeSDKClient.query`` sends a str prompt:
    ``query`` takes content blocks, and a uuid, only as a stream of such messages. The CLI's
    ``--replay-user-messages`` echo of the message carries *message_uuid* back (measured against
    CLI 2.1.281), which is how BOS follows a mid-turn message (``ClaudeCodeAgent._stream``)."""
    message: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    if message_uuid is not None:
        message["uuid"] = message_uuid
    yield message


def _usage(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    """``ResultMessage.usage`` under the keys ``CodexAgent`` reports, each meaning what it
    means there (BEP 19 §3.9), or None when the CLI reported none.

    Measured against the CLI 2.1.281, not pinned: the counts are the turn's, summed over its
    model calls, and not the session's. The Anthropic counts are split three ways — ``input_tokens`` leaves
    out what was read from the cache (``cache_read_input_tokens``) and what was written to it
    (``cache_creation_input_tokens``) — while the ``input_tokens`` Codex reports is OpenAI's,
    every input token with the cached ones among them. So ``input_tokens`` here is the sum of
    all three, ``cached_input_tokens`` and ``cache_write_input_tokens`` are the cache reads and
    writes, ``total_tokens`` is input plus output, and ``reasoning_output_tokens`` is the part
    of ``output_tokens`` the CLI reports as thinking (``output_tokens_details``), present only
    when it reports one.

    No counterpart, so not carried: ``server_tool_use`` (web search and fetch request counts),
    ``service_tier``, ``cache_creation`` (the cache writes split by lifetime),
    ``inference_geo``, ``iterations`` and ``speed``.
    """
    if not usage:
        return None
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    input_tokens = (usage.get("input_tokens") or 0) + cache_read + cache_write
    output_tokens = usage.get("output_tokens") or 0
    mapped = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    if isinstance(thinking, int):
        mapped["reasoning_output_tokens"] = thinking
    return mapped


def _add_usage(total: dict[str, int] | None, usage: dict[str, int] | None) -> dict[str, int] | None:
    """Two of :func:`_usage`'s dicts added key by key, for an attempt that ran more than one
    native turn: a mid-turn message the CLI answered in a turn of its own (``ClaudeCodeAgent._stream``).
    A key is the sum over the native turns that reported it."""
    if total is None or usage is None:
        return total or usage
    return {key: total.get(key, 0) + usage.get(key, 0) for key in total.keys() | usage.keys()}


def _visible_text(content: Any) -> str | None:
    """The BOS-visible text of one transcript message's raw Anthropic ``content`` — a plain
    string as-is (how the CLI records a plain-text turn; measured against CLI 2.1.281), or the
    concatenation of a content-block list's ``text`` blocks (``ClaudeCodeAgent.native_messages``,
    BEP 19 §3.7).

    ``tool_use`` and ``tool_result`` blocks, and anything else that is not ``text``, carry no BOS
    message content and are dropped. ``None`` when nothing survives that — a message that is only
    such blocks (the assistant's own tool call, or the user turn carrying nothing but its matching
    ``tool_result``) is not a message BOS projects, the same rule §3.7 applies to Codex's
    ``tool_use``/``tool_result`` ``ThreadItem``s.

    Multiple surviving ``text`` blocks in one message are not observed against CLI 2.1.281 —
    Task 7 found it streams each content block of a reply as its own transcript entry, so a
    reply that both spoke and called a tool is more than one entry, not one entry with two
    blocks — but nothing guarantees that of every transcript this method may be asked to read
    (an older CLI's, or one written by a different client entirely), so more than one is joined
    rather than only the first being kept.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    if not texts:
        return None
    return texts[0] if len(texts) == 1 else "\n\n".join(texts)


# BEP 19 §3.9: the name of the CLI's own synthetic tool for `output_format` (measured against
# CLI 2.1.281; pinned by test_structured_output_arrives_from_the_synthetic_tool_against_the_real_cli
# in tests/test_claude_code_runtime.py). Its `ToolUseBlock`/`ToolResultBlock` are the CLI answering
# itself, not a tool the agent chose, so `_events_for_message` excludes them from the `tool` event
# stream rather than reporting a call BOS never dispatched and a result BOS never received.
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

# How long a turn (or aclose(), across all of them) waits for the CLI to answer an interrupt, and
# then for the interrupted turn to stream its last message, before giving up on it — mirrors
# codex._INTERRUPT_GRACE_SECONDS and Agent._ABANDON_TEARDOWN_SECONDS (agent.py), for the same
# reason: a CLI that delays or ignores the request must not hold a stop, a timeout or aclose() open
# (BEP 19 §3.10.2). Measured against CLI 2.1.281, not pinned: it answers in milliseconds, and the
# interrupted turn's ResultMessage follows at once.
_INTERRUPT_GRACE_SECONDS = 2.0

# The outer bound aclose() puts on draining every in-flight turn. Must stay above `_settle`'s worst
# case (3 x the grace: the interrupt, the drain, the wait after the cancel), or aclose() reports
# turns that are winding down normally as stuck.
_ACLOSE_GRACE_SECONDS = 10.0

# BEP 19 §3.10.2: the `terminal_reason` of a turn the CLI ended because it was interrupted —
# "aborted_tools" while a tool ran, "aborted_streaming" while the model answered (both measured
# against CLI 2.1.281; the first pinned against the real CLI by
# test_request_stop_mid_turn_stops_the_cli_and_its_tool_against_the_real_cli). The SDK's own
# `ResultMessage.terminal_reason` docstring names the same two. Such a turn ends as an error result
# — subtype "error_during_execution", `is_error`, no `result` (measured) — so it is kept as a
# partial answer only when BOS itself asked for the stop; any other is raised like every error.
_INTERRUPTED = frozenset({"aborted_tools", "aborted_streaming"})

# Every await in this module that crosses to the CLI, and what bounds it, stated once so the next
# reader does not redo the audit:
#
# - `client.connect()` — the CLI starting and, on a resumed chat, loading its session — carries
#   `timeout_seconds` itself (the TimeoutError names the phase, "at startup"). The SDK bounds it
#   too (read from the claude-agent-sdk 0.2.159 source): its version probe (`claude -v`) starts and
#   is read under `anyio.fail_after(2)` — the probe's terminate() and wait() in its `finally` run
#   outside that scope, unbounded but on a process that exits on SIGTERM — and its initialize
#   handshake runs under `fail_after(max(CLAUDE_CODE_STREAM_CLOSE_TIMEOUT / 1000, 60))` seconds.
#   It is not raced against a stop: a stop that lands while it runs ends the call with the shutdown
#   marker once it returns, before any turn starts.
# - Each attempt's `query()`, every `receive_response()` round and each mid-turn message's
#   `query()` run in the attempt's stream task (`_stream`), inside `_run_attempt`'s
#   `asyncio.timeout(timeout_seconds)` AND raced against the stop flag, so `_settle`'s cancel bounds
#   them even when `timeout_seconds` is None. Per attempt: a schema retry gets a window of its own,
#   so a call with retries can take a multiple of it, while a mid-turn message's own native turn,
#   read as part of its attempt, runs inside that attempt's window.
# - The interrupt (`_interrupt`) is a control request the CLI answers at its leisure, and the
#   SDK's own wait for the answer is 60 seconds, so the whole of it — the lock ahead of it
#   included — is bounded by `_INTERRUPT_GRACE_SECONDS`. `_settle`, which sends it and then waits
#   for the stream, takes three graces at worst, then abandons a stream task that will not end.
# - `client.disconnect()` bounds itself for the options BOS sends, which name no session store and
#   no in-process MCP server (read from source, not measured): `Query.close()` cancels its reader
#   and awaits `SubprocessCLITransport.close()`, which bounds every await in it — the write lock
#   (5s), the CLI's exit once stdin closes (5s), SIGTERM and a wait (5s), SIGKILL and a wait (5s) —
#   ~20s at worst, the SDK's own figure. BOS never wraps it in a timeout, because an asyncio
#   cancellation delivered inside it skips that escalation (the SDK's docstring says so), and
#   `_close` runs it in a task of its own, shielded, so a caller's cancellation cannot land there.
# - `aclose()` waits for the in-flight turns for `_ACLOSE_GRACE_SECONDS`, then closes their clients
#   with `_close` regardless, all at once, so the SDK's bound is paid once, not per turn.
# - `_mcp_egress`'s `await server.start()` crosses nothing vendor-side: on the agent's first turn,
#   before that turn's CLI starts, it binds BOS's own loopback socket in this process — or returns at
#   once, when another agent already has. Unbounded, as in `CodexAgent`: a bind to 127.0.0.1:0
#   depends on nothing that can be slow, and it holds `_mcp_lock`, which only this agent's other
#   first turns wait on.
# - With `timeout_seconds = None` nothing above is bounded by it, because the caller declined a
#   deadline; the stop flag, `aclose()` and the SDK's own bounds still hold.
# - Host code the stream task awaits, the event sink and the interrupt callback, is not the CLI's
#   and has no bound of its own; a stream task parked there that swallows `_settle`'s cancel is
#   abandoned, which is why `aclose()` bounds its own wait.
# - `native_messages`' `get_session_messages()` call (Task 9, BEP 19 §3.7) is not in the list
#   above, and not for the reason the rest are missing from it: it never crosses to the CLI at
#   all. It is a plain filesystem read in this process — open, read, `json.loads` over the
#   session's own `.jsonl` — not a request across a pipe to a child that can choose not to
#   answer, so there is nothing here for `timeout_seconds` to guard against and it carries no
#   `wait_for` (contrast `CodexAgent.native_messages`'s `thread.read()`, which is the same
#   unbounded RPC path every other vendor call there is). It still runs in `asyncio.to_thread`,
#   since a file read is blocking I/O and this method must not block the event loop for it.


class _Turn:
    """One turn's client and what BOS knows of the turn, shared by ``run()``, the task streaming
    the attempt, and ``aclose()`` (BEP 19 §3.10.1: one client per turn)."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.task: asyncio.Task[ResultMessage] | None = None  # the attempt streaming now
        self.closing: asyncio.Task[None] | None = None  # the one disconnect, see `_close`
        # uuids of the mid-turn messages sent and not yet echoed back (`_stream`).
        self.pending: set[str] = set()
        # The CLI may be running a turn, or holding a message BOS sent: set by each query(), and
        # cleared by a ResultMessage with nothing pending or by an interrupt.
        self.busy = False
        # BOS is ending the turn: the stream task polls and sends nothing more, and returns at the
        # next ResultMessage whatever is pending.
        self.stopping = False
        # Orders a mid-turn message's write ahead of an interrupt's, see `_interrupt`.
        self.lock = asyncio.Lock()
        # BEP 19 §3.9: `tool_use_id` -> name, for every `ToolUseBlock` not yet matched by its
        # `ToolResultBlock` — see `_events_for_message`. One map for the whole call, a schema
        # retry's rounds included, since it is the same connected client and ids do not repeat.
        self.pending_tools: dict[str, str] = {}
        # Per attempt, from its top-level AssistantMessages (`_stream`).
        self.answer_uuid: str | None = None
        self.last_text: str | None = None
        self.usage: dict[str, int] | None = None
        # BEP 19 §3.5.3: the "Sandbox disabled" stderr lines the tripwire caught (workspace-write
        # only). The stderr callback appends here from the SDK's stderr reader task; `_stream` checks
        # it once per message and ends the turn if it is non-empty. A list, not a flag, so the raise
        # can quote the CLI's own line.
        self.sandbox_tripped: list[str] = []
        # BEP 19 §3.5.3, §8.2 (R14): the per-turn TMPDIR directory BOS created for this client's CLI
        # under workspace-write (None otherwise), which moves the sandbox's writable temp root off the
        # shared /tmp/claude-<uid>. `_teardown` removes exactly this path once the client has
        # disconnected.
        self.tmpdir: Path | None = None
        # BEP 19 §3.8: this turn's options named BOS's MCP server and the CLI's init message has not yet
        # been read for its status (`ClaudeCodeAgent._warn_unless_mcp_connected`); cleared at the first.
        self.mcp_unchecked = False


def _reads_past(turn: _Turn, result: ResultMessage) -> bool:
    """Whether BOS reads on past *result* into the native turn the CLI runs next for a mid-turn
    message still pending (BEP 19 §3.9; ``ClaudeCodeAgent._stream``). Not once BOS is stopping the
    turn, whose interrupt dropped that message, and never past an error result: it ends the attempt
    as it would with nothing pending — a max-turns result closes the turn, any other raises — and
    the teardown's ``cancel_queued`` interrupt drops the message, which it logs as dropped."""
    return bool(turn.pending) and not turn.stopping and not result.is_error


def _consume(task: asyncio.Task[Any]) -> None:
    """Read a finished *task*'s exception, so asyncio does not log it as never retrieved."""
    if not task.cancelled():
        task.exception()


class ClaudeCodeAgent:
    """``ExternalRuntime`` adapter over the Claude Code vendor SDK (BEP 19 §3.4, §3.5).

    One instance per ``create_agent`` call. It holds no client: each turn builds its own
    ``ClaudeSDKClient``, and with it a CLI child, from :meth:`_options` (§3.10.1).
    Constructing an agent must never start the CLI.
    """

    def __init__(
        self,
        *,
        kind: str,
        cfg: Mapping[str, Any],
        chat_store: ChatStore | None,
        workspace: Path,
        mcp: Callable[[], Any],
        structured_validator: StructuredValidator,
    ) -> None:
        self._kind = kind
        self._chat_store = chat_store
        self._mcp = mcp
        self._structured_validator = structured_validator
        cfg = dict(cfg)
        # BEP 19 §3.9: taken out before the shared parser, which drops it for both
        # runtimes — right only for Codex, which has no counterpart.
        max_iterations = cfg.pop("max_iterations", None)
        self._config = parse_external_config(cfg, runtime=_RUNTIME, workspace=Path(workspace))

        if max_iterations is not None and (
            not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1
        ):
            raise ValueError(
                f"`max_iterations` must be a positive integer (Claude Code's `max_turns`); got {max_iterations!r}. "
                f"0 would not mean zero: the SDK sends no limit at all for it."
            )
        self._max_turns: int | None = max_iterations

        setting_sources = cfg.get("setting_sources", _DEFAULT_SETTING_SOURCES)
        if not isinstance(setting_sources, (list, tuple)) or not all(s in _SETTING_SOURCES for s in setting_sources):
            raise ValueError(
                f"`setting_sources` must be a list drawn from {list(_SETTING_SOURCES)}; got {setting_sources!r}. "
                f"Each names settings the CLI loads, and a bare string is not a one-item list."
            )
        self._setting_sources = cast(list[SettingSource], list(setting_sources))

        _refuse_native_options(self._config.native_options)

        # BEP 19 §3.10.3. Read now, from this process's environment and filesystem as they
        # stand, since the CLI inherits both. An empty variable is not set: every read of
        # these in the CLI 2.1.281 source trims or tests truthiness, or both.
        if self._config.auth == "subscription":
            routes = [
                f"{name} is set, which {why}" for name, why in _SUBSCRIPTION_BYPASS_VARS.items() if os.environ.get(name)
            ]
            # Not Path.exists(), which raises where a parent cannot be searched; the CLI,
            # running as this same user, could not read the file there either.
            if os.path.exists(_WELL_KNOWN_API_KEY_FILE):
                routes.append(f"{_WELL_KNOWN_API_KEY_FILE} exists, and the CLI reads an API key from it and bills that")
            if routes:
                raise ValueError(
                    f'`auth = "subscription"` (the default), but the Claude Code CLI inherits this process\'s '
                    f"environment and filesystem, where BOS found what can take a run off the subscription login, "
                    f"each refused whatever its value: {'; '.join(routes)}. "
                    f'Remove {"it" if len(routes) == 1 else "them"}, or set `auth = "api_key"` to run that way '
                    f"deliberately."
                )

        if self._config.permission == "workspace-write" and (reason := _bash_sandbox_unavailable(sys.platform)):
            raise ValueError(
                f'`permission = "workspace-write"` is refused on this host: {reason}. It needs Claude Code\'s bash '
                f"sandbox, and BOS refuses rather than let bash run unsandboxed (BEP 19 §3.5.3)."
            )

        # BEP 19 §8.2: with an enterprise MCP config present every turn would fail at the CLI's own
        # startup, so it is refused once, here. `os.path.exists`, as for the key file above.
        if os.path.exists(_MANAGED_MCP_FILE):
            raise ValueError(
                f"{_MANAGED_MCP_FILE} exists: this host's administrator gives an enterprise MCP config exclusive "
                f"control of MCP servers, and the Claude Code CLI then refuses `--strict-mcp-config`, which BOS "
                f"sends on every client to keep a repository's MCP servers out (BEP 19 §3.5.3, §8.2). BOS does not "
                f"work around the administrator's policy; run this agent on a host without that file."
            )

        # Last, so that a config refused above does not also warn about an agent never built.
        if repo_files := [path for source, path in _REPO_SETTING_SOURCES.items() if source in self._setting_sources]:
            logger.warning(
                "%r agent: `setting_sources` = %s loads %s from the repository. Whatever the repository "
                "configures there takes effect, and the hooks, apiKeyHelper and other commands named there run "
                "on the host, outside the bash sandbox and outside `permission` (BEP 19 §3.5.3).",
                kind,
                self._setting_sources,
                " and ".join(repo_files),
            )

        self._stop_requested = asyncio.Event()
        self._claude_md_warned: set[tuple[Path, str]] = set()  # `_root_claude_md`'s once-only WARNINGs
        # BEP 19 §3.8: BOS's MCP server for this agent — its url, the agent's bearer token and the CLI's
        # names for the tools it granted — or None when there is nothing to serve; decided once, on the
        # first turn, by `_mcp_egress`, under the lock.
        self._mcp_grant: tuple[str, str, frozenset[str]] | None = None
        self._mcp_built = False
        self._mcp_lock = asyncio.Lock()
        # chat_id -> its running turn (BEP 19 §3.10.1's busy guard). Reserved (None) the instant
        # run() is entered, before any await could let a second call for the chat_id in; filled in
        # once the turn's CLI has started, so aclose() has a turn to wait on and a client to close.
        self._in_flight: dict[str, _Turn | None] = {}

    @property
    def name(self) -> str:
        return self._kind

    @property
    def resolved_config(self) -> Mapping[str, Any]:
        """The *resolved* config ``boscli inspect`` reads (BEP 19 §3.3.1): an absolute
        ``cwd``, a validated ``permission`` and what BOS derives from them, not the raw
        input the constructor was handed."""
        return MappingProxyType({
            "external_runtime": self._config.runtime,
            "cwd": str(self._config.cwd),
            "permission": self._config.permission,
            "permission_mode": _PERMISSION_MODES[self._config.permission],
            "setting_sources": list(self._setting_sources),
            "max_turns": self._max_turns,
            "system_prompt": self._config.system_prompt,
            "base_instructions": self._config.base_instructions,
            "model": self._config.model,
            "auth": self._config.auth,
            "timeout_seconds": self._config.timeout_seconds,
            "mcp_tools": list(self._config.mcp_tools),
            # The subset of `mcp_tools` the host has no `ep_tool` for, from the
            # predicate the MCP server's own warn-and-skip uses; computed here
            # because an agent that has run no turn has no server to ask.
            "mcp_tools_unavailable": list(unregistered_tools(self._config.mcp_tools)),
            "native_options": dict(self._config.native_options),
        })

    def _offered_tools(self) -> list[str]:
        """The built-in tools BOS offers the model at this agent's ``permission``, the CLI-level
        allowlist ``tools=`` carries (BEP 19 §3.5.3). Deny-by-default over the CLI's offered list
        (``_TOOL_LEVELS``): an excluded tool is never offered and is not callable even if the model
        asks (pinned), nor re-addable through a trusted repo's ``permissions.allow`` (measured while
        building the hook, not pinned)."""
        return sorted(name for name, levels in _TOOL_LEVELS.items() if self._config.permission in levels)

    def _hook(self, mcp_tools: frozenset[str] = frozenset()) -> HookCallback:
        """The ``PreToolUse`` gate on ``HookMatcher(matcher=None)`` (BEP 19 §3.5.3). It fires once
        per tool call in every permission mode, and its deny holds even where ``can_use_tool`` is
        never asked — under ``bypassPermissions`` and against a trusted repo's own allow rules
        (facts 2 and 4) — which is why the confinement rests on it and not on ``can_use_tool``.

        It never blocks or awaits anything slow: it runs inside the CLI's tool path. It returns a
        decision synchronously, from data captured when the turn's options were built.

        - *mcp_tools*, the CLI's names for the tools BOS's MCP server granted this agent
          (:meth:`_mcp_egress`), pass with no decision, at every level: ``permission`` bounds the
          filesystem, not the tools the host exposed through ``mcp_tools`` (§3.5, §3.8). Exact names,
          not the ``mcp__bos-tools__`` prefix, which other servers' names can produce
          (``_MCP_TOOL_PREFIX``). Where the mode asks, ``can_use_tool`` then allows them.
        - Any other tool outside this level's allowlist is denied — the per-call half of
          deny-by-default, a backstop should ``tools=`` ever fail to exclude it (e.g. a subagent, or
          a future CLI). This is what denies the cross-session tools at every level (R10) and every
          mutating tool under ``read-only``, and any MCP tool not in *mcp_tools*, at every level:
          ``tools=`` does not filter MCP tools, and ``strict_mcp_config`` keeps other servers out,
          which this backs; at ``full-access``, where ``can_use_tool`` is not asked, it is the only
          layer that would stop one. A granted name some other server could also produce is the
          residual ``_MCP_TOOL_PREFIX`` states.
        - Under ``full-access`` nothing else is checked: it confines nothing on the filesystem.
        - For a file tool (``_FILE_TOOL_PATH_ARG``) the path argument is resolved — relative to
          ``cwd``, then through ``Path.resolve()``, so ``..``, a symlink and ``~`` (already
          expanded by the CLI, but resolved again here) cannot escape — and the call is denied when
          the result leaves ``cwd``, sits inside the CLI's config directory (``read-only`` and
          ``workspace-write``; the memory-dir carve-out, §8.2(g)), or, when a host has opted into the
          repo's settings, sits inside ``<cwd>/.claude`` (so an agent cannot plant settings for its
          own next turn). Each deny carries a model-facing reason.
        """
        permission = self._config.permission
        cwd = self._config.cwd
        allowed = frozenset(self._offered_tools()) | mcp_tools
        config_dir = _cli_config_dir()
        dot_claude = (cwd / ".claude") if any(s in _REPO_SETTING_SOURCES for s in self._setting_sources) else None
        may_still = {
            "read-only": "You can still read files inside the workspace with Read.",
            "workspace-write": "You can still read, write and edit files inside the workspace, and run sandboxed Bash.",
            "full-access": "",
        }[permission]

        async def hook(input_data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
            data = cast(Mapping[str, Any], input_data)
            tool = data.get("tool_name", "")
            # No decision for the CLI's synthetic StructuredOutput tool, which the CLI offers from
            # `output_format` and answers itself (§3.9) — not a tool the agent chose, and its input is
            # re-validated by BOS locally (BEP 12), so it is not the confinement's business. BOS's own
            # MCP tools are in `allowed` by their exact names, and pass the checks below with none.
            if tool == _STRUCTURED_OUTPUT_TOOL:
                return {}
            if tool not in allowed:
                return _pretooluse_deny(
                    f"The {tool!r} tool is not available to this Claude Code agent, which BOS runs under "
                    f"{permission!r} permission. {may_still}".rstrip()
                )
            if permission == "full-access":
                return {}
            arg = _FILE_TOOL_PATH_ARG.get(tool)
            if arg is None:
                return {}
            raw = (data.get("tool_input") or {}).get(arg)
            if not isinstance(raw, str) or not raw:
                return {}
            target = Path(raw)
            target = (target if target.is_absolute() else cwd / target).resolve()
            if not _within(target, cwd):
                return _pretooluse_deny(
                    f"{tool} of {str(target)!r} was denied: it is outside the workspace root {str(cwd)!r}. {may_still}"
                )
            if _within(target, config_dir):
                return _pretooluse_deny(
                    f"{tool} of {str(target)!r} was denied: it is inside the CLI's own configuration directory "
                    f"{str(config_dir)!r}, which is off-limits. Work inside the workspace {str(cwd)!r} instead."
                )
            if dot_claude is not None and _within(target, dot_claude):
                return _pretooluse_deny(
                    f"{tool} of {str(target)!r} was denied: it is inside {str(dot_claude)!r}, which configures the "
                    f"agent itself and is off-limits. Work elsewhere inside the workspace {str(cwd)!r}."
                )
            return {}

        return hook

    def _can_use_tool(self, mcp_tools: frozenset[str] = frozenset()) -> CanUseTool | None:
        """The answer to a permission prompt, never the gate (BEP 19 §3.5.3, fact 3). The CLI asks
        it only where it would otherwise prompt a user — a subset the mode, the sandbox and a trusted
        repo's allow rules decide first (the matrix in §3.5.3). The confinement is the hook and the OS
        sandbox, both of which run *before* this callback: the hook fires on every tool call and
        denies anything outside the level's allowlist or outside ``cwd`` (facts 2, 4), and where it
        denies, the CLI never asks this callback. So anything that reaches here has already been
        vetted — an allowlisted tool whose path (for a file tool) the hook confined to ``cwd`` — and
        this only answers, at once and without awaiting anyone (§3.5.4):

        - *mcp_tools*, the CLI's names for the tools BOS's MCP server granted this agent, are
          allowed, by exact name as in :meth:`_hook`: the host chose them through ``mcp_tools``, and
          the server scopes every call to this agent's grant by its bearer token (§3.8). They are the
          calls measured to reach here under BOS's options: ``default`` and ``acceptEdits`` ask about
          each. Any other MCP tool is denied — behind the hook, which has already denied it.
        - A tool in this level's allowlist is allowed: the hook already confined it (a file tool's
          path to ``cwd``), and ``Bash`` under ``workspace-write`` is confined by the OS sandbox
          (fact 6b). Answering "yes" here is what lets an in-root edit the mode did not auto-allow,
          or a sandboxed ``Bash`` command, go ahead.
        - Anything else is denied — a defensive backstop, since the hook should have denied a
          non-allowlisted tool before it ever reached here; BOS has no one to ask.

        Under ``full-access`` (``bypassPermissions``) the CLI never consults it (facts 2, 6b; the
        SDK's own advisory), so BOS sets it to ``None`` there rather than install a callback the SDK
        would warn is shadowed.
        """
        permission = self._config.permission
        if permission == "full-access":
            return None
        allowed = frozenset(self._offered_tools()) | mcp_tools

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
            if tool_name in allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=(
                    f"{tool_name} is not permitted for this Claude Code agent, which BOS runs under "
                    f"{permission!r} permission. This decision is fixed by policy and no one can be asked to change it."
                )
            )

        return can_use_tool

    def _sandbox_tripwire(self, tripped: list[str], chat_id: str, turn_id: str) -> Callable[[str], None]:
        """A ``stderr`` callback that watches for the CLI's "Sandbox disabled" warning under
        ``workspace-write`` (BEP 19 §3.5.3). It is the third layer behind Task 3's construction
        check and ``failIfUnavailable`` (fact 6c turns a missing dependency into a startup refusal),
        for a degrade path the key does not cover — the day the CLI's dependency list changes and it
        warns-and-runs-unsandboxed (fact 6) rather than refusing. It only records the line and logs;
        ``_stream`` ends the turn (interrupt, then raise). Synchronous and fast, because it runs on
        the SDK's stderr reader task (``subprocess_cli.py``'s ``_handle_stderr``), which is the sole
        consumer of the child's stderr and must not be blocked."""

        def stderr(line: str) -> None:
            if _SANDBOX_DISABLED in line:
                tripped.append(line)
                logger.error(
                    "%s runtime %r: the CLI reported the bash sandbox disabled under workspace-write during turn %r "
                    "on chat %r; ending the turn. Line: %s",
                    self._config.runtime,
                    self._kind,
                    turn_id,
                    chat_id,
                    line.strip(),
                )

        return stderr

    async def _mcp_egress(self) -> tuple[str, str, frozenset[str]] | None:
        """BOS's loopback MCP server for this agent — its url, this agent's bearer token, and the
        CLI's names for the tools it granted (``_cli_tool_name``), which are exactly what the hook and
        ``can_use_tool`` let through — or None when there is nothing for it to serve (BEP 19 §3.8).

        Built once, on the first turn, by ``CodexAgent._mcp_egress_config``'s rules. "Nothing to
        serve" is an empty ``mcp_tools``, or one whose every name the host has no ``ep_tool`` for,
        and neither calls the ``mcp`` accessor, which builds the server when called (§3.1).
        ``unregistered_tools`` answers without a server, so this warns once per unknown name, naming
        the agent, and hands ``register_agent`` only names that resolve, so it does not warn a
        second time (§7.5). A build that raises is retried by the next turn, its warnings with it,
        as Codex's is.

        One grant per agent; each client's options name it under a bearer variable of their own
        (:meth:`_options`). Under a lock, so two first turns at once register one grant, not two.
        """
        async with self._mcp_lock:
            if not self._mcp_built:
                unavailable = unregistered_tools(self._config.mcp_tools)
                for name in unavailable:
                    logger.warning(
                        "%s runtime %r: mcp_tools names %r, which is not a registered tool; it is not exposed.",
                        self._config.runtime,
                        self._kind,
                        name,
                    )
                if available := [name for name in self._config.mcp_tools if name not in unavailable]:
                    server = self._mcp()
                    await server.start()
                    token = server.register_agent(self._kind, available)
                    self._mcp_grant = (server.url, token, frozenset(map(_cli_tool_name, available)))
                self._mcp_built = True
            return self._mcp_grant

    def _options(self, mcp: tuple[str, str, frozenset[str]] | None = None) -> ClaudeAgentOptions:
        """The options for one client of this agent, apart from the per-turn fields
        (``resume``, a turn's own model or effort, ``output_format``, and the stderr tripwire, which
        needs the turn's own state so ``run()`` sets it). *mcp* is what :meth:`_mcp_egress` returned:
        given, the client is pointed at BOS's MCP server (§3.8); None, it names no MCP server.

        Built afresh for every client, never cached: each call's settings carry a new nonce
        (``_SETTINGS_NONCE_VAR``), each call names the MCP bearer under a new variable, and each call
        reads the working directory's CLAUDE.md again (``_root_claude_md``).
        """
        # `env` carries `_INHERITED_ENV_OVERRIDES`, and deliberately no `CLAUDE_CODE_SANDBOXED`
        # override. The CLI source reads an inherited one as "trusted", but in everything
        # measured against CLI 2.1.281 it changed nothing (measured, not pinned): the repo's
        # allow rules are gated on the trust recorded in the CLI's config, which that variable
        # does not reach, and with `project` loaded, the repo's hooks, MCP servers, settings
        # `env` and apiKeyHelper ran without any trust at all.
        config = self._config
        # BEP 19 §3.4.1.4: the CLI loads CLAUDE.md itself under `project`, `base_instructions`
        # means the host owns the whole prompt, and the CLI's own switches for memory files turn
        # BOS's read off too, so only otherwise does BOS read the file.
        reads_claude_md = (
            config.base_instructions is None
            and "project" not in self._setting_sources
            and not any(
                os.environ.get(name, "").lower().strip(_JS_WHITESPACE) in _CLI_TRUE for name in _CLAUDE_MD_SWITCHES
            )
        )
        sandbox = (
            cast(SandboxSettings, dict(_WORKSPACE_WRITE_SANDBOX)) if config.permission == "workspace-write" else None
        )
        env = dict(_INHERITED_ENV_OVERRIDES)
        mcp_servers: dict[str, McpServerConfig] = {}
        mcp_tools: frozenset[str] = frozenset()
        if mcp is not None:
            url, token, mcp_tools = mcp
            # BEP 19 §3.8, fact 7: the SDK puts `mcp_servers` on the CLI's command line, which other
            # local users can read, so the entry carries a placeholder the CLI expands from its own
            # environment, and the token travels only in `env`. The variable is named afresh for each
            # client, for the reason Codex's is: no MCP server's config — the operator's, a
            # repository's — can name it in its own `${VAR}` to be handed the token.
            bearer = f"BOS_MCP_BEARER_{uuid.uuid4().hex}"
            env[bearer] = token
            mcp_servers[_MCP_SERVER_NAME] = {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer ${{{bearer}}}"},
            }
        return ClaudeAgentOptions(
            cwd=config.cwd,
            permission_mode=_PERMISSION_MODES[config.permission],
            sandbox=sandbox,
            settings=json.dumps({"env": {_SETTINGS_NONCE_VAR: uuid.uuid4().hex}}),
            setting_sources=list(self._setting_sources),
            system_prompt=_system_prompt(
                config, _root_claude_md(config.cwd, self._claude_md_warned) if reads_claude_md else None
            ),
            max_turns=self._max_turns,
            model=config.model,
            mcp_servers=mcp_servers,
            # BEP 19 §3.5.3, §3.8: only the servers above. Measured: it keeps out a repository's
            # .mcp.json and the operator's own user- and local-scope servers, which each load under
            # the matching `setting_sources` opt-in without it.
            strict_mcp_config=True,
            # BEP 19 §3.5.3: the deny-by-default confinement. `tools=` is the CLI-level allowlist per
            # `permission`; the PreToolUse hook is the per-call gate (path checks; deny anything
            # outside the allowlist); `can_use_tool` answers what `permission` already decided, and
            # is None under full-access (bypassPermissions never consults it). Both let through the
            # exact names of the MCP tools BOS granted (§3.8), and no other MCP tool.
            tools=self._offered_tools(),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._hook(mcp_tools)])]},
            can_use_tool=self._can_use_tool(mcp_tools),
            env=env,
            # BEP 19 §3.9: the CLI echoes each user message BOS sends it, with the uuid BOS put on
            # it, when it takes that message into a turn — how BOS tells a mid-turn message folded
            # into the running turn from one the CLI will run as a turn of its own (`_stream`). The
            # SDK documents this flag as an `extra_args` value (`ClaudeSDKClient.rewind_files`).
            extra_args={"replay-user-messages": None},
            **config.native_options,
        )

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def ask(
        self,
        chat_id: str,
        content: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: TurnEventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
    ) -> str:
        """Thin wrapper over :meth:`run`, mirroring ``Agent.ask`` (BOS's own agent)."""
        result = await self.run(
            chat_id,
            content,
            interrupt=interrupt,
            ctx_metadata=ctx_metadata,
            llm_args=llm_args,
            event_sink=event_sink,
            turn_id=turn_id,
            commit_observer=commit_observer,
        )
        return str(result.output)

    def _event(
        self,
        *,
        chat_id: str,
        turn_id: str,
        event_type: str,
        phase: str,
        stage: str | None = None,
        detail: str | None = None,
        tool_name: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnEvent:
        """Build one ``TurnEvent``, ``chat_id``/``turn_id``/``agent_name`` filled in exactly as
        ``CodexAgent._event`` fills them (BEP 19 §3.9). ``stage`` is the one field that method has
        no use for — Codex's own mapping never sets it — and Claude Code needs it for exactly one
        event, the ``max_turns`` closure below."""
        return TurnEvent(
            event_type=event_type,
            phase=phase,
            chat_id=chat_id,
            turn_id=turn_id,
            agent_name=self._kind,
            stage=stage,
            detail=detail,
            tool_name=tool_name,
            content=content,
            metadata=dict(metadata or {}),
        )

    def _events_for_message(
        self,
        message: Any,
        pending_tools: dict[str, str],
        *,
        chat_id: str,
        turn_id: str,
        metadata: dict[str, Any] | None,
    ) -> list[TurnEvent]:
        """BEP 19 §3.9's mapping: one message from ``receive_response()`` -> zero or more
        ``TurnEvent``s.

        - A ``ToolUseBlock`` inside an ``AssistantMessage`` -> ``tool``/``start``, its name the
          block's own.
        - A ``TextBlock`` inside an ``AssistantMessage`` -> ``response``/``finish``, carrying the
          block's text. Every assistant text block gets one — there is no commentary/final-answer
          split to read here the way Codex's ``MessagePhase`` gives it one, so a host that wants
          *the* answer reads ``AgentResult.output`` (``ResultMessage.result``, via ``run()``'s
          return value), never this stream.
        - The ``ToolResultBlock`` inside a ``UserMessage`` that matches a pending ``ToolUseBlock``
          by ``tool_use_id`` -> ``tool``/``finish``, or ``tool``/``fail`` when ``is_error`` — using
          the name *pending_tools* recorded when its ``ToolUseBlock`` streamed by, since a result
          block carries no name of its own.
        - A ``ResultMessage`` -> ``turn``/``finish`` — unless the CLI ended the turn on
          ``max_turns`` (``subtype == "error_max_turns"``), in which case it is the same event
          BOS's own ``Agent`` emits at ``_close_with_handoff("max_iterations")`` (``agent.py``):
          ``turn``/``fail``, ``stage`` and ``detail`` both ``max_iteration``, ``content`` the
          static ``MAX_ITERATION_CONTENT`` marker. Of that call's own metadata keys
          (``iteration``, ``max_iterations``, ``handoff``, ``closure_reason``), only two carry a
          meaning here and are added beside whatever ``metadata`` the caller supplied:
          ``max_iterations`` (``self._max_turns``, the CLI's own budget) and ``closure_reason``
          (always ``"max_iterations"``, since nothing hands an external runtime a reason to
          close on ``"shutdown"`` the way ``Agent`` can). ``iteration`` has no counterpart —
          BOS does not see the CLI's own internal turn count — and ``handoff`` is dropped rather
          than always sent as ``False``: no external runtime is ever handed a consolidator, so it
          could never be anything else.

        Everything else is skipped rather than half-mapped, as ``CodexAgent._event_for_notification``
        skips a ``ThreadItem`` variant it has no ``tool`` vocabulary for: a ``SystemMessage``; a
        ``ThinkingBlock`` or server-tool block inside an ``AssistantMessage``; a plain-text
        ``UserMessage`` — the CLI's own one-shot "[structured-output-enforce]" nudge on a schema
        turn (BEP 19 §3.9) arrives this way and needs no special case, since only
        ``ToolResultBlock`` is ever matched inside a ``UserMessage`` at all. The CLI's synthetic
        ``StructuredOutput`` tool call (``_STRUCTURED_OUTPUT_TOOL``) is excluded too, and so is a
        subagent's own stream: every message whose ``parent_tool_use_id`` names the ``Agent`` call
        that started it, since that top-level call's own start and finish already stand for the
        subagent's work, and its inner tools would otherwise surface as the agent's own. The
        synthetic tool is excluded the same deliberate way twice over: its ``ToolUseBlock`` is
        never turned into an event and never added to *pending_tools*, so its ``ToolResultBlock``
        is later found to match no pending id and is skipped exactly as an untracked id would be
        — no check on its name is needed at that end.

        *pending_tools* is threaded in by the caller (``run()``) rather than owned here, so one
        map survives across every message of one ``run()`` call, a schema retry's fresh
        ``receive_response()`` round included — the same round trip that makes a retry re-enter
        this method for its own tool calls, so a turn with retries emits every attempt's events,
        not just the winning one, mirroring ``CodexAgent``'s own retry loop, which re-runs
        ``_run_turn``/``_emit_stream`` in full for every retry because each is a brand-new native
        turn.
        """
        if isinstance(message, (AssistantMessage, UserMessage)) and message.parent_tool_use_id is not None:
            return []
        if isinstance(message, AssistantMessage):
            events: list[TurnEvent] = []
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    if block.name == _STRUCTURED_OUTPUT_TOOL:
                        continue
                    pending_tools[block.id] = block.name
                    events.append(
                        self._event(
                            chat_id=chat_id,
                            turn_id=turn_id,
                            event_type=AgentEventType.tool,
                            phase=TurnEventPhase.start,
                            tool_name=block.name,
                            metadata=metadata,
                        )
                    )
                elif isinstance(block, TextBlock):
                    events.append(
                        self._event(
                            chat_id=chat_id,
                            turn_id=turn_id,
                            event_type=AgentEventType.response,
                            phase=TurnEventPhase.finish,
                            content=block.text,
                            metadata=metadata,
                        )
                    )
            return events
        if isinstance(message, UserMessage):
            blocks = message.content if isinstance(message.content, list) else []
            events = []
            for block in blocks:
                if not isinstance(block, ToolResultBlock):
                    continue
                tool_name = pending_tools.pop(block.tool_use_id, None)
                if tool_name is None:
                    continue
                events.append(
                    self._event(
                        chat_id=chat_id,
                        turn_id=turn_id,
                        event_type=AgentEventType.tool,
                        phase=TurnEventPhase.fail if block.is_error else TurnEventPhase.finish,
                        tool_name=tool_name,
                        metadata=metadata,
                    )
                )
            return events
        if isinstance(message, ResultMessage):
            if message.subtype == "error_max_turns":
                return [
                    self._event(
                        chat_id=chat_id,
                        turn_id=turn_id,
                        event_type=AgentEventType.turn,
                        phase=TurnEventPhase.fail,
                        stage=TurnEventStage.max_iteration,
                        detail=TurnEventDetail.max_iteration,
                        content=MAX_ITERATION_CONTENT,
                        metadata={
                            **(metadata or {}),
                            "max_iterations": self._max_turns,
                            "closure_reason": "max_iterations",
                        },
                    )
                ]
            return [
                self._event(
                    chat_id=chat_id,
                    turn_id=turn_id,
                    event_type=AgentEventType.turn,
                    phase=TurnEventPhase.finish,
                    metadata=metadata,
                )
            ]
        return []

    async def _emit(self, sink: TurnEventSink | None, event: TurnEvent) -> None:
        """Best-effort dispatch to *sink*: a sink that raises must not end the turn, mirroring
        ``CodexAgent._emit_stream`` and ``Agent._emit_event`` (BEP 19 §3.9)."""
        if sink is None:
            return
        try:
            await sink.emit(event)
        except Exception:
            logger.debug("Claude Code event sink emit error", exc_info=True)

    async def _stream(
        self,
        turn: _Turn,
        prompt: str | list[dict[str, Any]],
        *,
        chat_id: str,
        turn_id: str,
        event_sink: TurnEventSink | None,
        ctx_metadata: dict[str, Any] | None,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None,
    ) -> ResultMessage:
        """One native turn attempt: send *prompt*, then read ``receive_response()`` until a
        ``ResultMessage`` it does not read past (:func:`_reads_past`) — one that leaves no mid-turn
        message pending, an error result, or any once BOS is stopping the turn — emitting §3.9's
        ``TurnEvent``s on the way and polling ``interrupt`` (BEP 19 §3.9, §3.10.2), and return that
        ``ResultMessage``.

        ``interrupt`` is polled once per message, as ``Agent._interrupt`` reads it once per
        iteration, but never on a ``ResultMessage``: the poll is destructive (``AgentActor`` pops
        what it returns) and after the ``ResultMessage`` there is no turn to put a message into.

        - **A truthy return is a message for the running turn**, not a stop (``agent.py``: ``Agent``
          merges it into the live context). It goes to the CLI with ``query()`` (:meth:`_steer`),
          and what the CLI does with it was measured against CLI 2.1.281 with the fake Messages
          API: it queues it and folds it into its next model request after a tool call — "within
          the running turn, often alongside the next tool result, rather than as a separate
          conversation turn", in the CLI's own words to the model — but a message that misses the
          turn's last model call is not dropped: the CLI runs it as a native turn of its own right
          after the ``ResultMessage``. So this reads on past a ``ResultMessage`` while a message
          is pending, and that turn becomes part of this one: its events stream, its answer and
          its session entry are the ones ``run()`` reports, and its usage is added. The
          ``ResultMessage`` read past emits no ``turn`` event, because the BOS turn does not end
          there; the one that ends the attempt does, as each schema retry's attempt ends in its
          own. It never reads past an error result (:func:`_reads_past`): that ends the attempt as
          it would with nothing pending, and the teardown drops the message. Which case a
          message is in shows in the stream: under ``--replay-user-messages`` (:meth:`_options`)
          the CLI echoes it, carrying the uuid BOS put on it, when it takes it into a turn — before
          the ``ResultMessage`` when folded in, after it when it runs as its own turn (measured).
        - **Raising ends the turn.** Nothing here catches what the callback raises: it propagates
          out of this task, ``run()``'s teardown tells the CLI to stop, and ``run()`` returns the
          aborted-turn marker for ``AbortTurn``, as ``Agent`` does, and fails the turn for
          anything else.
        - **A falsy return does nothing.**

        With ``turn.stopping`` set BOS is ending the turn, so nothing is polled or sent any more,
        and the next ``ResultMessage`` ends the read whatever is pending: the interrupt
        :meth:`_interrupt` sent carries ``cancel_queued``, so once the CLI has it, it holds nothing
        to run.
        """
        async with turn.lock:  # ahead of any interrupt, which then stops the turn this starts
            turn.busy = True
            turn.answer_uuid = turn.last_text = turn.usage = None
            await turn.client.query(prompt if isinstance(prompt, str) else _user_message(prompt))
        while True:
            await self._raise_if_sandbox_tripped(turn, chat_id=chat_id, turn_id=turn_id)
            result: ResultMessage | None = None
            async for message in turn.client.receive_response():
                # BEP 19 §3.5.3: the tripwire (`_sandbox_tripwire`) may have caught the CLI's "Sandbox
                # disabled" warning on its stderr task while this message streamed. Checked once per
                # message so the turn ends promptly (interrupt, then raise).
                await self._raise_if_sandbox_tripped(turn, chat_id=chat_id, turn_id=turn_id)
                if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                    # Recorded as `native_turn_id`, since the stream names no turn: the turn's last
                    # top-level assistant message, a transcript entry, which `get_session_messages`
                    # returns under this uuid (pinned by
                    # test_a_second_turn_resumes_the_first_by_session_id_against_the_real_cli).
                    # `ResultMessage.uuid` is in no transcript, and the prompt's own entry is not in
                    # the stream (both measured, not pinned).
                    turn.answer_uuid = message.uuid
                    # What a stopped turn keeps (`run()`): the latest non-empty text. The CLI streams each
                    # content block of a reply as a message of its own (measured), so the tool call that
                    # follows a reply's text arrives with none and must not clear it.
                    if text := "".join(block.text for block in message.content if isinstance(block, TextBlock)):
                        turn.last_text = text
                elif isinstance(message, UserMessage) and message.uuid in turn.pending:
                    turn.pending.discard(message.uuid)  # the CLI took this mid-turn message into a turn
                elif isinstance(message, ResultMessage):
                    result = message
                elif isinstance(message, SystemMessage) and message.subtype == "init" and turn.mcp_unchecked:
                    turn.mcp_unchecked = False  # once per turn: every native turn's init repeats it
                    self._warn_unless_mcp_connected(message, chat_id=chat_id, turn_id=turn_id)
                # BEP 19 §3.9: every attempt streams its own events, by being run through this same
                # per-message loop. No sink, as CodexAgent._emit_stream also checks, means nothing
                # to build; nor does a result this read goes on past (see the docstring).
                reads_on = result is not None and message is result and _reads_past(turn, result)
                if event_sink is not None and not reads_on:
                    for event in self._events_for_message(
                        message, turn.pending_tools, chat_id=chat_id, turn_id=turn_id, metadata=ctx_metadata
                    ):
                        await self._emit(event_sink, event)
                if interrupt is not None and result is None and not turn.stopping:
                    if message_for_the_turn := await _apply_async(interrupt, {}):
                        await self._steer(turn, message_for_the_turn, chat_id=chat_id, turn_id=turn_id)
            # Also after the stream ends: the warning can arrive while the CLI is between messages (a
            # tool running), so the per-message check above may not see it before the round closes.
            await self._raise_if_sandbox_tripped(turn, chat_id=chat_id, turn_id=turn_id)
            if result is None:
                raise RuntimeError("the CLI ended the turn without a result")
            turn.usage = _add_usage(turn.usage, _usage(result.usage))
            if not _reads_past(turn, result):
                if not turn.pending:
                    turn.busy = False  # nothing running and nothing queued; else the teardown interrupts
                return result

    def _warn_unless_mcp_connected(self, init: SystemMessage, *, chat_id: str, turn_id: str) -> None:
        """Log a WARNING when the CLI's init message says BOS's MCP server did not connect (BEP 19
        §3.8). The CLI carries on without it either way, so the turn runs with none of the agent's
        ``mcp_tools`` and nothing else says so; this does not fail the turn.

        Measured against CLI 2.1.281: the init message lists each MCP server with its status —
        ``connected``, or ``failed`` when the server refuses the CLI (a 401) — and repeats it for
        every native turn; a server an MCP allow or deny list drops is not listed at all (measured
        with the list in BOS's own flag settings, and in a repository's or the user's settings under a
        ``setting_sources`` opt-in), while the CLI names it only on stderr, which BOS does not read.
        An init message with no server list at all tells BOS nothing, and is not warned about."""
        servers = init.data.get("mcp_servers")
        if not isinstance(servers, list):
            return
        status = next(
            (s.get("status") for s in servers if isinstance(s, dict) and s.get("name") == _MCP_SERVER_NAME),
            None,
        )
        if status == "connected":
            return
        logger.warning(
            "%s runtime %r: the CLI reports BOS's MCP server %r as %s for turn %r on chat %r, so the agent has "
            "none of its mcp_tools this turn",
            self._config.runtime,
            self._kind,
            _MCP_SERVER_NAME,
            repr(status)
            if status is not None
            else "not loaded (an MCP allow or deny list in its settings drops it so)",
            turn_id,
            chat_id,
        )

    async def _steer(self, turn: _Turn, message: dict[str, Any], *, chat_id: str, turn_id: str) -> None:
        """Send a message the ``interrupt`` poll returned into the running turn (BEP 19 §3.9; see
        :meth:`_stream` for what the CLI does with it). Best-effort, but never silent: the poll
        already took the message from the caller's queue and it came from a user, so a message
        BOS cannot send is logged at WARNING, where nothing else records that it existed. The
        message's ``"content"`` is a BOS ``MessageContent``, converted as a turn's own is."""
        content = _content_to_claude_prompt(message.get("content", ""))
        async with turn.lock:
            if turn.stopping:
                why, exc_info = "the turn is being stopped", False
            else:
                message_uuid = str(uuid.uuid4())
                turn.pending.add(message_uuid)
                try:
                    await turn.client.query(_user_message(content, message_uuid))
                    return
                except Exception:
                    turn.pending.discard(message_uuid)
                    why, exc_info = "sending it to the CLI failed", True
        logger.warning(
            "%s runtime %r: a mid-turn message for turn %r on chat %r was dropped: %s",
            self._config.runtime,
            self._kind,
            turn_id,
            chat_id,
            why,
            exc_info=exc_info,
        )

    async def _raise_if_sandbox_tripped(self, turn: _Turn, *, chat_id: str, turn_id: str) -> None:
        """End the turn if the tripwire caught the CLI's "Sandbox disabled" warning under
        ``workspace-write`` (BEP 19 §3.5.3): interrupt the CLI (Task 7's machinery), then raise
        ``_SandboxDisabledError``. A no-op otherwise, so it is cheap to call once per message. The
        interrupt clears ``turn.busy``, so ``run()``'s teardown does not interrupt a second time."""
        if not turn.sandbox_tripped:
            return
        await self._interrupt(turn)
        raise _SandboxDisabledError(
            f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat {chat_id!r} was ended because "
            f"the CLI reported its bash sandbox disabled under workspace-write, where BOS requires it and refuses to "
            f"run bash unsandboxed (BEP 19 §3.5.3): {turn.sandbox_tripped[0].strip()}"
        )

    async def _interrupt(self, turn: _Turn) -> None:
        """Tell the CLI to stop the turn and to drop every message BOS sent that it has not yet
        taken into a turn — bounded by ``_INTERRUPT_GRACE_SECONDS`` and best-effort, since a
        stop's outcome must not depend on it (BEP 19 §3.9, §3.10.2).

        It is the CLI's ``interrupt`` control request with ``cancel_queued``. Without that flag a
        mid-turn message still queued in the CLI outlives the interrupt and runs as a turn of its
        own — right after the interrupted one, and on stdin closing too, when the client is closed
        (both measured against CLI 2.1.281) — which nothing would read. The CLI takes the flag
        (read from its source, where the SDK client bundled with it sends it as
        ``interrupt({cancelQueued: true})``), but claude-agent-sdk 0.2.159's
        ``ClaudeSDKClient.interrupt()`` cannot, so the request goes through the SDK's private
        ``Query._send_control_request``, the method every one of the client's own control
        requests goes through.
        ``test_the_interrupt_reaches_the_cli_through_an_sdk_path_that_still_exists`` fails when
        that path moves, and when ``interrupt()`` gains an argument, which would be the supported
        way instead.

        Under ``turn.lock``, so a mid-turn message already on its way to the CLI is written first
        and dropped with the rest, while one after it finds ``turn.stopping`` set and is not sent.
        """
        turn.stopping = True
        turn.busy = False
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_INTERRUPT_GRACE_SECONDS), turn.lock:
                await turn.client._query._send_control_request({"subtype": "interrupt", "cancel_queued": True})

    async def _settle(self, turn: _Turn, task: asyncio.Task[ResultMessage]) -> ResultMessage:
        """Stop the attempt *task* streams, then give it a bounded window to stream the
        ``ResultMessage`` the CLI ends the turn with, rather than waiting it out (BEP 19 §3.10.2).
        Shared by a stop and a timeout, as ``CodexAgent._settle_interrupted`` is, so a CLI that
        ignores the interrupt is one code path.

        Raises if the stream does not end: there is no result to hand back, and making one up would
        misreport the turn. At worst that is three graces — the interrupt, the drain, and the wait
        after the cancel — and on that path the task is abandoned, not killed, as
        ``Agent._abandon`` does: a stream task parked in host code, the sink or the interrupt
        callback, can swallow a cancel.
        """
        await self._interrupt(turn)
        done, _ = await asyncio.wait({task}, timeout=_INTERRUPT_GRACE_SECONDS)
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.wait({task}, timeout=_INTERRUPT_GRACE_SECONDS)
        # Only so asyncio does not log "exception was never retrieved" for a task that finished in
        # the instant after the cancel, as codex.py and Agent._abandon do; the raise follows anyway.
        if task.done() and not task.cancelled():
            task.exception()
        raise RuntimeError(f"the CLI did not respond to interrupt within {_INTERRUPT_GRACE_SECONDS}s")

    async def _run_attempt(
        self,
        turn: _Turn,
        prompt: str | list[dict[str, Any]],
        *,
        chat_id: str,
        turn_id: str,
        event_sink: TurnEventSink | None,
        ctx_metadata: dict[str, Any] | None,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None,
    ) -> tuple[ResultMessage, bool]:
        """Run one attempt (:meth:`_stream`) as a task, raced against the stop flag and bounded by
        ``timeout_seconds`` (BEP 19 §3.10.2), as ``CodexAgent._run_turn`` races a Codex turn.

        Returns ``(result, stopped)``: *stopped* is True only when the stop flag is why the attempt
        ended — as opposed to a ``timeout_seconds`` expiry (raised, never returned) or a turn that
        ended on its own. ``run()`` decides from it whether an interrupted result is a kept partial
        answer or a failure. However the attempt ends early the CLI is told to stop: a stop or a
        timeout through :meth:`_settle`, and a stream that raised — ``AbortTurn`` or anything else
        from the callback, or a vendor failure — through ``run()``'s teardown (:meth:`_teardown`),
        which interrupts a CLI the stream left busy. An interrupt clears ``turn.busy``, so no turn
        is interrupted twice.
        """
        task = asyncio.ensure_future(
            self._stream(
                turn,
                prompt,
                chat_id=chat_id,
                turn_id=turn_id,
                event_sink=event_sink,
                ctx_metadata=ctx_metadata,
                interrupt=interrupt,
            )
        )
        turn.task = task
        stop = asyncio.ensure_future(self._stop_requested.wait())
        try:
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            except TimeoutError as exc:
                # A timeout raises whatever _settle comes back with (BEP 19 §3.10.2); it waits only
                # so the CLI is told to stop before this returns.
                with contextlib.suppress(Exception):
                    await self._settle(turn, task)
                raise TimeoutError(
                    f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat {chat_id!r} exceeded "
                    f"timeout_seconds={self._config.timeout_seconds!r} during the turn and was interrupted"
                ) from exc
        finally:
            stop.cancel()
        if task.done():
            # It finished on its own, or raced the stop and won: nothing was abandoned. A raise from
            # the stream surfaces here, through task.result(), and `run()`'s teardown then tells the
            # CLI to stop, the stream having left it busy.
            return task.result(), False
        return await self._settle(turn, task), True

    async def _close(self, turn: _Turn) -> None:
        """Disconnect *turn*'s client once, whoever asks first — ``run()`` as the turn ends, or
        ``aclose()`` giving up on it. In a task of its own and shielded: ``disconnect()`` bounds
        itself, and a cancellation delivered inside it would skip the SDK's terminate-then-kill of
        the CLI (see the audit above ``_Turn``)."""
        closing = turn.closing
        if closing is None:
            closing = turn.closing = asyncio.ensure_future(turn.client.disconnect())
        await asyncio.shield(closing)

    async def _teardown(self, turn: _Turn, *, chat_id: str, turn_id: str) -> None:
        """Leave the CLI running nothing and close the client, however ``run()`` is ending — a second
        cancellation arriving while the interrupt is in flight included (``AgentActor`` cancels a turn
        on a user's abort, and can cancel it again on a retire or shutdown), since after ``run()``
        nothing else could reach this client: the close is in a ``finally``."""
        try:
            if turn.task is not None:
                if not turn.task.done():
                    turn.task.cancel()  # run() itself is unwinding: cancelled, or past a stream it gave up on
                # Whatever it ends with is read, as `_settle` does, so asyncio never reports a stream that
                # failed as `run()` was cancelled — whose result `run()` then never read — as never retrieved.
                turn.task.add_done_callback(_consume)
            if turn.busy:
                await self._interrupt(turn)
            if turn.pending:
                logger.warning(
                    "%s runtime %r: %d mid-turn message(s) for turn %r on chat %r were dropped: the turn ended "
                    "before the CLI answered them",
                    self._config.runtime,
                    self._kind,
                    len(turn.pending),
                    turn_id,
                    chat_id,
                )
        finally:
            await self._close(turn)
            # BEP 19 §3.5.3 (R14): the CLI has now disconnected (its child has exited), so removing the
            # per-turn TMPDIR removes only what this client owned. Exactly the path BOS created, and
            # best-effort — a teardown error must not mask the turn's result.
            if turn.tmpdir is not None:
                shutil.rmtree(turn.tmpdir, ignore_errors=True)

    def _release(self, chat_id: str, turn: _Turn | None) -> None:
        """Free *chat_id* for its next turn once *turn*'s CLI is gone (BEP 19 §3.10.1). A
        cancellation that lands on :meth:`_close`'s shield ends ``run()`` while the close still runs,
        and the chat's next turn would resume the same session while the old CLI may still be
        flushing it — the SDK gives it five seconds after stdin closes for that (read from source) —
        so the chat stays busy until the close has ended. A close that fails on that path has no one
        awaiting it, so its failure is logged here."""
        closing = turn.closing if turn is not None else None
        if closing is None or closing.done():
            self._in_flight.pop(chat_id, None)
            return

        def released(closed: asyncio.Task[None]) -> None:
            self._in_flight.pop(chat_id, None)
            if not closed.cancelled() and (failure := closed.exception()) is not None:
                logger.warning(
                    "%s runtime %r: closing the CLI for chat %r failed",
                    self._config.runtime,
                    self._kind,
                    chat_id,
                    exc_info=failure,
                )

        closing.add_done_callback(released)

    def _shutdown_result(self, chat_id: str, turn_id: str) -> AgentResult:
        """``Agent``'s own marker for a turn a stop came before, so a host needs no second string;
        nothing is committed (BEP 19 §3.10.2)."""
        logger.info(
            "%s runtime %r: chat %r has a stop requested; returning the shutdown marker without starting a turn",
            self._config.runtime,
            self._kind,
            chat_id,
        )
        return external_agent_result(output=SHUTDOWN_CONTENT, turn_id=turn_id, usage=None, finish_reason="shutdown")

    async def run(
        self,
        chat_id: str,
        content: MessageContent,
        *,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: TurnEventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_schema_retries: int = 1,
    ) -> AgentResult:
        """Run one Claude Code turn to completion and persist it (BEP 19 §3.6, §3.7, §3.9).

        One ``ClaudeSDKClient``, and with it one CLI child, per turn (§3.10.1): built from
        :meth:`_options` — pointed at BOS's MCP server when :meth:`_mcp_egress` has one for this
        agent (§3.8), which it builds on the agent's first turn — plus the turn's own fields —
        ``resume`` when the chat has a native session on record (§3.6), ``llm_args["model"]`` as
        ``model`` and ``llm_args["reasoning_effort"]`` as ``effort``, whose values BOS's ``low``/
        ``medium``/``high`` are among — and disconnected however the turn ends, schema retries
        included.

        The answer is ``ResultMessage.result``. ``usage`` is mapped by :func:`_usage` and summed
        over the attempt's native turns — more than one when a mid-turn message ran as a turn of
        its own (:meth:`_stream`) — and ``finish_reason`` is the CLI's ``terminal_reason``, or its
        ``stop_reason`` when it reports none, verbatim. The turn is committed as two messages, the assistant's carrying
        the session id the CLI answered from. A turn that spends its ``max_turns`` closes as
        BOS's own ``Agent`` closes at ``max_iterations``, answering ``MAX_ITERATION_CONTENT``,
        and is committed too (§3.9). Any other error result raises and commits nothing — a
        failed turn is not history — and so does a turn that ends without a result, or on a
        session other than the one it resumed (§3.6). A vendor failure raises with the turn's
        runtime, agent, chat and turn, and whether it came at startup; a caller's malformed
        content raises its own error, before any client is built.

        ``schema`` sends ``options.output_format={"type": "json_schema", "schema": schema}``
        (§3.9). What that makes the CLI do to the *model* was measured against CLI 2.1.281 with
        the fake Messages API, not assumed: it offers a synthetic ``StructuredOutput`` tool whose
        ``input_schema`` is *schema* verbatim, and answers a call to it itself with a synthetic
        tool result — never handing BOS a tool call to run — and if the model replies without
        calling it, the CLI injects its own one-shot nudge (a ``UserMessage`` reading
        ``"[structured-output-enforce] You MUST call the StructuredOutput tool..."``) and tries
        once more, inside the *same* ``receive_response()``; so BOS always sees exactly one
        ``ResultMessage`` per attempt, whichever way it went. None of that is trusted on its own
        (BEP 12): whatever happened, ``result.result`` — the tool's JSON input, verbatim, when it
        was called — is re-validated locally with the injected ``StructuredValidator``, the same
        one every ``Agent`` gets. A validation failure sends one plain-text correction message per
        retry, up to ``max_schema_retries``, as ``CodexAgent`` does; exhausting retries raises
        ``StructuredOutputError`` and commits nothing, the same as any other turn failure
        (``test_structured_output_arrives_from_the_synthetic_tool_against_the_real_cli`` pins the
        tool and the round trip; ``test_a_schema_validation_failure_sends_one_correction_message_
        per_retry`` pins the retry). Each retry is a new ``query()`` on the *same* connected client
        and native session — not a new CLI child — and, measured, ``max_turns`` is a budget that
        resets for every ``query()``, so a retry gets the same fresh budget the first attempt did,
        as it gets a ``timeout_seconds`` window of its own (§3.10.2). A turn that
        spends ``max_turns`` — first attempt or retry — is never schema-checked: like BOS's own
        ``Agent`` when a schema turn hits ``max_iterations`` (``_close_with_handoff`` never sets
        its ``structured_ok``), it closes unstructured, answering ``MAX_ITERATION_CONTENT``.

        Ending early has the three causes ``CodexAgent.run`` tells apart, decided as each happens
        (§3.10.2):

        - **``timeout_seconds``** is the caller's own deadline. It bounds ``connect()`` and each
          attempt, a window apiece; on expiry during an attempt the turn is interrupted, then a
          ``TimeoutError`` naming the phase is raised. Nothing is committed: an answer cut off by
          the caller's deadline is a failure, not history.
        - **A stop** — :meth:`request_stop` or :meth:`aclose` racing the turn — is BOS taking the
          turn away, and ``Agent`` keeps what a stopped turn established: the turn is interrupted,
          and the latest non-empty text among its top-level assistant messages (a tool call alone
          does not clear it), or ``""``, is returned and committed with its session,
          ``finish_reason`` the CLI's own — ``aborted_tools`` or ``aborted_streaming``, never
          Codex's ``interrupted`` (measured). A stop that lands while
          the CLI is starting starts no turn and returns ``SHUTDOWN_CONTENT``; one that lands
          before a schema retry raises the validation failure rather than starting another turn.
        - **An interrupted result BOS never asked for** raises, as every other error result does.

        The ``interrupt`` callback is not a fourth. A truthy return is a message delivered into the
        running turn (:meth:`_stream`), and a raised ``AbortTurn`` returns ``ABORTED_TURN_CONTENT``
        with ``finish_reason="aborted"`` once the CLI is told to stop, as ``Agent`` returns it
        (``agent.py``) — not a raise, which would make ``AgentActor`` report a failed turn. That
        commits nothing: ``Agent`` persists the marker to shape the model's history, and the native
        session is this model's history, which BOS never replays into the CLI.

        Two concurrent turns on one ``chat_id`` are refused rather than queued: a native session
        is single-threaded (§3.10.1). A turn started after :meth:`request_stop` or
        :meth:`aclose` returns ``SHUTDOWN_CONTENT`` before any client is built.
        """
        turn_id = turn_id or uuid.uuid4().hex
        # Before anything that costs, as `CodexAgent.run` does and for its reason: a turn
        # started after `request_stop()` cannot succeed, so it starts no CLI and no billable
        # turn. The flag is never cleared, as `Agent`'s is not.
        if self._stop_requested.is_set():
            return self._shutdown_result(chat_id, turn_id)
        if chat_id in self._in_flight:
            raise RuntimeError(
                f"Agent {self._kind!r} already has a turn running on chat {chat_id!r}. A Claude Code session is "
                f"single-threaded; wait for the turn to finish."
            )
        self._in_flight[chat_id] = None  # reserved synchronously: no await before this line
        turn: _Turn | None = None
        try:
            native_session_id = (
                await read_native_session_id(self._chat_store, chat_id, runtime=self._config.runtime)
                if self._chat_store is not None
                else None
            )
            llm = llm_args or {}
            # BEP 19 §3.5.3: the sandbox tripwire's stderr callback appends to `tripped`, which the
            # turn reads. Built before the client, since the client is built from these options and
            # the callback must close over a list that outlives the turn. Only under workspace-write,
            # the one level with a bash sandbox to degrade; `_compact` drops the None otherwise.
            tripped: list[str] = []
            stderr_cb = (
                self._sandbox_tripwire(tripped, chat_id, turn_id)
                if self._config.permission == "workspace-write"
                else None
            )
            options = replace(
                self._options(await self._mcp_egress()),
                **_compact(
                    resume=native_session_id,
                    model=llm.get("model"),
                    effort=llm.get("reasoning_effort"),
                    stderr=stderr_cb,
                    # BEP 19 §3.9: the CLI's own structured-output flag (`run()`'s docstring says
                    # what it makes the CLI do). `_compact` drops this when `schema` is None, so an
                    # unstructured turn's options are unaffected.
                    output_format={"type": "json_schema", "schema": schema} if schema is not None else None,
                ),
            )
            # BEP 19 §3.5.3, §8.2 (R14): under workspace-write, give this client's CLI a per-turn
            # TMPDIR of its own. The CLI derives the bash sandbox's writable temp root from TMPDIR
            # (Node's os.tmpdir()), so pointing it at a BOS-owned directory moves that root off the
            # shared `<system temp>/claude-<uid>` — the directory every live Claude Code session of the
            # same user shares. Measured: the agent's own commands keep a working temp (mktemp lands in
            # the BOS dir), while a bash write to the shared root is refused ("Read-only file system"),
            # because the shared root is no longer among the sandbox's writable binds. This alone
            # closes the write half; a `permissions.deny` Edit rule was tried too (the plan's candidate)
            # and proved redundant — moving TMPDIR already removes the shared root's writable bind — so
            # it is not sent. Reads are not restricted (a bash read of the shared root still returns
            # files, §3.5.5). Set in `run()`, not `_options()`, because the directory is per-turn and
            # its lifetime is the turn's; `_teardown` removes it once the client has disconnected.
            # The caller's content is converted first: a malformed part raises before the
            # directory exists, so it cannot be left behind by an error no teardown sees.
            prompt = _content_to_claude_prompt(content)
            tmpdir_override: Path | None = None
            if self._config.permission == "workspace-write":
                tmpdir_override = Path(tempfile.mkdtemp(prefix="bos-cc-tmpdir-"))
                options = replace(options, env={**(options.env or {}), "TMPDIR": str(tmpdir_override)})

            # ponytail: a client per turn costs one CLI spawn (~1s). A per-chat_id session pool
            # is the upgrade if that latency shows up; resume= makes the stateless version correct.
            turn = _Turn(_CLIENT_FACTORY(options))
            turn.mcp_unchecked = bool(options.mcp_servers)  # BEP 19 §3.8: read its status from the init
            turn.sandbox_tripped = tripped  # the list the tripwire appends to (BEP 19 §3.5.3)
            turn.tmpdir = tmpdir_override  # removed in `_teardown` once the client has disconnected
            phase = "at startup"  # connect(): the CLI starting, the resumed session loading
            structured_output: Any = None
            structured_ok = False
            ran_out = False
            kept = False
            retries = 0

            def _vendor_failure(exc: Exception) -> RuntimeError:
                # Every vendor failure on the turn path gets the runtime, agent, chat and turn, the
                # cause chained, as CodexAgent's do — the SDK's typed errors, and the bare
                # Exception it raises for a control request that times out (the initialize
                # handshake's among them) or is answered with an error. A caller's malformed
                # content is not among them: it was converted, and refused, before the client.
                #
                # BEP 19 §3.6, and only for the measured refusal. Given a `resume` it has no
                # session for, the CLI refuses at startup — "No conversation found with session
                # ID: <id>", exit code 1 — before any model call and without starting a session,
                # and the SDK raises that out of connect() as a ResultError (measured against the
                # CLI 2.1.281; test_the_real_cli_refuses_a_session_it_does_not_have_and_bos_says_so
                # pins it). Matched on that statement, which the ResultError carries in `errors`
                # and which names the id. Not on the ResultError's `session_id`: it is the resumed
                # id in the refusal too — the CLI reports the id it was asked for as its own — so
                # another failure at startup on the resume path would likely carry it as well
                # (inferred, not measured). Any other error result at startup is reported below as
                # the startup failure it is, with the CLI's own text — a value that is not a UUID
                # among them ("--resume requires a valid session ID or session title"), unless the
                # CLI finds a session with that title (its message says it would; not measured),
                # which the session check below catches — because reporting it as a lost session
                # sends an operator looking for one (§3.10.2).
                #
                # Shared between `connect()` and every attempt (both call this), since a schema
                # retry can fail natively too — a new native failure, not one more validation
                # attempt to retry — and so can the interrupt callback, which is wrapped the same.
                refusal = f"No conversation found with session ID: {native_session_id}"
                if (
                    native_session_id is not None
                    and isinstance(exc, ResultError)
                    and any(refusal in error for error in exc.errors)
                ):
                    return RuntimeError(
                        f"{self._config.runtime} runtime {self._kind!r}: session {native_session_id!r} for chat "
                        f"{chat_id!r} could not be resumed, and BOS does not silently start a fresh session "
                        f"under the same chat_id (BEP 19 §3.6): {exc}"
                    )
                return RuntimeError(
                    f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat {chat_id!r} failed "
                    f"{phase}: {exc}"
                )

            try:
                try:
                    async with asyncio.timeout(self._config.timeout_seconds):
                        await turn.client.connect()
                except TimeoutError as exc:
                    # Named, so a CLI that never finishes starting is not reported as a turn that
                    # timed out, nor — on a resumed chat — as §3.6's lost session. No turn had
                    # started, so there is nothing to interrupt; the SDK's connect() closes the
                    # half-started CLI itself on the cancellation.
                    raise TimeoutError(
                        f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat {chat_id!r} "
                        f"exceeded timeout_seconds={self._config.timeout_seconds!r} at startup (connect)"
                    ) from exc
                except Exception as exc:
                    raise _vendor_failure(exc) from exc
                phase = "during the turn"
                self._in_flight[chat_id] = turn
                if self._stop_requested.is_set():  # it landed while the CLI started: start no turn
                    return self._shutdown_result(chat_id, turn_id)
                while True:
                    try:
                        result, stopped = await self._run_attempt(
                            turn,
                            prompt,
                            chat_id=chat_id,
                            turn_id=turn_id,
                            event_sink=event_sink,
                            ctx_metadata=ctx_metadata,
                            interrupt=interrupt,
                        )
                    except TimeoutError:
                        # Unwrapped: it names its phase already, and a vendor-failure wrap would
                        # bury which deadline fired.
                        raise
                    except _SandboxDisabledError:
                        # BEP 19 §3.5.3: the tripwire ended the turn. Its own message already names the
                        # confinement failure; a vendor-failure wrap would bury it. The turn was
                        # interrupted before it raised, so teardown does not interrupt again.
                        raise
                    except AbortTurn:
                        # Ahead of the broad except, or a cooperative stop would be reported as a
                        # vendor failure. Agent CATCHES AbortTurn and returns (agent.py); see the
                        # docstring for why nothing is committed.
                        return external_agent_result(
                            output=ABORTED_TURN_CONTENT, turn_id=turn_id, usage=None, finish_reason="aborted"
                        )
                    except Exception as exc:
                        raise _vendor_failure(exc) from exc

                    if native_session_id is not None and result.session_id != native_session_id:
                        # Never a silent new session under the same chat_id (BEP 19 §3.6). A resumed
                        # turn answers from the session it resumed — measured: the ResultMessage
                        # carries that id — and the SDK documents a new id only under
                        # `fork_session`, which `native_options` refuses. So this fires only if the
                        # CLI stops doing so, or resumed another session by that name; the turn ran,
                        # and is not committed.
                        raise RuntimeError(
                            f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} resumed session "
                            f"{native_session_id!r} for chat {chat_id!r}, but the CLI answered from session "
                            f"{result.session_id!r}. BOS does not move a chat to another session silently "
                            f"(BEP 19 §3.6), so it committed nothing."
                        )
                    if stopped and result.terminal_reason in _INTERRUPTED:
                        # BOS stopped this turn itself (see the docstring's "a stop"): keep what it
                        # produced, never schema-checked — it did not finish, so a validation failure
                        # would only swap one negative outcome for another that says nothing truer.
                        kept = True
                        break
                    # BEP 19 §3.9: `max_iterations` is a budget, not an error. The CLI ends a turn
                    # that spends `max_turns` as an error result, subtype "error_max_turns" (pinned
                    # by test_a_turn_that_spends_max_iterations_closes_like_bos_agent_against_the_
                    # real_cli), and BOS closes it as its own Agent closes at `max_iterations`:
                    # MAX_ITERATION_CONTENT, with no handoff, since nothing hands an external
                    # runtime a consolidator, and committed with the session that ran it, so the
                    # next turn resumes there. A schema turn that runs out is not schema-checked
                    # below, the same call BOS's own Agent.run makes: a turn closed by
                    # `_close_with_handoff` never sets `structured_ok`, so it returns unstructured
                    # too (agent.py) — an unstructured marker is honest about what happened, where
                    # a `StructuredOutputError` would blame validation for a budget the model never
                    # got to spend on the schema at all. Measured: with `schema` set, the CLI's own
                    # self-correction nudge (above) already spends a native turn, so `max_turns=1`
                    # exhausts on the very first attempt whenever the model does not comply
                    # immediately, before BOS's own retry loop ever runs.
                    ran_out = result.subtype == "error_max_turns"
                    if result.is_error and not ran_out:
                        # The SDK documents an API failure as arriving under subtype "success", its
                        # prose in `result`; an error the CLI raises itself names its subtype and
                        # carries `errors`, as the measured resume refusal does. All four go out.
                        raise RuntimeError(
                            f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                            f"{chat_id!r} failed: subtype={result.subtype!r}, errors={result.errors!r}, "
                            f"api_error_status={result.api_error_status!r}, result={result.result!r}"
                        )
                    if schema is None or ran_out:
                        break
                    try:
                        # BEP 12: the CLI's own `structured_output` (from its `StructuredOutput`
                        # tool call) is never trusted on its own — `result.result` is exactly that
                        # tool call's JSON input when it was called (measured), so validating it
                        # here is one code path for both "the model complied" and "the model never
                        # called the tool", the same as CodexAgent's plain-text schema turns.
                        structured_output = self._structured_validator.validate(result.result or "", schema)
                        structured_ok = True
                        break
                    except StructuredOutputError as e:
                        if retries >= max_schema_retries or self._stop_requested.is_set():
                            # Exhausted, or stopped: a correction would start another native turn
                            # after the stop. Commits nothing, same as a native failure above — an
                            # unvalidated reply is not the answer `schema=` promised, so it is not
                            # turn history either.
                            raise
                        retries += 1
                        # A new query() on the SAME connected client and native session (measured:
                        # the session id does not change) — not a new CLI child — and `max_turns`
                        # is a per-query() budget that resets for it (measured: a client already at
                        # its limit still completes a later query() cleanly), so this retry gets the
                        # same fresh budget the first attempt did, as it gets a `timeout_seconds`
                        # window of its own from `_run_attempt` (BEP 19 §3.10.2).
                        prompt = (
                            f"Your previous response failed schema validation: {e}. "
                            "Reply ONLY with JSON matching the schema."
                        )
            finally:
                await self._teardown(turn, chat_id=chat_id, turn_id=turn_id)

            if ran_out:
                text = MAX_ITERATION_CONTENT
            elif kept:
                text = turn.last_text or ""
            else:
                text = result.result or ""
            if self._chat_store is not None:
                commit = await commit_external_turn(
                    self._chat_store,
                    chat_id,
                    turn_id=turn_id,
                    user_content=content,
                    response=text,
                    runtime=self._config.runtime,
                    native_session_id=result.session_id,
                    native_turn_id=turn.answer_uuid,
                    usage=turn.usage,
                )
                if commit_observer is not None:
                    observed = commit_observer(commit)
                    if inspect.isawaitable(observed):
                        await observed

            return external_agent_result(
                output=structured_output if structured_ok else text,
                structured=structured_ok,
                turn_id=turn_id,
                usage=turn.usage,
                finish_reason=result.terminal_reason or result.stop_reason,
            )
        finally:
            self._release(chat_id, turn)

    async def native_messages(self, chat_id: str) -> list[Message]:
        """Claude Code's own transcript for *chat_id*, projected into BOS ``Message``s (BEP 19
        §3.7) — what ``BosApp.get_messages(source="native")`` delegates to.

        Reads ``get_session_messages(session_id, directory=str(cwd))`` (``claude_agent_sdk``) in a
        thread (``asyncio.to_thread``), since it is a filesystem read: it parses the session's own
        JSONL under ``<CLAUDE_CONFIG_DIR or ~/.claude>/projects/<slug>/`` itself and chains it by
        ``parentUuid``, so this method never re-derives that chain. That storage is per **OS
        user**, not per workspace (BEP 19 §3.12 item 2) — reads are still safe because BOS always
        addresses a session by the id it stored (§3.6) and passes the resolved ``cwd`` as
        *directory*, so lookups are scoped and ids do not collide across a host's other
        workspaces. Unlike every other vendor call on this class, and unlike
        ``CodexAgent.native_messages``, this touches no CLI child at all: reading a transcript
        starts nothing.

        **No native session means no transcript, not a missing one.** ``read_native_session_id``
        returning ``None`` says this chat never ran a Claude Code turn, or ran one on another
        runtime since (logged at WARNING there), or this agent has no chat store at all — an empty
        transcript, ``[]``, logged at DEBUG.

        **A session id that exists and cannot be read is the opposite case, and raises** — the
        same rule ``CodexAgent.native_messages`` states, and for the same reason (§3.7): "BOS
        cannot read it" and "there is nothing to read" are different answers. Telling them apart
        here is on this method, not the vendor SDK: for each of its three documented misses — the
        session is not found, the id is not a valid UUID, or the transcript has no visible
        messages — ``get_session_messages`` returns ``[]`` rather than raising, and does not tell
        "no such file" apart from "a real file with nothing visible in it": both come back ``[]``.
        So an **empty result for a session id this chat's own record names** is treated as the
        missing case. That holds because of what a recorded id means: it is written by
        :func:`commit_external_turn` from a real ``ResultMessage.session_id`` (§3.6), so it never
        names a session that ran zero turns, and a session that ran at least one always has at
        least one real top-level message for ``get_session_messages`` to find — an empty result
        for such an id is therefore always the transcript being gone, pruned, or unreachable under
        the directory this agent resolved, never a legitimately empty conversation. A *stopped*
        turn is not this case either: the CLI logs the user's own prompt as a real, visible entry
        before it can respond at all, so an interrupted turn's transcript is never empty (real-CLI
        pinned, both interrupted-before-any-answer and interrupted-mid-tool-call).

        **It is not immune to every failure, though.** A transcript file that exists but cannot be
        decoded as UTF-8 — never a normal CLI write, only external corruption — raises a bare
        ``UnicodeDecodeError`` out of the vendor's own reader (``_try_read_session_file``'s
        ``Path.read_text(encoding="utf-8")``, guarded only against ``OSError``). Such a failure,
        and any other unexpected one, is wrapped the same way ``CodexAgent.native_messages`` wraps
        its own read errors: a ``RuntimeError`` naming the runtime, the agent, the session and the
        chat, with the original exception chained as its cause — never a bare, contextless
        traceback.

        **Messages only, for §3.7's structural reason.** Every ``SessionMessage``
        ``get_session_messages`` returns is already a top-level ``user`` or ``assistant`` entry —
        its own conversion drops sidechain, meta and every non-message transcript line
        (attachments, queue and cost bookkeeping, prompt snapshots, …) before this method ever
        sees one. What remains is filtered again here, to what BOS's tool-call pairing can
        represent (:func:`_visible_text`): kept for its ``text`` blocks only, or as-is when it is
        already a plain string, which is how the CLI records a plain-text turn; ``tool_use`` and
        ``tool_result`` blocks carry no BOS message content and are dropped. A message that is
        *only* such blocks — the assistant's own tool call, or the user turn carrying nothing but
        the matching ``tool_result`` — has no text left once they are dropped and is skipped
        rather than projected as an empty message. **Claude Code has no commentary/final-answer
        phase at all** (unlike Codex's ``MessagePhase``), so every kept assistant text is kept —
        none of it is excluded the way Codex excludes ``commentary``.

        **The CLI's own synthetic entries read as ordinary user text.** On an interrupt, the CLI
        writes its own plain, ``role="user"`` transcript entry — ``"[Request interrupted by
        user]"``, or the tool-use variant, ``"[Request interrupted by user for tool use]"``
        (real-CLI observed) — which :func:`_visible_text` cannot tell apart from text the operator
        actually typed, so it is projected here like any other user message. Nothing filters it
        out: BOS has no general policy for telling a vendor's own synthetic transcript text from a
        real one, and this one is arguably useful context — it says the turn was interrupted —
        rather than noise to hide. A host rendering this transcript sees it exactly as written.

        **One ``Message`` per surviving transcript entry, not per model reply.** CLI 2.1.281
        streams each content block of a reply as its own transcript entry (measured — Task 7), so
        a reply that both spoke and called a tool is more than one entry here, exactly as
        ``CodexAgent.native_messages`` emits one ``Message`` per ``AgentMessageThreadItem`` rather
        than one per Codex model turn.

        **``metadata["native_turn_id"]`` is always ``None`` here — never a real id.**
        ``SessionMessage`` (what ``get_session_messages`` returns) carries no per-entry turn or
        prompt identifier: unlike Codex's ``Turn.id``, the only id on it is the entry's own
        ``uuid``, already ``native_item_id``. §3.7's ``native_turn_id`` for Claude Code is a
        *write-time* label (:meth:`run`, §3.6): the uuid of a turn's last top-level assistant
        message — and that uuid already appears here as that same entry's own ``native_item_id``,
        so a host correlating "which transcript entry closed a BOS turn" reads BOS's own
        committed record (``source="bos"``) and matches its ``native_turn_id`` against this
        method's ``native_item_id``, rather than this method inventing a value nothing in the
        transcript records.

        ``Message.turn_id`` is left ``None``: BOS turn ids are minted by :meth:`run`, and a
        transcript BOS did not (all of) author has none. ``created_at`` is left at its default
        (now): unlike Codex's ``Turn.started_at``, ``SessionMessage`` carries no timestamp for BOS
        to prefer instead.
        """
        runtime = self._config.runtime
        native_session_id = (
            await read_native_session_id(self._chat_store, chat_id, runtime=runtime)
            if self._chat_store is not None
            else None
        )
        if native_session_id is None:
            logger.debug(
                "%s runtime %r: chat %r is bound to no native session, so its native transcript is empty",
                runtime,
                self._kind,
                chat_id,
            )
            return []

        try:
            raw = await asyncio.to_thread(get_session_messages, native_session_id, directory=str(self._config.cwd))
        except Exception as exc:
            # Not the missing/empty case above — get_session_messages returns [] for that rather
            # than raising. This is the read genuinely failing (e.g. a transcript file that exists
            # but is not valid UTF-8), so it is wrapped the same way, naming what BOS was trying to
            # read rather than surfacing the vendor's bare exception.
            raise RuntimeError(
                f"{runtime} runtime {self._kind!r}: the native transcript of session "
                f"{native_session_id!r} for chat {chat_id!r} could not be read: {exc}"
            ) from exc
        if not raw:
            raise RuntimeError(
                f"{runtime} runtime {self._kind!r}: the native transcript of session "
                f"{native_session_id!r} for chat {chat_id!r} could not be found under "
                f"{self._config.cwd} — get_session_messages() returned no messages for a session "
                f"this chat's own record names, so the transcript is missing rather than merely "
                f"empty."
            )

        messages: list[Message] = []
        for entry in raw:
            content = entry.message.get("content") if isinstance(entry.message, dict) else None
            text = _visible_text(content)
            if text is None:
                continue
            messages.append(
                Message(
                    llm_message={"role": entry.type, "content": text},
                    metadata={"source": runtime, "native_turn_id": None, "native_item_id": entry.uuid},
                )
            )
        return messages

    async def aclose(self) -> None:
        """Stop every in-flight turn, wait for them within a bound, then close their clients
        regardless (BEP 19 §3.10.2).

        Setting the stop flag — the one :meth:`request_stop` sets, so a turn started afterwards
        returns the shutdown marker — is what makes each running turn interrupt itself and race
        that, as a stop does (:meth:`run`); this adds the waiting and the closing. The wait is
        bounded by ``_ACLOSE_GRACE_SECONDS`` because :meth:`_settle` abandons a stream task it
        cannot cancel, which then outlives its turn; a turn still winding down after the bound is
        reported and its client closed anyway, since closing the client is what reaps the CLI. So
        this is a bounded drain, not a clean one. Each client is closed through :meth:`_close`, the
        same once-only disconnect the turn's own teardown uses. A turn whose CLI is still starting
        has no client registered yet — closing one mid-start could leave the CLI running — so it
        closes its own client when ``connect()`` returns, which ``timeout_seconds`` or the SDK's
        own initialize timeout bounds.
        """
        self._stop_requested.set()
        turns = [turn for turn in self._in_flight.values() if turn is not None]
        tasks = [turn.task for turn in turns if turn.task is not None and not turn.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_ACLOSE_GRACE_SECONDS)
            if pending:
                logger.warning(
                    "%s runtime %r: %d turn(s) still running after %ss; closing their clients anyway",
                    self._config.runtime,
                    self._kind,
                    len(pending),
                    _ACLOSE_GRACE_SECONDS,
                )
        # Concurrently and to the end: each close is bounded by the SDK, and one that raises must not
        # leave another turn's CLI running. Its error surfaces from that turn's own teardown, which
        # awaits the same close.
        await asyncio.gather(*(self._close(turn) for turn in turns), return_exceptions=True)
