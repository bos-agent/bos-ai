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
and structured output: a ``schema`` turn validates the CLI's answer locally and retries with a
correction message on failure, up to ``max_schema_retries`` (§3.9, BEP 12).
A turn is not yet streamed as ``TurnEvent``s, polled for an ``interrupt`` or bounded by
``timeout_seconds``, and the hook and ``can_use_tool`` are not built, so nothing in this module
confines anything yet.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import stat
import sys
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
    PermissionMode,
    ResultError,
    ResultMessage,
    SandboxSettings,
    SettingSource,
)
from claude_agent_sdk.types import SystemPromptPreset

from bos.core.agent import (
    SHUTDOWN_CONTENT,
    AgentResult,
    ChatStore,
    MessageContent,
    StructuredOutputError,
    StructuredValidator,
    TurnEventSink,
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
    "hooks": "reserved for BOS's file-tool confinement, a PreToolUse hook (BEP 19 §3.5.3)",
    "can_use_tool": "reserved for BOS's answer to a permission prompt (BEP 19 §3.5.3)",
    "stderr": "reserved for BOS's sandbox tripwire on the CLI's stderr (BEP 19 §3.5.3)",
    "tools": "reserved for BOS, to restrict the tools offered to the model per `permission` (BEP 19 §3.5.3)",
    "disallowed_tools": "reserved for BOS, to restrict the tools offered to the model per `permission` (BEP 19 §3.5.3)",
    "allowed_tools": "reserved for BOS: an entry pre-approves a tool before `can_use_tool` is asked "
    "(BEP 19 §3.5.3, §3.8)",
    "mcp_servers": "reserved for BOS's MCP egress, which `mcp_tools` selects for (BEP 19 §3.8)",
    "strict_mcp_config": "set by BOS, always True, so the CLI loads only the MCP servers BOS passes and never "
    "a repo's .mcp.json (BEP 19 §3.5.3)",
    "env": "set by BOS, to switch off inherited variables that would load what its default leaves out "
    "(BEP 19 §3.12); its MCP bearer is to travel there too (§3.8)",
    "effort": "reserved for BOS, to set per turn from `llm_args['reasoning_effort']` (BEP 19 §3.9)",
    "resume": "reserved for BOS, to resume the chat's own native session (BEP 19 §3.6)",
    "output_format": "reserved for BOS, to set per turn from `schema=` (BEP 19 §3.9)",
})

_SESSION = "decides which native session a turn continues, or its id, and BOS owns that mapping (BEP 19 §3.6)"
_STORE = "mirrors transcripts to an external store that `resume` can read back from, which is deferred (BEP 19 §8.2)"
_STREAM = "changes what BOS reads back from the CLI, which is BOS's end of the connection, not a model setting"

