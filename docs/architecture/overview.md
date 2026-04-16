# Architecture Overview

`bos-ai` is organized around four top-level concerns:

- `bos.protocol`
  Shared message contracts used across components, especially transport-facing envelopes and message kinds.
- `bos.core`
  Runtime framework primitives: contracts, default implementations, agent loop, actor loop, harness lifecycle, registries, and internal helpers.
- `bos.config`
  Workspace discovery and TOML-backed configuration loading.
- `bos.runner`
  In-process composition of harness, actor, broadcast, and channels.

The core design rule is:

- `protocol` defines cross-component message contracts.
- `core` defines runtime service contracts and implementations.
- `config` loads and shapes configuration.
- `runner` builds a runtime topology from a workspace.

## Main Runtime Flow

1. `Workspace` locates `.bos/config.toml` and loads configuration.
2. `workspace.bootstrap_platform()` loads extension modules and registers configured agents.
3. `runner.start(workspace)` creates an `AgentHarness`.
4. The harness builds shared services such as mailbox, stores, interceptor chain, and LLM client.
5. The runner creates an `AgentActor` and configured channels.
6. Channels translate external traffic into `Envelope` objects and send them through the mailbox.
7. The actor drives `ReactAgent`, which uses tools, stores, and providers through the harness-owned services.

## Package Boundaries

- `bos.protocol` should stay transport-neutral and stable.
- `bos.core.contract` owns runtime contracts and extension points.
- `bos.core.defaults` owns built-in `_default` implementations.
- `bos.core._utils` owns private helper functions shared inside `bos.core`.
- `bos.core.__init__` is a package surface, not a logic module.

## Design Intent

- Keep the local single-process path simple.
- Keep clustered or multi-process evolution possible through mailbox/channel abstractions.
- Prefer explicit package boundaries over a giant central module.
