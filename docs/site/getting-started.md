# Getting Started

!!! note "Stub"
    This page is a starting point. Expand it with prerequisites, configuration,
    and a full first-project walkthrough.

## Install

```bash
pip install bos-ai
```

## Create a project

```bash
mkdir my-agent && cd my-agent
boscli init          # guided setup: purpose, topology, model — writes a runnable baseline
boscli gateway start # start the agent runtime
boscli tui           # connect the terminal UI
```

## Grow the project

```bash
boscli gen agent <name>      # add a specialist agent
boscli gen tool <Name>       # add a custom tool stub
boscli gen channel telegram  # wire a Telegram bot
boscli doctor                # check config, paths, env, credentials
```
