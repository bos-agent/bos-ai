# Architecture

!!! note "Stub"
    This page is a starting point. Expand each principle below into its own
    section as the design solidifies.

This section describes how BOS is built and the principles that guide its design.
It is the up-to-date source of truth — for the deeper, point-in-time design
rationale behind individual decisions, see the [BEP Index](beps.md).

## Design principles

- **From zero to agent in one command.** Sensible defaults; complexity is opt-in.
- **Single-file friendly, project-ready.** Start minimal, grow into a multi-agent
  project without rewrites.
- **Micro-kernel and plugins.** A small core with well-defined extension points
  rather than a monolith.
- **Clean architecture.** Dependencies point inward through concentric rings;
  the core stays free of framework and I/O concerns.

## Core concepts

- **Agent runtime / gateway** — the process that runs agents and routes messages.
- **Channels** — how users reach an agent (TUI, Telegram, …).
- **Memory** — platform-managed, with a consolidation agent.
- **Tools** — capabilities agents can call.

!!! info "Deeper detail"
    The [BEP Index](beps.md) links to the enhancement proposals that record the
    full design reasoning. BEPs capture point-in-time decisions and may contain
    detail that has since evolved — this section is the current reference.
