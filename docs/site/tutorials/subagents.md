# Tutorial 4 — Delegate to sub-agents

A single agent works well for general tasks, but some workflows benefit from
specialist agents: one that is great at research, one focused on writing, one
that only touches the filesystem. BOS supports two complementary patterns for
this — **delegation** (the main agent calls a specialist as a tool) and
**direct addressing** (each specialist has its own inbox). This tutorial covers
both.

This tutorial assumes you have the `my-agent/` project from
[Tutorial 2](create-a-project.md).

---

## Create agent files

Each `.md` or `.toml` file in `.bos/agents/` defines a named agent. The
**filename stem** is the agent name. Markdown files use YAML frontmatter for
config and the body as the system prompt.

Create `.bos/agents/researcher.md`:

```markdown
---
description: Researches topics and returns structured findings.
model: openai/gpt-4o
tools:
  enabled: [ReadFile, GrepSearch, WebSearch]
---
You are a diligent research assistant.

When asked to research a topic:
1. Search the web for recent, authoritative sources.
2. Cross-reference findings across at least two sources.
3. Return a structured summary with key facts and source references.

Do not write code. Do not ask the user questions. Deliver findings concisely.
```

Create `.bos/agents/writer.md`:

```markdown
---
description: Turns research findings into polished prose.
model: openai/gpt-4o
tools:
  enabled: [ReadFile, WriteFile]
---
You are a precise technical writer.

Given research findings or a brief, produce clear, well-structured prose.
Match the requested format (blog post, doc section, executive summary, etc.).
Do not invent facts. If something is unclear, flag it rather than guessing.
```

!!! note "Frontmatter rules"
    - Do **not** put `system_prompt` in the frontmatter — the Markdown body is
      the prompt. Adding it there causes a validation error.
    - If the frontmatter is invalid YAML, BOS uses the entire file content
      (including the frontmatter block) as the system prompt.
    - Frontmatter uses a small YAML subset: flat scalars, and one-level keys
      whose value is an **inline** list (`enabled: [ReadFile, GrepSearch]`) or
      scalar. Deeper block nesting (a `tools:` block with an indented
      `enabled:` block list beneath it) is **not** supported — write the list
      inline as shown above, or use a `.toml` agent file when you need richer
      structure.

---

## Pattern 1 — Delegation via `SubagentPlugin`

In this pattern, the main agent calls the `AskSubagent` tool, which runs a
specialist on a self-contained task and returns its result. The specialist gets
a fresh chat context (no shared history with main).

Enable the plugin in `config.toml`:

```toml
[agents.main.plugin-bindings.SubagentPlugin]
enabled = ["researcher", "writer"]
```

This tells `SubagentPlugin` that the `main` agent is allowed to delegate to
the `researcher` and `writer` agent kinds.

!!! tip "Wildcard"
    `enabled = ["*"]` permits delegation to **all** registered agent kinds.
    For tighter control, list them explicitly.

Restart the gateway and try it:

```bash
boscli gateway stop && boscli gateway start
```

In the TUI (`boscli tui`), ask:

```
Research the history of the CAP theorem and then write a 200-word summary.
```

The main agent will call `AskSubagent` with `researcher` and then `writer`,
assembling the final result from their outputs.

### How `AskSubagent` works

`SubagentPlugin` registers an `AskSubagent` tool on each agent that binds it.
When the main agent calls it, BOS:

1. Creates a new agent instance for the named specialist kind.
2. Runs a single turn with the delegated task as its prompt.
3. Returns the specialist's final reply as the tool result.

The specialist sees only its own agent config (system prompt, tools, model).
It does not see the main agent's conversation history.

### Alternative: bind at the actor level

If you want different actors to delegate to different sub-agents, you can put
the binding under `[runtime.actors.<name>.agent_cfg]` instead:

```toml
[runtime.actors.main.agent_cfg.plugin-bindings.SubagentPlugin]
enabled = ["researcher", "writer"]
```

This overrides the agent-level config for this actor only.

---

## Pattern 2 — Direct addressing with separate actors

In this pattern, each specialist has its own **actor** — its own long-lived
inbox. Users (or channels) can address it directly with `@name`.

Add to `config.toml`:

```toml
[runtime.actors.researcher]
agent = "researcher"
display_name = "Researcher"

[runtime.actors.writer]
agent = "writer"
display_name = "Writer"
```

After restarting the gateway, you can address the researcher directly in the
TUI by typing `@researcher` in your message, or by selecting the actor in the
UI. The researcher runs with its own memory scope and conversation history,
isolated from `main`.

!!! note "Combine both patterns"
    A specialist can be both an actor (directly addressable by users) and an
    allowed sub-agent of `main` (callable via `AskSubagent`). Just define it
    under `[runtime.actors.<name>]` **and** include it in
    `SubagentPlugin.enabled`.

---

## The `team` preset

BOS ships a built-in `team` preset that pre-configures a main actor plus a
researcher and writer, wired together with both patterns above:

```bash
boscli -c team gateway start
boscli -c team tui
```

Use it to explore the multi-agent setup before adapting it to your own project.

---

## What you learned

- Create agent files in `.bos/agents/` as Markdown (frontmatter + body) or
  TOML. The filename stem is the agent name.
- `SubagentPlugin` gives the main agent an `AskSubagent` tool; the
  `plugin-bindings.SubagentPlugin.enabled` list controls which specialists it
  can call.
- Specialists run with a fresh chat context — no shared history with the
  calling agent.
- Giving a specialist its own `[runtime.actors.<name>]` entry makes it
  directly addressable with `@name`.
- Both patterns can be combined: an actor can also be a delegatable
  sub-agent.

**Next:** [Connect a channel](channels.md) — attach a Telegram bot so users
can talk to your agent from outside the terminal.
