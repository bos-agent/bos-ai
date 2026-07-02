# BOS

> An agent out of the box. A framework for your own agent-native applications.

**BOS** gives you a working agent in one command — then grows with you into your
own agent-native application, from a single assistant to a multi-agent system.

## Quick Start

```bash
pip install bos-ai
OPENAI_API_KEY=<api-key> boscli ask "how are you" --model openai/gpt-4o
```

Prefer to set the credentials and model once instead of repeating them on every
call? Export them into your environment — `boscli ask` reads `BOS_MODEL` when
`--model` is omitted:

```bash
export OPENAI_API_KEY=<api-key>
export BOS_MODEL=openai/gpt-4o
boscli ask "how are you"
```

Using a different provider? See LiteLLM's [provider docs](https://docs.litellm.ai/docs/providers)
for the right `BOS_MODEL` prefix and required environment variables. For example, with a
DeepSeek model:

```bash
export DEEPSEEK_API_KEY=<api-key>
export BOS_MODEL=deepseek/deepseek-v4-pro
boscli ask "how are you"
```

Alternatively, use [uv](https://docs.astral.sh/uv/)'s `uvx` to start without installing:

```bash
export OPENAI_API_KEY=<api-key>
export BOS_MODEL=openai/gpt-4o
uvx boscli ask "how are you"
```

## Where to go next

- **[Getting Started](getting-started.md)** — set up a project and run the agent runtime.
- **[Tutorials](tutorials/index.md)** — a hands-on path from your first agent to a packaged,
  shareable extension.
- **[Concepts](concepts/index.md)** — how the runtime, agents, actors, memory, and skills fit
  together.
- **[Configuration](configuration/index.md)** — the complete `config.toml` reference.
- **[Extending BOS](extending/index.md)** — write tools, plugins, channels, and providers.
- **[CLI](cli/index.md)** — the `boscli` commands you'll use day to day.
- **[Architecture](architecture/index.md)** — the design and principles behind BOS,
  plus an index of the enhancement proposals (BEPs) that record deeper design decisions.

!!! info "Building on BOS with an AI agent?"
    Every project scaffolded by `boscli init` includes an `llm-full.md` — a single dense
    reference that covers every mechanism (configuration, extension points, plugins, channels,
    skills, the CLI, and the runtime) in one file, intended for ingestion by an AI coding agent.
    Source: [`src/bos/llm-full.md`](https://github.com/bos-agent/bos-ai/blob/main/src/bos/llm-full.md).
