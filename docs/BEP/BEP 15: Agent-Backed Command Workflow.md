# BEP 15: Agent-Backed Command Workflow

Status: **draft** (bootstrapped 2026-06-15; scope expected to expand; renumbered 12→15 on 2026-06-25 — see BEP 12: Structured Agent Output, which it depends on)

---

## Core Insight

Most `boscli` commands today are either pure deterministic logic (scaffolding from fixed templates) or, at most, a single bare LLM completion (`_complete` in the init wizard generates specialist prose). Neither can *understand intent*: research the actual project, ask the user a clarifying question, weigh options, and produce a tailored result. As BOS grows day-2 tooling (`gen agent`, `gen tool`, `gen channel`, config edits, refactors), more commands hit the same wall — the user knows what they want in prose, but the command can only act on flags.

BEP 15 defines a **reusable pattern for agent-backed commands**: a CLI command whose core work is performed by a real **in-process `Agent.ask()` turn** — custom system prompt, builtin tools and skills, a human-in-the-loop `AskUser` tool, and an isolated audit-retained message store — while the **CLI retains deterministic ownership of any mutation** to project files and config. Commands that change startup-loaded state run their generation and mutation inside a throwaway **git worktree**, smoke-verify there, and only **merge back on success** — so a bad result can never break the user's `boscli gateway start`.

This is a *framework BEP*, not a single feature. It specifies the shared building blocks (the agent-backed runner, the HITL tool, the audit store, the structured-output contract, and the worktree verify-before-apply lifecycle) and the seam each command plugs into. `gen agent` is the **first worked instance**; `gen tool`, `gen channel`, and future intent-driven commands are expected to reuse the same harness.

It is also the sanctioned lift of BEP 9's deferred Non-Goal #1 ("LLM-assisted topology generation… explicitly deferred"): BEP 9 built the deterministic substrate; BEP 15 builds the agent layer that parameterizes it.

The design metric: **a user describes intent in one sentence and ends with a tailored, already-smoke-tested change merged into their project — with zero risk to a running gateway — and adding a new agent-backed command is mostly prompt + a verify hook, not new plumbing.**

---

## Goals

1. **A reusable agent-backed command harness** — shared building blocks any command can adopt:
   - **In-process `Agent.ask()` runner** over the existing `ws.harness()` → `harness.create_agent(agent_cfg=…)` → `agent.ask(…)` path, executed on the shared event loop (`_run_llm`).
   - **Per-command `agent_cfg`**: custom `system_prompt`, builtin `tools`/`plugins` (skills), `model`, `agent_name`.
   - **`AskUser` human-in-the-loop tool** (terminal-backed) injected as an agent-local tool.
   - **Isolated, audit-retained message store** separate from the project's chat store.
   - **Structured-output contract**: the agent proposes a typed result; the **CLI owns mutation** (files + `bos.toml`) deterministically.
   - **Worktree verify-before-apply lifecycle**: create worktree → generate/mutate there → run a command-supplied **verify hook** → commit + merge back on pass / discard on fail.

2. **First instance — `boscli gen agent` (interactive, agent-backed)**: turn a natural-language intent into a complete agent (`name`, `description`, `system_prompt`, `tools`[, `model`]); verify via a `boscli ask` smoke reply; merge back. Preserves the existing non-interactive `gen agent NAME …` contract as an additive mode.

3. **Extensibility to other commands**: define the seam so `gen tool`, `gen channel`, and similar intent-driven or mutation-risky commands can adopt the harness by supplying (a) a generation prompt + result schema and (b) a verify hook — without re-implementing worktree/HITL/audit/runner plumbing.

---

## Non-Goals

