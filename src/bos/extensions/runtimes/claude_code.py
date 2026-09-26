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

What exists so far is construction: the config, the permission mapping, the per-client
settings nonce, no repository settings unless a host opts in, the ``native_options``
allowlist, and the fail-closed preflights. Every check that reads the host runs here, and
none starts the CLI. No turn runs yet, and the hook and ``can_use_tool`` are not built, so
nothing in this module confines anything yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast, get_args

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionMode, SandboxSettings, SettingSource
from claude_agent_sdk.types import SystemPromptPreset

from bos.core.agent import AgentResult, ChatStore, MessageContent, StructuredValidator, TurnEventSink
from bos.extensions.runtimes._shared import ExternalAgentConfig, parse_external_config

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
# reads an empty value as unset. None of these variables is read as set by its mere presence,
# which an override could not undo — the SDK cannot unset a variable — and BOS would have to
# refuse instead.
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
})
# Read on those paths and left alone, because under BOS's default each is inert or loads
# nothing from outside the session:
# - compiled out of this build, their readers returning nothing: CLAUDE_CODE_MANAGED_
#   SETTINGS_PATH and CLAUDE_CODE_REMOTE_SETTINGS_PATH; with no reader at all:
#   CLAUDE_CODE_MOCK_REMOTE_SETTINGS and ALLOW_ANT_COMPUTER_USE_MCP.
# - where the CLI keeps its own login and global config: CLAUDE_CONFIG_DIR and
#   CLAUDE_SECURESTORAGE_CONFIG_DIR. What they hold that the default excludes stays excluded.
# - relocations of what only a settings file enables: CLAUDE_CODE_PLUGIN_CACHE_DIR, and
#   CLAUDE_CODE_USE_COWORK_PLUGINS, which renames the user settings file and plugin cache.
# - acting only on plugins a settings file enables — how and when those install, refresh,
#   update or are watched: CLAUDE_CODE_SYNC_PLUGIN_INSTALL, CLAUDE_CODE_ENABLE_BACKGROUND_
#   PLUGIN_REFRESH, FORCE_AUTOUPDATE_PLUGINS and CLAUDE_CODE_PLUGIN_DIR_WATCH.
# - background-session mode and trust: CLAUDE_CODE_SESSION_KIND and CLAUDE_BG_WORKSPACE_
#   TRUSTED. Under the default no repo settings load for trust to gate.
# - feature switches, which change what the CLI offers rather than load anything:
#   CLAUDE_CODE_ENABLE_FUNCTION_HOOKS, CLAUDE_CODE_WORKFLOWS, CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT,
#   and CLAUDE_CODE_COORDINATOR_EXTRA_TOOLS (coordinator mode only).
# - CLAUDE_CODE_BRIDGE_CHILD_MACHINE_SETTINGS, read in an ordinary session too, but only by
#   Claude in Chrome's gate, after CLAUDE_CODE_ENABLE_CFC's explicit false has closed it.
# - read only by modes BOS never starts: the other bridge-child variables, the
#   self-hosted runner (SELF_HOSTED_RUNNER_*, its lifecycle hooks included), the
#   environment-delivered workflow subcommand (CLAUDE_REMOTE_WORKFLOW_SCRIPT and _ARGS), and
#   the `claude agents` view (CLAUDE_AGENTS_SELECT, CLAUDE_CODE_AGENT).
# - the rest of the CLI's memory features, which relocate or extend the auto-memory switched
#   off above: CLAUDE_MEMORY_STORES, CLAUDE_CODE_REMOTE_MEMORY_DIR, CLAUDE_COWORK_MEMORY_*
#   and CLAUDE_CODE_POST_TURN_MEMORY*.
# - CLAUDE_AGENT_SDK_MCP_NO_PREFIX, which renames the tools of in-process SDK MCP servers
#   only, and BOS's MCP server is an HTTP one (§3.8).
# - restrictions only: CLAUDE_CODE_DISABLE_* and CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS*.
# Every other variable on those paths was classified by its name alone, and not read one by
# one: those named as a timeout, size, batch, cgroup or display setting. Each name that reads
# as a trigger — sync, install, update, fetch, enable, load — was read where it is used.

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


class ClaudeCodeAgent:
    """``ExternalRuntime`` adapter over the Claude Code vendor SDK (BEP 19 §3.4, §3.5).

    One instance per ``create_agent`` call. It holds no client: each turn is to build its
    own ``ClaudeSDKClient``, and with it a CLI child, from :meth:`_options` (§3.10.1).
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
        raise NotImplementedError("ClaudeCodeAgent does not run turns yet (BEP 19 §6 step 10 is in progress)")

    async def aclose(self) -> None:
        """Set the same one-way stop flag :meth:`request_stop` sets (BEP 19 §3.10.2).
        There is no client to close: each lives only as long as the turn that built it
        (§3.10.1)."""
        self._stop_requested.set()
