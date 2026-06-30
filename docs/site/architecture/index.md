# Architecture

This section describes how BOS is built and the principles that guide its design. It is the
up-to-date source of truth — for the deeper, point-in-time rationale behind individual
decisions, see the [BEP Index](beps.md).

## Design principles

- **From zero to agent in one command.** Sensible defaults; complexity is opt-in. A bare
  config runs; every layer (channels, sub-agents, custom tools) is something you add.
- **Minimal to start, project-ready.** Start minimal and grow into a multi-agent project
  by editing one TOML file and dropping files into conventional directories — no rewrites.
- **Micro-kernel and plugins.** A small core with well-defined **extension points** rather
  than a monolith. Tools, providers, channels, stores, consolidators, and plugins are all
  named, interchangeable implementations registered at an extension point.
- **Clean architecture.** Dependencies point inward through concentric rings; the agent core
  stays free of framework and I/O concerns. Outer rings (harness, gateway, CLI) depend
  inward on the core, never the reverse.

## How the pieces fit

```
                 ┌─────────────────────────── gateway process ───────────────────────────┐
 external client │   channel ──▶ mailbox ──▶ actor ──▶ agent (LLM loop) ──▶ tools/plugins  │
 (TUI/Telegram)  │     ▲                        │              │                           │
                 │     └──────── reply ─────────┘        harness services:                 │
                 │                                       chat_store, consolidator,         │
                 │                                       mail_route, job_runner            │
                 └────────────────────────────────────────────────────────────────────────┘
```

- **Gateway** — the process that hosts actors and channels and serves a control plane.
  See [Runtime & gateway](../concepts/runtime.md).
- **Actors & agents** — named, addressable runtime instances bound to LLM-driven agents.
  See [Agents & actors](../concepts/agents-and-actors.md).
- **Channels** — how users reach an agent (TUI, Telegram, Lark, HTTP).
  See [Writing channels](../extending/channels.md).
- **Harness** — the lifecycle owner of shared services (chat persistence, memory
  consolidation, message routing, background jobs), each selected by name in `[harness]`.
- **Extension points** — the micro-kernel seams. See [Extending BOS](../extending/index.md).
- **Memory & skills** — platform-managed memory with off-turn consolidation, and
  progressively-disclosed skill playbooks. See [Memory & skills](../concepts/memory-and-skills.md).

## Where to read more

- **[Concepts](../concepts/index.md)** — the mental models in depth.
- **[Configuration](../configuration/index.md)** — how each part is configured.
- **[Extending BOS](../extending/index.md)** — the extension-point model and how to build on it.
- **[`llm-full.md`](https://github.com/bos-agent/bos-ai/blob/main/src/bos/llm-full.md)** — a single dense, code-grounded reference of every
  mechanism, intended for ingestion by an AI agent. `boscli init` drops a copy into every project.

!!! info "Deeper detail"
    The [BEP Index](beps.md) links to the enhancement proposals that record the full design
    reasoning. BEPs capture **point-in-time** decisions and may contain detail that has since
    evolved — this section is the current reference.