1. **A shipped "preset" agent persona**: the backing agent is configured ad hoc per command via `agent_cfg`. No standing agent-builder persona ships in the framework. (Considered and dropped during design.)
2. **Agent-owned mutation**: the backing agent does not write project files or edit `bos.toml` through tools. The CLI owns all mutation deterministically; the agent proposes.
3. **Multi-agent / whole-topology generation**: instances generate one artifact at a time (one agent, one tool, …). Drafting entire topologies/workflows/channel graphs from prose stays out of scope (the broad BEP 9 deferral).
4. **Remote/gateway HITL transport**: the `AskUser` tool is terminal-bound (commands run on an interactive TTY). No new gateway transport for mid-turn questions.
5. **General config migration**: mutation only appends well-defined entries, matching BEP 9's TOML-mutation discipline. No refactoring of existing config.
6. **Mandatory worktree for every command**: the worktree lifecycle is opt-in per instance — only commands that change startup-loaded state (and thus risk gateway startup) must use it. Read-only or low-risk agent-backed commands may skip it.

---

## Design

### A. The agent-backed command harness (reusable)

A command adopts the harness by providing a small config: the generation prompt, the result schema/validator, the set of builtin tools/skills the agent may use, and (if mutation-risky) a verify hook. The harness provides the rest.

#### A1. In-process agent runner

Precedent: `boscli debug prompt` (`src/bos/cli/commands/debug.py:58-63`).

```python
ws = _bootstrapped_workspace(workspace_path)  # resolve_agents + bootstrap_platform


async def _run() -> str:
    async with ws.harness(...) as harness:  # isolated audit store (A3)
        agent = await harness.create_agent(
            kind=None,
            agent_cfg={
                "agent_name": "<command>-agent",
                "system_prompt": COMMAND_PROMPT,  # per-command
                "tools": [...],  # builtin read/inspect tools
                "plugins": {"enabled": ["*"]},  # builtin plugin tools + skills
                "local_tools": ask_user_registry,  # HITL tool (A2)
                "model": model,
            },
        )
        return await agent.ask(chat_id, intent)  # full tool/skill loop


result = _run_llm(_run())  # shared event loop (litellm-logging-safe)
```

`harness.create_agent` (`src/bos/core/harness.py:183-215`) deep-merges `agent_cfg` into `Agent(**kwargs)`; `Agent.__init__` (`src/bos/core/agent.py:283-341`) accepts `system_prompt`, `tools`, `plugins`, `model`, `agent_name`, `local_tools`. Running under `_run_llm` keeps the harness lifecycle and litellm background-logging correct.

#### A2. Human-in-the-loop `AskUser` tool

`Agent` already registers local tools (`AskSubagent`, `UpdateMemory`, `LoadSkill`). The harness injects an `AskUser` tool via a `ToolRegistry` passed as `agent_cfg["local_tools"]`, backed by `bos.cli.prompts.*` (`select`/`text`/`confirm`) since commands run on an interactive TTY. This lets the backing agent disambiguate intent or confirm choices mid-turn rather than guessing.

#### A3. Isolated, audit-retained message store

The backing agent must not pollute project chat history, but its conversations should be **persisted for audit** (not in-memory). The chat store accepts `store_dir` (`src/bos/config/template.toml:42-43`); the harness points the backing agent at a dedicated location. `InMemChatStore` exists but is rejected — it would lose the audit trail.

#### A4. Structured-output contract + deterministic mutation

The turn returns a typed result; the command parses/validates it in Python (reuse `_parse_specialists`-style extraction + name rules `_AGENT_NAME_RE`/`_SPECIALIST_NAME_RE`) and performs all file/`bos.toml` mutation via templates + tomlkit, in the style of `_render_agent_md` / `_append_actor` (`src/bos/cli/commands/scaffolding.py:629-648`).

#### A5. Worktree verify-before-apply lifecycle

```
<command> (intent)
  ├─ create git worktree of current repo            (no existing worktree helpers; _git_init/_inside_git_repo are precedents)
  ├─ run agent (A1–A3) + deterministic mutation (A4) INTO the worktree
  ├─ run the command's VERIFY HOOK in the worktree   (command-specific; see B)
  ├─ pass → commit on worktree branch → merge/cherry-pick into current branch → remove worktree
  └─ fail/reject → discard worktree (working tree untouched)
```

The verify hook is the per-command extension point: it returns pass/fail (and detail) given the mutated worktree.

### B. First instance — `boscli gen agent`

