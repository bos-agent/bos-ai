# BEP 15: Built-in Config Agent for BOS Project Configuration

- **Status:** Draft
- **Depends on:** BEP 4 (extension points), BEP 9 (scaffolding, `doctor`, `llm-full.md`), SubagentPlugin (BEP 4/14 lineage)
- **Blocked by:** nothing — implementable now

## 1. Motivation

A user working in a BOS project (scaffolded by `boscli init`) frequently needs configuration changes: switch models, add an agent, enable a plugin, bind subagents, add a channel. Today they either edit `.bos/config.toml` by hand with the `llm-full.md` reference open, or ask their project's main agent — which has every tool enabled and no prescribed procedure, so it edits the live config in place with no validation gate. A bad edit is only discovered when the gateway restarts and fails to boot.

This BEP adds one built-in `ep_agent` extension, **`bos_config`**: a specialist agent that owns the *procedure* for changing BOS project configuration safely — isolate in a git worktree, edit, validate with `boscli doctor`, merge back, and stop, telling the user to restart the gateway.

### 1.1 Why an `ep_agent` extension and not a skill

Both ship built-in with BOS (cf. the `python` skill at `src/bos/skills/python/SKILL.md` and the `BOS` agent at `src/bos/extensions/agents/bos.py`). The discriminators:

