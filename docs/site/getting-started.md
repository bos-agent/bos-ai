# Getting Started

!!! note "Stub"
    This page is a starting point. Expand it with prerequisites, configuration,
    and a full first-project walkthrough.

## Install

```bash
pip install bos-ai
```

## First run

```bash
OPENAI_API_KEY=<api-key> boscli ask "how are you" --model openai/gpt-4o
```

Using a different provider? See LiteLLM's [provider docs](https://docs.litellm.ai/docs/providers)
for the right `BOS_MODEL` prefix and required environment variables.

## Create a project

```bash
mkdir my-agent && cd my-agent
boscli init          # guided setup: purpose, topology, model — writes a runnable baseline
boscli gateway start # start the agent runtime
boscli tui           # connect the terminal UI
```

## Grow the project

Edit the generated `.bos/config.toml` to extend the project — it ships with
commented-out blocks for sub-agent delegation (`SubagentPlugin`) and a Telegram
channel; uncomment and configure the one you need.