# Neither set by BOS nor safe to forward.
_REFUSED: Mapping[str, str] = MappingProxyType({
    "extra_args": "appends arbitrary flags, `--dangerously-skip-permissions` among them, to the CLI command BOS builds",
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


async def _user_message(blocks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """*blocks* as one user message, shaped as ``ClaudeSDKClient.query`` sends a str prompt:
    ``query`` takes content blocks only as a stream of such messages."""
    yield {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


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
        # The chat_ids with a turn running (BEP 19 §3.10.1's busy guard), taken the instant
        # run() is entered, before any await could let a second call for the chat_id in.
        self._in_flight: set[str] = set()

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

    def _options(self) -> ClaudeAgentOptions:
        """The options for one client of this agent, apart from the per-turn fields
        (``resume``, a turn's own model or effort, ``output_format``). The hook,
        ``can_use_tool``, the stderr tripwire and the MCP egress are not built yet, so
        nothing sets them.

        Built afresh for every client, never cached: each call's settings carry a new nonce
        (``_SETTINGS_NONCE_VAR``), and each call reads the working directory's CLAUDE.md again
        (``_root_claude_md``).
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
            strict_mcp_config=True,
            env=dict(_INHERITED_ENV_OVERRIDES),
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
        :meth:`_options` plus the turn's own fields — ``resume`` when the chat has a native
        session on record (§3.6), ``llm_args["model"]`` as ``model`` and
        ``llm_args["reasoning_effort"]`` as ``effort``, whose values BOS's ``low``/``medium``/
        ``high`` are among — and disconnected however the turn ends, schema retries included.

        The answer is ``ResultMessage.result``. ``usage`` is mapped by :func:`_usage`, and
        ``finish_reason`` is the CLI's ``terminal_reason``, or its ``stop_reason`` when it
        reports none, verbatim. The turn is committed as two messages, the assistant's carrying
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
        the way a fresh ``timeout_seconds`` window will once Task 7 adds one (§3.10.2). A turn that
        spends ``max_turns`` — first attempt or retry — is never schema-checked: like BOS's own
        ``Agent`` when a schema turn hits ``max_iterations`` (``_close_with_handoff`` never sets
        its ``structured_ok``), it closes unstructured, answering ``MAX_ITERATION_CONTENT``.

        Two concurrent turns on one ``chat_id`` are refused rather than queued: a native session
        is single-threaded (§3.10.1). A turn started after :meth:`request_stop` or
        :meth:`aclose` returns ``SHUTDOWN_CONTENT`` before any client is built.
        """
        # Accepted but not acted on yet, each until its task lands: `event_sink` and
        # `ctx_metadata` (Task 6: streamed TurnEvents, BEP 19 §3.9); `interrupt` (Task 7, §3.9),
        # which is not polled, so a message a caller queues mid-turn stays in its queue; and the
        # config's `timeout_seconds` (Task 7, §3.10.2), so nothing bounds a turn yet.
        turn_id = turn_id or uuid.uuid4().hex
        # Before anything that costs, as `CodexAgent.run` does and for its reason: a turn
        # started after `request_stop()` cannot succeed, so it starts no CLI and no billable
        # turn. `Agent`'s own marker, so a host needs no second string; nothing is committed;
        # and the flag is never cleared, as `Agent`'s is not.
        if self._stop_requested.is_set():
            logger.info(
                "%s runtime %r: chat %r asked for a turn after request_stop(); returning the shutdown marker "
                "without starting one",
                self._config.runtime,
                self._kind,
                chat_id,
            )
            return external_agent_result(output=SHUTDOWN_CONTENT, turn_id=turn_id, usage=None, finish_reason="shutdown")
        if chat_id in self._in_flight:
            raise RuntimeError(
                f"Agent {self._kind!r} already has a turn running on chat {chat_id!r}. A Claude Code session is "
                f"single-threaded; wait for the turn to finish."
            )
        self._in_flight.add(chat_id)  # taken synchronously: no await before this line
        try:
            native_session_id = (
                await read_native_session_id(self._chat_store, chat_id, runtime=self._config.runtime)
                if self._chat_store is not None
                else None
            )
            llm = llm_args or {}
            options = replace(
                self._options(),
                **_compact(
                    resume=native_session_id,
                    model=llm.get("model"),
                    effort=llm.get("reasoning_effort"),
                    # BEP 19 §3.9: the CLI's own structured-output flag (`run()`'s docstring says
                    # what it makes the CLI do). `_compact` drops this when `schema` is None, so an
                    # unstructured turn's options are unaffected.
                    output_format={"type": "json_schema", "schema": schema} if schema is not None else None,
                ),
            )
            prompt = _content_to_claude_prompt(content)

            # ponytail: a client per turn costs one CLI spawn (~1s). A per-chat_id session pool
            # is the upgrade if that latency shows up; resume= makes the stateless version correct.
            client = _CLIENT_FACTORY(options)
            result: ResultMessage | None = None
            answer_uuid: str | None = None
            phase = "at startup"  # connect(): the CLI starting, the resumed session loading
            structured_output: Any = None
            structured_ok = False
            ran_out = False
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
                # Shared between `connect()` and every retry's `query()` (both call this), since a
                # schema retry can fail natively too — a new native failure, not one more
                # validation attempt to retry.
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
                    await client.connect()
                except Exception as exc:
                    raise _vendor_failure(exc) from exc
                phase = "during the turn"
                while True:
                    try:
                        await client.query(prompt if isinstance(prompt, str) else _user_message(prompt))
                        result = None
                        async for message in client.receive_response():
                            if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
                                # Recorded as `native_turn_id`, since the stream names no turn: the
                                # turn's last top-level assistant message, a transcript entry, which
                                # `get_session_messages` returns under this uuid (pinned by
                                # test_a_second_turn_resumes_the_first_by_session_id_against_the_real_cli).
                                # `ResultMessage.uuid` is in no transcript, and the prompt's own entry
                                # is not in the stream (both measured, not pinned). On a schema retry
                                # this is overwritten by the winning attempt's own message.
                                answer_uuid = message.uuid
                            elif isinstance(message, ResultMessage):
                                result = message
                    except Exception as exc:
                        raise _vendor_failure(exc) from exc

                    if result is None:
                        raise RuntimeError(
                            f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                            f"{chat_id!r} ended without a result"
                        )
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
                        if retries >= max_schema_retries:
                            # Exhausted: commits nothing, same as a native failure above — an
                            # unvalidated reply is not the answer `schema=` promised, so it is not
                            # turn history either.
                            raise
                        retries += 1
                        # A new query() on the SAME connected client and native session (measured:
                        # the session id does not change) — not a new CLI child — and `max_turns`
                        # is a per-query() budget that resets for it (measured: a client already at
                        # its limit still completes a later query() cleanly), so this retry gets the
                        # same fresh budget the first attempt did, mirroring the per-attempt
                        # `timeout_seconds` window BEP 19 §3.10.2 gives Codex's own retries.
                        prompt = (
                            f"Your previous response failed schema validation: {e}. "
                            "Reply ONLY with JSON matching the schema."
                        )
            finally:
                await client.disconnect()

            text = MAX_ITERATION_CONTENT if ran_out else (result.result or "")
            usage = _usage(result.usage)
            if self._chat_store is not None:
                commit = await commit_external_turn(
                    self._chat_store,
                    chat_id,
                    turn_id=turn_id,
                    user_content=content,
                    response=text,
                    runtime=self._config.runtime,
                    native_session_id=result.session_id,
                    native_turn_id=answer_uuid,
                    usage=usage,
                )
                if commit_observer is not None:
                    observed = commit_observer(commit)
                    if inspect.isawaitable(observed):
                        await observed

            return external_agent_result(
                output=structured_output if structured_ok else text,
                structured=structured_ok,
                turn_id=turn_id,
                usage=usage,
                finish_reason=result.terminal_reason or result.stop_reason,
            )
        finally:
            self._in_flight.discard(chat_id)

    async def aclose(self) -> None:
        """Set the same one-way stop flag :meth:`request_stop` sets (BEP 19 §3.10.2), so a
        turn started afterwards returns the shutdown marker. The agent keeps no client to
        close: each turn disconnects its own when it ends (§3.10.1). A turn already running is
        not interrupted, and aclose() does not wait for it."""
        self._stop_requested.set()
