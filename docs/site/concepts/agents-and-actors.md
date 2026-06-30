# Agents & actors

BOS draws a deliberate line between the _specification_ of what an agent does (its
prompt, model, tools, and plugins) and the _runtime identity_ under which it runs (the
actor). This page explains both, how they are assembled, and how multi-agent patterns
compose on top of them.

See [Concepts overview](index.md) for the full vocabulary table, and [Runtime &
gateway](runtime.md) for how actors live inside the gateway process.

---

## What an agent is

An **agent** is an LLM-driven turn loop assembled at runtime from:

- A **system prompt** — the base instruction set, extended each turn by plugin
  prompt sections.
- A **model** — any LiteLLM-style `provider/model` string (e.g. `openai/gpt-4o`,
  `gemini/gemini-2.5-flash`, `anthropic/claude-...`).
- A **tool set** — async functions the LLM may call, drawn from the global `ep_tool`
  registry and any agent-local tools registered by plugins.
- A set of **plugins** — each plugin contributes tools, prompt sections, and
  interceptors and may hold per-agent state.
- Config **knobs** — `max_tokens`, `max_iterations`, `reasoning_effort`,
  `tool_noise_filter`, `history_attribution`, and more.

Agents are assembled lazily; the spec (name, prompt, model, tool/plugin lists) is
resolved at startup, but the object that runs turns is built on first use.

---

## What an actor is

An **actor** is a named, addressable, restartable runtime instance bound to one
agent kind. Where the agent is a _specification_, the actor is the _identity_.

```toml
[runtime.actors.researcher]
agent        = "researcher"    # which agent kind to run
display_name = "Researcher"
```

The TOML key (`researcher`) is the actor's address (`agent@researcher`), its memory
scope (MemoryPlugin isolates memories per actor name), and its mention token
(`@researcher` in channels). Two actors can run the _same_ agent kind but have
separate histories and memories because their keys differ.

Actors are long-lived and restartable:

```toml
[runtime.actors.main]
agent             = "main"
restart_on_error  = true
max_restarts      = 5

[runtime.actors.main.agent_cfg]
model = "openai/gpt-4o"         # per-actor model override (same shape as [agent.defaults])
```

`agent_cfg` accepts the same fields as `[agent.defaults]` and layers on top of the
agent spec, so you can run identical agent logic under two actors with different
models or plugin bindings.

---

## Agent resolution and the merge chain

For every agent name, the final specification is a **deep merge in this order**:

```
[agent.defaults]
        ↓ merged with
@ep_agent factory result  (if a code-defined factory exists for this name)
        ↓ merged with
[agents.<name>] / external file  (if defined)
```

This means:

- `[agent.defaults]` applies to every agent — put your project-wide model, tool
  list, and plugin list here.
- A registered `@ep_agent` factory (a Python function decorated with
  `@ep_agent(name="...")`) can synthesise an agent spec programmatically; users can
  still override the result in `[agents.<name>]`.
- `[agents.<name>]` or an external file is the highest-priority layer and fully
  replaces any matching key from the layers below.

The built-in `BOS` agent — a general-purpose assistant with memory, planning, task,
skills, and subagent plugins — is a normal builtin `@ep_agent` extension, registered
when `bos.exts` is loaded (the default). Reference it by name (`agent = "BOS"`), override
it via `[agents.BOS]`, or inherit from it with `_parent = "BOS"`.

### Inline agent config

```toml
[agent.defaults]
model       = "openai/gpt-4o"
max_tokens  = 131072

[agents.researcher]
description   = "Researches codebases."
system_prompt = "You research codebases and report findings."
model         = "openai/gpt-4o"

[agents.researcher.tools]
enabled = ["ReadFile", "GrepSearch", "WebSearch"]
```

### External agent files

Every `*.toml` or `*.md` file in an `agent_dirs` directory defines one agent. The
filename stem is the agent name unless the file overrides it with a `name` field.
External files **replace** (not merge with) an inline `[agents.<name>]` of the same
name.

**Markdown format** (recommended for agents with long system prompts):

```markdown
---
description: Writes documentation.
model: openai/gpt-4o
tools:
  enabled: [ReadFile, WriteFile]
---
You are a meticulous technical writer. Produce clear, accurate docs.
```

The frontmatter becomes config fields; the Markdown body becomes `system_prompt`.
Putting `system_prompt` in the frontmatter is rejected — the body is the prompt.
Frontmatter supports a small YAML subset (flat scalars and one-level keys with
**inline** lists, like `enabled: [ReadFile, WriteFile]`); for deeper structure
use a `.toml` agent file.

**TOML format** (`agents/researcher.toml`): a flat `AgentConfig` table, useful when
you need structured config without a long prose prompt.

---

## Tool resolution

An agent sees a **merged, filtered view** of two registries:

1. **Agent-local tools** — registered by plugins via `register_tools`. These take
   precedence on name collision.
2. **Global `ep_tool` registry** — all tools registered with `@ep_tool`.

The `enabled` and `disabled` lists in `[agents.<name>.tools]` (or
`[agent.defaults.tools]`) then filter the merged set:

```toml
[agents.researcher.tools]
enabled  = ["ReadFile", "GrepSearch", "WebSearch"]  # explicit allow-list
# enabled  = ["*"]                                  # or: all tools
# disabled = ["WriteFile"]                          # exclude from the all-tools set
```

Per-tool guidance shown to the model can be overridden via `usages`:

```toml
[agents.researcher.tools]
enabled = ["*"]
usages  = { WebSearch = "Always search before answering factual questions." }
```

---

## Multi-agent patterns

BOS supports two composable patterns. You can use either or both at the same time.

### Pattern 1: Delegation via SubagentPlugin (one inbox)

Keep a single `main` actor and give it the `SubagentPlugin`. The main agent can
invoke any of its enabled subagents by calling the `AskSubagent` tool, which runs
the specialist agent in-process and returns its result. Subagents run with a fresh
chat history (no shared context).

```toml
[agents.main.plugin-bindings.SubagentPlugin]
enabled = ["researcher", "writer"]   # which agent kinds main may delegate to
```

This is the right pattern when:

- The main agent decides _when_ to delegate.
- Specialist results are returned inline to the main conversation.
- You do not need specialists to be independently reachable by end users.

### Pattern 2: Direct addressing (many inboxes)

Give a specialist its own actor in `[runtime.actors]`. Users and channels can then
address it directly with `@name` mentions, and it has its own persistent mailbox,
memory, and conversation history.

```toml
[runtime.actors.researcher]
agent = "researcher"

[runtime.actors.writer]
agent = "writer"
```

This is the right pattern when:

- End users need to talk directly to a specialist.
- Each specialist should maintain its own independent history and memory.

### Combining both

A specialist can simultaneously be an actor (directly addressable) **and** an
allowed subagent of `main`. The `team` preset (`boscli -c team gateway start`)
demonstrates this: it configures a `main` actor with `SubagentPlugin` and also
registers each specialist as its own actor.

```
     user
      │
      ├──► @main   ──► AskSubagent("researcher") ──► researcher agent
      │                                            └─► writer agent
      │
      ├──► @researcher   (directly addressable)
      └──► @writer       (directly addressable)
```

!!! tip "When to choose which pattern"
    Start with SubagentPlugin delegation (pattern 1) — it is simpler and requires
    only one channel entry. Add dedicated actors (pattern 2) when users need to
    talk directly to a specialist or when specialists need their own persistent
    memory scope.

For the hands-on tutorial see [Delegate to sub-agents](../tutorials/index.md).
