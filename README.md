# BOS AI

Lightweight, extensible infrastructure for building agentic systems in Python.

`bos-ai` is an actor-based framework for running LLM-driven agents with explicit
message routing, configurable runtime boundaries, and a small core. It is meant
to be shaped into different kinds of agentic systems rather than forcing one
opinionated product workflow.

## What It Is

- Fully configurable: agents, tools, skills, memories, channels, runtime, and
  storage are configured in TOML and wired through extension points.
- Actor-based: agents communicate through a `MailRoute` plus bound `MailBox`
  capabilities instead of direct in-process coupling.
- Built for composition: the same core can back a local TUI, an HTTP/WebSocket
  endpoint, a Telegram bot, or grouped channel topologies with `BroadcastChannel`.
- Small core, explicit boundaries: contracts, defaults, harness lifecycle,
  runtime orchestration, and extensions are kept separate.

## Privacy

BOS AI does not ship with built-in telemetry, analytics, or hosted data
collection. Your privacy boundary is defined by the models, tools, channels, and
storage backends you configure.

If you use your own local or self-hosted model and keep your channels and
storage local, BOS AI itself does not require sending your data to any BOS-owned
service.

## Quickstart

Install the package:

```bash
pip install bos-ai
```

Initialize a workspace:

```bash
mkdir my-agent
cd my-agent
bos init
```

Start the agent runtime:

```bash
bos start
```

Connect the built-in TUI:

```bash
bos tui
```

Useful lifecycle commands:

```bash
bos status
bos restart
bos stop
```

## Workspace Model

Each workspace gets a `.bos/config.toml`. BOS AI searches upward from the
current directory for `.bos/config.toml`, then falls back to `~/.bos/config.toml`
if no workspace-local config exists.

The primary actor is always addressed as `agent@main`. The selected main agent
implementation is configured separately under `[main].agent`.

Example channel configuration:

```toml
[main]
agent = "main"

[[main.channels]]
name = "BroadcastChannel"
bind_address = "channel@user"
target_address = "agent@main"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "channel@user"
host = "127.0.0.1"
port = 5920

#[[main.channels]]
#name = "TelegramChannel"
#bind_address = "channel@telegram"
#target_address = "channel@user"
#token = "123456:telegram-bot-token"
#poll_timeout = 30
#allowed_chat_ids = [123456789]
```

This keeps routing explicit:

- leaf channels may target `agent@main` directly
- leaf channels may target a `BroadcastChannel`
- `BroadcastChannel` must target `agent@main`

Deep broadcast trees are intentionally rejected.

## Runtime

The agent can run in-process or in Docker.

Docker runtime example:

```toml
[main.runtime]
kind = "docker"
image = "bos-ai:local"
workspace_dir = "/workspace"
```

Build and run:

```bash
docker build -t bos-ai:local .
bos start
bos tui
```

When Docker is enabled, `HttpChannel` host binding is normalized for container
access, and BOS AI publishes configured HTTP channel ports automatically.

## Extension Points

The framework is designed to be reconfigured and extended, not forked.

Core extension points include:

- `@ep_provider` for model backends
- `@ep_tool` for tool calls
- `@ep_memory_store` for long-term memory backends
- `@ep_message_store` for conversation history
- `@ep_mail_route` for message transport
- `@ep_react_interceptor` for ReAct loop interception
- `@ep_channel` for external interfaces like HTTP, Telegram, or grouped channels

Minimal example:

```python
from bos.core import ep_tool


@ep_tool(
    name="echo_upper",
    description="Return an uppercase version of the input.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
async def echo_upper(text: str) -> str:
    return text.upper()
```

Load extensions by adding their modules to `platform.extensions` in
`.bos/config.toml`.

## CLI

The built-in CLI currently exposes:

- `bos init`
- `bos auth`
- `bos start`
- `bos stop`
- `bos status`
- `bos restart`
- `bos tui`

Global workspace selection:

```bash
bos -w /path/to/workspace start
```

## What BOS AI Is Good For

- local-first agent runtimes
- custom tool-using assistants
- multi-agent or actor-based orchestration experiments
- private deployments with self-hosted models
- channel-driven agents exposed over HTTP or Telegram

If you want a fixed hosted product, this repo is probably too low-level. If you
want infrastructure for shaping your own agent runtime, this is the right layer.