- **Generation**: prompt the backing agent to design one agent for the stated intent; result schema = `{name, description, system_prompt, tools[, model]}`.
- **Mutation**: write `agents/NAME.md` from `_shared/agent.md.tmpl`; register the agent / add `[runtime.actors.NAME]` via tomlkit; warn if `./agents` is absent from `agent_dirs`.
- **Verify hook**: run `boscli ask` against the new agent on a free port and expect a sensible reply, then stop the gateway. The backing agent decides smoke specifics (target `--agent <name>`, prompt). This is the chosen baseline depth (a real `ask` reply), motivated explicitly by the risk that a bad agent/config breaks `boscli gateway start`.
- **Merge-back**: commit in worktree + merge into current branch.

### C. `boscli ask` dependency (for verify hooks that smoke-run the project)

Today `boscli ask` (`src/bos/cli/commands/agent.py:345-384`) has **no `--agent` / `--port`** flag and intentionally **leaves the started gateway running** (`_ensure_gateway_endpoint`, `:203-225`). Verify hooks that smoke-run the project need either:
- `ask` extended with `--agent <kind>`, `--port <n>` (0 = auto-bind), and an ephemeral stop-after mode; **or**
- the hook orchestrates `gateway start --port 0` + `ask` + `gateway stop`.

The scaffolded config already defaults to `[runtime.gateway] port = 0`, making free-port binding the natural default.

### D. Future instances (illustrative, not committed here)

- **`gen tool`**: agent drafts an `@ep_tool` implementation + docstring/params; verify hook = import the module + confirm registration (and optionally a dry tool call).
- **`gen channel`**: agent proposes channel config; verify hook = bootstrap + gateway dry-start with the channel wired.
- Each instance supplies only a prompt, a result schema, and a verify hook.

---

## Affected Code

| Area | File(s) | Change |
|---|---|---|
| Harness building blocks | new module (e.g. `src/bos/cli/agent_backed.py`) | runner, `AskUser` tool, audit-store wiring, worktree lifecycle, verify-hook seam |
| In-process agent | `src/bos/core/harness.py` `create_agent`, `src/bos/core/agent.py` `ask` | reuse; possibly expose `local_tools` injection cleanly |
| First instance | `src/bos/cli/commands/scaffolding.py` (`gen_agent` `:597`) | add interactive/agent-backed mode using the harness |
| Verify dependency | `src/bos/cli/commands/agent.py` (`ask`, gateway lifecycle) | add `--agent`/`--port`/stop-after, or orchestrate start/stop |
| Audit store | chat-store `store_dir` config | dedicated generation store |
| Worktree | new git helpers (cf. `_git_init`, `_inside_git_repo`) | create / commit / merge / remove worktree |

---

## Open Issues (scope to expand)

1. **Harness API shape**: the exact interface a command implements to adopt the harness (prompt, result schema/validator, allowed tools, verify hook) — function, dataclass, or protocol.
2. **Generation prompt + result schemas** per instance; retry/repair on malformed output; validating proposed `tools` against the registered catalog.
3. **Which builtin tools/skills** the backing agent gets (read-only inspection vs broader) and how it discovers existing artifacts to avoid collisions.
4. **`boscli ask` extension shape**: `--agent`/`--port`/ephemeral-stop vs explicit start/stop orchestration; whether this is a sub-BEP.
5. **Worktree mechanics**: branch naming, location, dirty-tree handling, non-git projects, merge/cherry-pick strategy.
6. **Audit store location + retention**: per-project vs global; cleanup policy.
7. **Verify-hook taxonomy**: standard hooks (smoke `ask`, bootstrap, gateway dry-start, import-and-register) and how an instance composes/escalates them.
8. **Non-interactive / CI behavior**: how the agent-backed mode and the `AskUser` tool degrade without a TTY.
9. **Scope of instances**: which commands become agent-backed first after `gen agent` (`gen tool`, `gen channel`, others), and relationship to BEP 9.

---

## References

- BEP 9: Project Scaffolding and Guided Init (deterministic substrate; defers LLM-assisted generation — lifted here).
