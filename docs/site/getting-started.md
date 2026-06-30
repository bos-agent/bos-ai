# Getting Started

This page takes you from nothing to a running, persistent agent project. For a guided,
example-first path, follow the [Tutorials](tutorials/index.md); this page is the quick
linear version.

## Prerequisites

- **Python ≥ 3.13.**
- **An LLM API key** for a provider [LiteLLM supports](https://docs.litellm.ai/docs/providers)
  (e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`).

## Install

```bash
pip install bos-ai
```

Or run without installing, using [uv](https://docs.astral.sh/uv/):

```bash
uvx boscli ask "how are you" --model openai/gpt-4o
```

Optional extras: `pip install 'bos-ai[search]'` (Tavily web search), `'bos-ai[lark]'`
(Lark/Feishu channel), or `'bos-ai[all]'`.

## First run (no project)

```bash
OPENAI_API_KEY=<api-key> boscli ask "how are you" --model openai/gpt-4o
```

The model string is `provider/model`. The provider prefix selects a custom
[provider](extending/stores-and-providers.md) if one is registered under that name; otherwise
it falls back to LiteLLM, which reads the matching `*_API_KEY` environment variable. For a
DeepSeek model, for example:

```bash
DEEPSEEK_API_KEY=<api-key> boscli ask "how are you" --model deepseek/deepseek-v4-pro
```

## Create a project

```bash
mkdir my-agent && cd my-agent
boscli init          # guided setup: purpose, archetype, provider/model
boscli gateway start # start the agent runtime
boscli tui           # connect the terminal UI
```

`boscli init` writes a runnable baseline under `.bos/`:

```
.bos/
├── config.toml      # your one config file
├── .env             # provider keys and secrets
├── agents/          # add specialist agents here (*.md / *.toml)
├── extensions/      # add custom tools here (*.py)
└── skills/          # add skills here (dirs with SKILL.md)
```

To see every option documented inline, run `boscli init --minimal` in an empty directory —
it emits the fully-commented reference config.

## Verify your setup

```bash
boscli inspect       # show the resolved harness, config, agents, and actors
```

## Grow the project

- **Add a tool** — drop a Python file with an `@ep_tool` into `.bos/extensions/`.
  See [Add a custom tool](tutorials/custom-tool.md).
- **Add specialists** — create `agents/researcher.md` and let your main agent delegate to it.
  See [Delegate to sub-agents](tutorials/subagents.md).
- **Reach the agent over chat** — add a Telegram or Lark channel.
  See [Connect a channel](tutorials/channels.md).
- **Tune behavior** — edit `.bos/config.toml`. See the
  [Configuration reference](configuration/index.md).

## Where to go next

- **[Tutorials](tutorials/index.md)** — a hands-on path from your first agent to a packaged,
  shareable extension.
- **[Concepts](concepts/index.md)** — how the runtime, agents, actors, memory, and skills fit
  together.
- **[Configuration](configuration/index.md)** — the complete `config.toml` reference.
- **[Extending BOS](extending/index.md)** — write tools, plugins, channels, and providers.
