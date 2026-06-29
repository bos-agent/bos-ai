# Tutorials

These tutorials walk you from a single `boscli ask` command to a packaged,
shareable extension — each one building on the last. By the end you will have
a working multi-agent project with custom tools, a persistent channel, and a
Python package you can install in any BOS project.

## Prerequisites

- **Python 3.13 or newer** — BOS requires it; check with `python3 --version`.
- **An API key** for at least one LLM provider (OpenAI, Anthropic, Gemini,
  DeepSeek, …). BOS uses [LiteLLM](https://docs.litellm.ai/docs/providers)
  under the hood, so most providers work out of the box with the right env var.
- **`bos-ai` installed** — or use `uvx boscli` to run without installing.
  See [Tutorial 1](first-agent.md) for the exact install command.

!!! tip "New to BOS?"
    Start at Tutorial 1 and follow in order. Each page ends with a **Next**
    link and a summary of what you learned, so it's easy to pick up where you
    left off.

## The tutorial track

| # | Tutorial | What you learn |
|---|----------|----------------|
| 1 | [Your first agent](first-agent.md) | Install BOS, set an API key, and run a one-shot query from the command line. |
| 2 | [Create a project](create-a-project.md) | Scaffold a project with `boscli init`, understand the generated layout and config, and start the gateway + TUI. |
| 3 | [Add a custom tool](custom-tool.md) | Drop a Python file into `.bos/extensions/`, decorate a function with `@ep_tool`, and have the agent call it. |
| 4 | [Delegate to sub-agents](subagents.md) | Create specialist agents in Markdown files and wire them up with `SubagentPlugin` so the main agent can delegate tasks. |
| 5 | [Connect a channel](channels.md) | Attach a Telegram bot so users can talk to your agent from their phone. |
| 6 | [Package & share extensions](package-extension.md) | Turn your extensions into an installable Python package that registers itself automatically in any BOS project. |

## How the tutorials fit together

```
Tutorial 1 ──► Tutorial 2 ──► Tutorial 3 ──► Tutorial 4
  (install)      (project)      (tools)       (sub-agents)
                                                  │
                                          Tutorial 5 ──► Tutorial 6
                                          (channels)     (packaging)
```

Tutorials 3–6 all build on the project created in Tutorial 2. Keep the
`my-agent/` directory around as you work through the track.