- **Tool policy.** A skill is procedure text injected into whichever agent loads it and inherits that agent's tool surface. This workflow rewrites config and merges branches; it warrants an agent spec with an explicit tool allowlist.
- **Context isolation.** The workflow reads `llm-full.md` (~940 lines) plus config contents plus validation output. Delegation via `AskSubagent` keeps that out of the user's main conversation.
- **Config addressability.** An `ep_agent` resolves through `[agent.defaults] -> factory -> [agents.<name>]`, so projects can override its model, prompt, or tools, and other agents can `_parent = "bos_config"` (factory agents are parentable since #69). Skills have no equivalent.

## 2. Goals and Non-Goals

### 2.1 Goals

- Ship a built-in `bos_config` agent kind, registered like `BOS`, available in every project whose `[platform.extensions]` includes `bos.exts` (the default).
- Encode the conservative workflow — worktree → edit → validate → merge back → report — in its system prompt as a contract, not a suggestion.
- Make the user's main agent delegate configuration changes to it by default, via description-based routing (§3.7), with zero scaffold or config changes.
- Ground its edits in the shipped BOS reference (`llm-full.md`).

### 2.2 Non-Goals

- **No gateway self-restart.** The agent runs as an actor inside the gateway process; restarting the gateway from inside would kill the agent (and the user's session) mid-task. The agent stops after merge-back and instructs the user to run `uv run boscli gateway restart`. A gateway-side drain-and-reload mechanism is a possible future BEP; this BEP must not grow a private process-management mechanism.
- **No automatic restart, ever** — not even detached. Reporting an unverifiable restart is overclaiming.
- **No `aggressive_bos` / `conservative_bos` fork of the `BOS` agent.** Risk posture lives in this one specialist (conservative by construction, §3.6 knob); the flagship `BOS` agent is not bifurcated by posture.
- **No config-migration or schema-upgrade engine.** The agent edits what the user asks for; it does not rewrite configs across BOS versions.
- **No general git-workflow agent.** The worktree procedure exists solely to isolate config edits; branching strategy, PRs, and code changes are out of scope.
- **No background/scheduled operation.** This agent acts only on an explicit user or main-agent request.

## 3. Design

### 3.1 Runtime shape

`bos_config` is an **agent kind** (an agent spec registered at bootstrap), not a process or service. It materializes as a short-lived subagent run when the main agent calls `AskSubagent(role="bos_config", task=...)` (executed by `SubagentAgentPlugin` via `AgentRunner.run`, `src/bos/plugins/subagent.py`), or as a oneshot in-process run via `boscli ask --agent bos_config "..."`. It holds no state between runs; everything durable lives in the project's git history and `.bos/config.toml`.

### 3.2 Registration and ownership

- **Module:** `src/bos/extensions/agents/config.py`, mirroring `bos.py`. Exports `BOS_CONFIG_AGENT_NAME = "bos_config"` and registers via `@ep_agent(name=BOS_CONFIG_AGENT_NAME, description=...)`.
- **Loading:** one import line in `src/bos/exts.py`, next to `import bos.extensions.agents.bos`. Like `BOS`, it is available when `bos.exts` is on `[platform.extensions]` — not unconditionally.
- **Source of truth:** the factory owns the default spec; per-project overrides flow through the existing chain `[agent.defaults] -> factory -> [agents.bos_config]`. Factory kwargs come from `[exts.ep_agent.bos_config]` (per the `ep_agent` contract in `src/bos/core/contract.py`). No new config keys or mechanisms are introduced.
- **Name-collision stance:** a project that already defines `[agents.bos_config]` merges over the factory result — the same defined behavior `BOS` has. Documented, accepted.

### 3.3 Agent spec

```python
default_agent_spec = {
    "description": _BOS_CONFIG_DESCRIPTION,   # §3.7 — doubles as the routing rule
    "system_prompt": _system_prompt,          # §3.4 — the workflow contract
    "tools": {
        "enabled": ["Bash", "ReadFile", "EditFile", "WriteFile", "GrepSearch", "GlobSearch"],
        "disabled": [],
        "usages": {},
    },
    "plugins": {"enabled": [], "disabled": []},
}
```

- Tool names are the built-in registrations in `src/bos/extensions/tools/` (`Bash`, `ReadFile`, `EditFile`, `WriteFile`, `GrepSearch`, `GlobSearch`). No web tools, no `Sleep`, no `PowerShell` (git worktree flow is POSIX-shell shaped; Windows support follows whatever `Bash` tooling the host provides).
- No plugins: the agent needs no memory, planning, tasks, skills, or sub-delegation. Lean context, cheap runs.
- No `model` key: inherits `[agent.defaults].model` like any agent.

### 3.4 The workflow contract (system prompt)

The system prompt prescribes, in order. "Config change" below means edits under `.bos/` (`config.toml`, `agents/`, `skills/`, `extensions/` — the files tracked by the scaffolded `.bos/.gitignore` allowlist).

1. **Ground.** Read `<workspace>/llm-full.md` (shipped by `boscli init` since #68). If absent (pre-#68 project), read the packaged copy: `uv run - <<'EOF'` with `importlib.resources.files("bos").joinpath("llm-full.md")`. Read the current `.bos/config.toml` fully before planning any edit.
2. **Isolate.** If the workspace is a git repository: `git worktree add -b bos-config/<slug> <scratch-dir>` from `HEAD`. Copy gitignored runtime prerequisites into the worktree — at minimum the configured envfile (default `.bos/.env`), which the scaffolded `.bos/.gitignore` excludes and whose absence fails `doctor`'s `paths` check. If the workspace is **not** a git repository, fall back to in-place editing with a timestamped backup copy of each file it touches, and say so in the report.
3. **Edit.** Smallest change satisfying the request; preserve formatting and comments; never touch files outside `.bos/` plus workspace-root agent/skill files the request explicitly names.
4. **Validate — static.** Run `uv run boscli doctor` **with cwd inside the worktree** — `doctor` resolves the project from the cwd via `Workspace.from_discovery(".")` (`_discover_project`, `src/bos/cli/commands/scaffolding.py`), not from `-c`. Gate on exit code: `doctor` exits 1 only on `fail` findings. Failures caused by the edit must be fixed and re-validated; pre-existing failures unrelated to the edit are reported and do not block, but must be explicitly attributed. `--probe` is unnecessary here — the smoke turn below supersedes it.
5. **Validate — smoke turn.** Run `uv run boscli ask "say hello to me"` with cwd inside the worktree. `ask` boots the full harness in-process (extension loading, agent resolution, plugin bindings, one live model turn) and leaves no gateway running — it exercises everything `doctor` cannot. If the change targets a specific non-main agent, additionally smoke that agent via `--agent <name>`. Gate: a non-zero exit or an error reply blocks the merge if attributable to the edit; failures that reproduce identically on the unmodified config (e.g. missing credentials, network egress unavailable) are pre-existing — report them, note the smoke turn was inconclusive, and proceed on the `doctor` gate alone. This step makes one short LLM call and requires a configured model and credentials (copied with the envfile in step 2).
6. **Merge back.** Commit in the worktree branch; from the main workspace run `git merge bos-config/<slug>`; remove the worktree and delete the branch. If the main workspace has uncommitted changes to the same files, stop and report instead of merging.
7. **Stop and report.** Never run `gateway restart`, `gateway stop`, or `gateway start` (Non-Goal). The final report states: files and keys changed (old → new), the `doctor` result, the smoke-turn result, merge status, and the literal next step — `uv run boscli gateway restart` — for the user.

**Failure recovery.** Any failure before merge-back: remove the worktree and branch, report the failure with the `doctor`/smoke-turn output; the live config was never touched. Failure during merge (conflict): abort the merge, keep the branch, report it for manual resolution. In-place fallback failure: restore from the backup copy.

### 3.5 What "validated" means (precise vocabulary)

Validation has two layers, and the report must attribute claims to the right one:

- **`doctor` (static):** config parse/validation, path existence, extension importability, agent spec resolution, env references, model configuration, gateway port state — read-only. Passing means *the config loads and its references resolve*.
- **Smoke turn (`boscli ask`, live):** the harness bootstraps under the new config and completes one real agent turn in-process. Passing means *an agent actually runs with this config*.

Passing both still does **not** guarantee the gateway will serve traffic after restart (channels, port binding, and long-running actor behavior are not exercised). The report says "validated with doctor and a smoke turn" — never "gateway restarted" or "verified running".

### 3.6 Posture knob

`[exts.ep_agent.bos_config] workflow = "worktree" | "in_place"` — passed to the factory as a kwarg (existing `ep_agent` mechanism). Default `"worktree"`. `"in_place"` swaps step 2 for direct editing with backups (validation and stop-before-restart still apply). This knob is the entire posture surface; no other posture mechanism is added.

### 3.7 Binding and routing (how the main agent uses it)

No binding work is needed. `SubagentAgentPlugin` treats `enabled = ["*"]` (or the string `"*"`) as "all registered agents" (`_normalize_enabled`, `src/bos/plugins/subagent.py`), and both the built-in `BOS` agent (`plugins.enabled` includes `SubagentPlugin`) and the scaffolded project (`[agent.defaults.plugins] enabled = ["*"]` in `config.toml.tmpl`) hit that default. Registering the kind is sufficient: `bos_config` appears in the main agent's `<available_subagents>` prompt section automatically.

Routing therefore rides on the description, which is written as a rule, not a summary:

> "BOS project configuration specialist. ALWAYS delegate changes to BOS project configuration (`.bos/config.toml`, `[agents.*]`, `[exts.*]`, `[runtime.*]`, agent/skill registration) to this agent instead of editing those files directly. It validates changes in an isolated git worktree with `boscli doctor` before merging back, and reports the restart step for the user."

Projects that want direct addressing add `[runtime.actors.config] agent = "bos_config"` themselves (existing mechanism, documented in the scaffolded config comments); this BEP changes no templates.

### 3.8 Interactions with look-alikes

- **vs. the `BOS` agent:** `BOS` remains general-purpose and *may* still touch config if a user insists; the description rule steers default behavior, it does not enforce. Enforcement (tool-level guards on config paths) is out of scope.
- **vs. skills:** the workflow lives in the agent's system prompt, not a SKILL.md; there is no `bos-config` skill to keep in sync (no hybrid).
- **vs. `doctor`:** `doctor` stays the single validation mechanism; the agent is a caller, never a reimplementation.
- **vs. agent-dir agents (`.bos/agents/*.md|toml`):** those are project-defined kinds; `bos_config` is a built-in kind. Same registry, same resolution chain, no special casing.

## 4. Audience flows (end state)

- **End user (TUI):** "switch my main agent to claude-sonnet-5" → main agent calls `AskSubagent(role="bos_config", ...)` → subagent runs the §3.4 contract → main agent relays: what changed, doctor and smoke turn passed, "run `uv run boscli gateway restart` to apply". User runs the restart; TUI reconnects (existing `boscli tui` behavior).
- **Operator (direct):** `uv run boscli ask --agent bos_config "add a researcher subagent binding to main"` — oneshot, in-process, no gateway involvement; same contract, same report.
- **Operator (inspect/recover):** the audit trail is git — worktree branches are named `bos-config/<slug>`; a failed merge leaves the branch for manual `git merge`/`git diff`. `boscli doctor` re-checks health at any time.
- **Background/automated:** none (Non-Goal §2.2).

## 5. Compatibility and fallout

Purely additive: a new module, one import in `bos/exts.py`. No existing contract changes, no template changes, no test rewrites. Two visible effects: (a) every project with default extensions gains one entry in `<available_subagents>` (bounded by the existing `BOS_CAPABILITY_LIMIT` rendering cap); (b) a project already defining `[agents.bos_config]` now merges over a factory (previously the name was purely theirs) — behavior is the defined merge chain, called out in release notes.

## 6. Implementation plan (dependency-ordered)

1. **Factory module** — `src/bos/extensions/agents/config.py`: name constant, description (routing rule), system prompt (workflow contract §3.4–§3.6), spec dict, `@ep_agent` factory with `workflow: str = "worktree"` kwarg validated to the two allowed values.
2. **Registration** — import line in `src/bos/exts.py`.
3. **Tests** — `tests/test_config_agent.py` (mirroring existing agent/extension tests): factory registers under `ep_agent`; spec validates as `AgentConfig`; resolution merge with `[agents.bos_config]` override works; `_parent = "bos_config"` resolves (regression vs #69); `workflow` kwarg accepted, invalid value rejected; prompt contains the load-bearing contract strings (never-restart, doctor gate, smoke-turn gate, worktree branch prefix).
4. **Docs** — `llm-full.md` section describing the built-in kind and the `[exts.ep_agent.bos_config]` knob.

Each step depends only on what precedes it; step 1+2 alone yield a working agent.

## 7. Acceptance criteria

- With a scaffolded project and default extensions, `AskSubagent(role="bos_config", ...)` resolves the kind (precondition: `SubagentPlugin` enabled on the calling agent, which is the scaffold/built-in default).
- `uv run boscli ask --agent bos_config ...` runs it oneshot (precondition: a configured model).
- `uv run pytest -q`, `uv run ruff check src tests`, `npx -y pyright src` — all clean.
- The rendered system prompt contains no instruction to run `gateway restart|start|stop`.

## 8. Open questions

- Whether the scaffolded `config.toml.tmpl` comments should mention `bos_config` in the subagent-delegation example (nice-to-have; not required for the routing to work).

## 9. Revision history

- 2026-07-03 — Draft. Decisions: `ep_agent` over skill; stop-before-restart (no self-restart); description-based auto-binding; no BOS-agent posture fork; single `workflow` factory kwarg.
- 2026-07-03 — Added the live smoke-turn validation layer (`boscli ask` in the worktree) alongside `doctor`; two-layer vocabulary in §3.5.
