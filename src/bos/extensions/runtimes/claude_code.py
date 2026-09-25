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
settings nonce, the ``native_options`` allowlist, and the fail-closed preflights. Every
check that reads the host runs here, and none starts the CLI. No turn runs yet, and the
hook and ``can_use_tool`` are not built, so nothing in this module confines anything yet.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
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
# its name against the real CLI.
_SETTINGS_NONCE_VAR = "BOS_SETTINGS_NONCE"

# BEP 19 §3.5.3, fact 9: left at None the SDK sends no `--setting-sources`, and then every
# source loads — the operator's own ~/.claude/settings.json among them (the SDK's own
# docstring). So BOS always sends the list. `project` loads the repo's
# .claude/settings.json and its CLAUDE.md — and that settings file is live config: its
# hooks, MCP servers (with the repo's .mcp.json) and apiKeyHelper ran as commands, outside
# the bash sandbox, in a workspace nobody had trusted (measured against CLI 2.1.281).
_SETTING_SOURCES: tuple[str, ...] = get_args(SettingSource)
_DEFAULT_SETTING_SOURCES: tuple[SettingSource, ...] = ("project",)

# BEP 19 §3.10.3: under `auth = "subscription"`, either of these in BOS's environment
# silently turns a subscription run into a billed API run, because the CLI inherits that
# environment whole (§3.12).
_API_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

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
    "strict_mcp_config": "reserved for BOS's MCP egress (BEP 19 §3.8)",
    "env": "reserved for BOS, whose MCP bearer is to travel in the CLI's environment (BEP 19 §3.8)",
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


def _system_prompt(config: ExternalAgentConfig) -> str | SystemPromptPreset:
    """BEP 19 §3.4.1: ``system_prompt`` is appended to Claude Code's own prompt and
    ``base_instructions`` replaces it. With neither, the preset goes out bare — never
    ``None``, which the SDK turns into an *empty* prompt (§3.4.1.3, fact 8)."""
    if config.base_instructions is not None:
        return config.base_instructions
    if config.system_prompt is not None:
        return {"type": "preset", "preset": "claude_code", "append": config.system_prompt}
    return {"type": "preset", "preset": "claude_code"}


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

        # BEP 19 §3.10.3. Read now, from this process's environment as it stands, since
        # the CLI inherits it. An empty value is not a credential: the CLI 2.1.281 source
        # reads both by truthiness or through a parser that maps an empty value to unset.
        if self._config.auth == "subscription":
            if present := [name for name in _API_CREDENTIAL_VARS if os.environ.get(name)]:
                raise ValueError(
                    f'`auth = "subscription"` (the default), but {" and ".join(present)} '
                    f"{'is' if len(present) == 1 else 'are'} set in this process's environment. The Claude Code "
                    f"CLI inherits that environment, and a credential there silently turns a subscription run "
                    f'into a billed API run. Unset it, or set `auth = "api_key"` to bill it deliberately.'
                )

        if self._config.permission == "workspace-write" and (reason := _bash_sandbox_unavailable(sys.platform)):
            raise ValueError(
                f'`permission = "workspace-write"` is refused on this host: {reason}. It needs Claude Code\'s bash '
                f"sandbox, and BOS refuses rather than let bash run unsandboxed (BEP 19 §3.5.3)."
            )

        self._stop_requested = asyncio.Event()

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

        Built afresh for every client, never cached, because each call's settings carry
        a new nonce (``_SETTINGS_NONCE_VAR``).
        """
        # `env` stays empty for now, and deliberately carries no `CLAUDE_CODE_SANDBOXED`
        # override. The CLI source reads an inherited one as "trusted", but in everything
        # measured against CLI 2.1.281 it changed nothing: the repo's allow rules are gated
        # on the trust recorded in the CLI's config, which that variable does not reach, and
        # the repo's hooks, MCP servers, settings `env` and apiKeyHelper ran without any
        # trust at all.
        config = self._config
        sandbox = (
            cast(SandboxSettings, dict(_WORKSPACE_WRITE_SANDBOX)) if config.permission == "workspace-write" else None
        )
        return ClaudeAgentOptions(
            cwd=config.cwd,
            permission_mode=_PERMISSION_MODES[config.permission],
            sandbox=sandbox,
            settings=json.dumps({"env": {_SETTINGS_NONCE_VAR: uuid.uuid4().hex}}),
            setting_sources=list(self._setting_sources),
            system_prompt=_system_prompt(config),
            max_turns=self._max_turns,
            model=config.model,
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
