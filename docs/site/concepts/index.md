# Concepts overview

This section explains the mental model behind BOS — how the pieces fit together,
what each term means, and where to go to learn more. If you are new to BOS, read
this page first, then follow the links into the pages that cover each concept in
depth.

---

## The big picture

A running BOS deployment is a **gateway process** that hosts one or more **actors**.
Each actor is a long-lived, addressable mailbox bound to an **agent** — an LLM-driven
turn loop. **Channels** bridge the outside world (the TUI, Telegram, Lark, HTTP
clients) to an actor's mailbox. Cross-cutting services — conversation persistence,
memory consolidation, background jobs, message routing — are owned by the **harness**
and selected by name in configuration.

```
              ┌─────────────────────────── gateway process ───────────────────────────┐
external      │  channel ──► mailbox ──► actor ──► agent (LLM loop) ──► tools/plugins │
client        │    ▲                       │              │                            │
(TUI/Telegram)│    └──────── reply ────────┘        harness services:                 │
              │                                     chat_store, consolidator,          │
              │                                     mail_route, job_runner             │
              └────────────────────────────────────────────────────────────────────────┘
```

Everything pluggable is a named **extension** registered at an **extension point**.
The agent itself is assembled from: a system prompt, a model, a set of **tools**, a
set of **plugins** (which each contribute more tools, prompt sections, and
interceptors), and config knobs. Configuration lives in one TOML file
(`.bos/config.toml`) plus optional Python extensions and Markdown/TOML agent files.

---

## Vocabulary

| Term | What it is |
|------|------------|
| **Agent** | An LLM-driven turn loop assembled from a system prompt, model, tools, and plugins. The agent is _assembled_ at runtime — it is not a persistent process. |
| **Actor** | A named, addressable, restartable runtime instance bound to one agent kind. The TOML key `[runtime.actors.<name>]` is the actor's identity and memory scope. |
| **Gateway** | The process that hosts actors + channels + an HTTP control plane. Started with `boscli gateway start`. |
| **Channel** | Bridges an external client to an actor's mailbox. Channels are long-lived and run for the life of the gateway. |
| **Harness** | The lifecycle owner of shared services: chat store, consolidator, mail route, job runner. Services are selected by name in `[harness]`. |
| **Extension Point** | A named registry of interchangeable implementations (e.g. `ep_tool`, `ep_channel`, `ep_plugin`). Core points are prefixed `ep_`; plugin-defined points are prefixed `pep_`. |
| **Extension** | One registered implementation at an extension point — a function or class decorated with e.g. `@ep_tool(...)`. |
| **Plugin** | A bundle that adds tools, prompt sections, and interceptors to an agent. Has two cooperating roles: a `HarnessPlugin` (one per process) and an `AgentPlugin` (one per agent). |
| **Skill** | A Markdown playbook the agent can load on demand via the `LoadSkill` tool. Only the name and description appear in the system prompt; the full body is fetched when needed (progressive disclosure). |
| **Tool** | An async function the LLM can call during a turn. Registered globally via `@ep_tool` or locally by a plugin's `register_tools`. |

---

## How concepts relate

```
config.toml
├── [platform]        extensions to load, env file, agent_dirs
├── [harness]         which chat_store / consolidator / mail_route / job_runner to use
├── [exts.ep.*.*]     per-extension configuration injected at invocation time
├── [agent.defaults]  base agent settings (model, tools, plugins, …)
├── [agents.<name>]   agent-specific overrides (or an external .md / .toml file)
└── [runtime]
    ├── [runtime.gateway]          host, port, api_key_env
    ├── [runtime.actors.<name>]    one actor per entry
    └── [[runtime.channels]]       zero or more persistent channels
```

Agents are assembled lazily when first needed. The extension registry is populated
at startup from `[platform].extensions` entries and entry-point discovery. Plugins
are instantiated once per process and bound once per agent. Channels run for the
life of the gateway.

---

## Where each concept is covered in depth

| Concept | Where to read |
|---------|---------------|
| Gateway, actors, channels, message flow | [Runtime & gateway](runtime.md) |
| Agent assembly, actor identity, multi-agent patterns | [Agents & actors](agents-and-actors.md) |
| Memory, consolidation, skills | [Memory & skills](memory-and-skills.md) |
| Extension points, tools, plugins, providers | [Architecture](../architecture/index.md) |
| Hands-on walkthroughs | [Tutorials](../tutorials/index.md) |

!!! tip "New to BOS?"
    The fastest path to understanding is to run
    [Tutorial 1](../tutorials/index.md) while keeping this vocabulary table
    open in a second tab.
