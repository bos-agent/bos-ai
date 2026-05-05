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
  endpoint, or a Telegram bot. Server-side chat cursors let one client
  resume threads created from another client.
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
current directory for `.bos/config.toml`. If no workspace-local `.bos` exists,
set `BOS_DIR` explicitly. If both an ancestor `.bos` and `BOS_DIR` are present
but point to different locations, BOS AI fails with an ambiguity error instead
of silently choosing one.

Agent definitions may stay inline in `.bos/config.toml` or be placed in external
directories. By default, `.bos/agents/` is auto-scanned. To customize the scan
locations:

```toml
[platform]
agent_dirs = ["agents", "../shared-agents", "~/.bos/agents", "/opt/agents"]
```

Relative paths are resolved against `.bos/`; absolute paths and `~` are
supported. External agents are `<dir>/<name>.toml` files or `<dir>/<name>.md`
files. For Markdown agents, optional YAML-style frontmatter supplies settings
such as `name`, `description`, `tools`, and `exclude_tools`; the Markdown body is
the `system_prompt`. If `name` is provided in the file it is used as-is; if
omitted, the name is derived from the filename stem. Inline agents load first,
external agents load second (alphabetically within each directory, directories
processed in list order), exact-name duplicates are last-wins, and case-only
collisions are errors. `Workspace.config` stays as the raw manifest, and
runtime-facing agent config is produced through a resolved platform view.

For the full rule set and resolver behavior, see
`docs/architecture/config-workspace.md`.

By default, the primary actor is addressed as `agent@main`. The selected main
agent implementation is configured separately under `[main].agent`.

Example channel configuration:

```toml
[main]
agent = "main"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
host = "127.0.0.1"
port = 5920

#[[main.channels]]
#name = "TelegramChannel"
#bind_address = "channel@telegram"
#target_address = "agent@main"
#token = "123456:telegram-bot-token"
#poll_timeout = 30
#allowed_chat_ids = [123456789]
```

This keeps routing explicit:

- channels target `agent@main` directly
- client cursors are stored server-side by `client_id`
- aliases can point to durable `chat_id` values for cross-client resume

### Named Actors

Named Actors let one runtime host multiple long-lived, addressable actors. The
actor key under `[main.actors]` is the route name, mailbox identity, and memory
scope. The `agent` field is the reusable agent kind, so multiple actors can
instantiate the same agent definition with different runtime identities.

```toml
[main.actors.main]
agent = "assistant"
display_name = "Main"

[main.actors.bob]
agent = "architect"
display_name = "Bob"

[main.actors.investment]
agent = "assistant"
display_name = "Investment"
```

Channels that support mention routing can route a message such as `@bob review
this design` to `agent@bob`. Channels can also bind directly to a specific actor
with `target_address = "agent@investment"`.

Named Actors is a routing feature, not autonomous swarm orchestration. Planning,
delegation lifecycle, actor-to-actor relay, and result synthesis belong in a
separate orchestration layer.

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

Named agents are capability-deny by default. Omitted `tools`, `skills`,
`memories`, and `subagents` settings are treated as empty allow-lists. Configure
each agent with explicit allow-lists, or use `"*"` to allow all names in that
capability group:

```toml
[[platform.agents]]
name = "main"
tools = ["ReadFile", "SearchFiles"]
skills = "*"
memories = ["user", "memory"]
subagents = ["researcher"]
```

`exclude_tools`, `exclude_skills`, `exclude_memories`, and `exclude_subagents`
can still be used to subtract names from an allow-list, including from `"*"`.
Scratch agents created directly through `harness.create_agent()` without a named
agent config also start with no tools, skills, memories, or subagents.

## Skills

Skills are discovered from configured skill directories. A skill is a directory
containing `SKILL.md`; the directory name is the skill name. Configure workspace
skill roots with:

```toml
[harness.skills_loader]
skill_dirs = ["skills"]
```

Agents see allowed skills in the system prompt with each skill's `SKILL.md`
location and summary. `LoadSkill` reads and returns the full `SKILL.md`
instructions as a tool result; it does not permanently mutate the agent's
system prompt.

## Subagent Orchestration

BOS already supports a lightweight form of subagent orchestration inside one
active harness. The pattern is:

- register multiple named agent profiles under `platform.agents`
- allow the parent agent to use the local `AskSubagent` tool
- scope which specialists it may call with `subagents = [...]`
- customize specialist behavior under `[[harness.subagents]]`

This is delegated orchestration, not a second long-running actor process. The
parent agent stays at `agent@main`, while specialist agents are invoked on
demand as additional `agent.ask(...)` calls with their own chat IDs.

Minimal use case: a manager agent handles the user-facing chat and
delegates focused document analysis to a `researcher` subagent.

```toml
[platform.agent_defaults]
tools = []
subagents = []

[[platform.agents]]
name = "main"
description = "User-facing manager."
tools = ["AskSubagent"]
subagents = ["researcher"]
system_prompt = """
Handle the user-facing workflow. Delegate focused repo analysis when needed.
"""

[[platform.agents]]
name = "researcher"
description = "Focused repo analyst."
tools = []
system_prompt = """
You only perform focused research tasks delegated by the main agent.
Return concise findings.
"""

[[harness.subagents]]
name = "researcher"
task_template = """
You are the {agent_name} specialist.
Use this dedicated thread for delegated analysis work.

Task:
{task}
"""
```

`AskSubagent` always runs the specialist in a fresh child thread. The runtime
derives that child `chat_id` from the current parent thread plus a
short agent tag and random suffix, for example
`parent-chat~researcher1a2b3c4d`.

## Extension Points

The framework is designed to be reconfigured and extended, not forked.

Core extension points include:

- `@ep_provider` for model backends
- `@ep_tool` for tool calls
- `@ep_memory_store` for long-term memory backends
- `@ep_message_store` for chat history
- `@ep_mail_route` for message transport
- `@ep_turn_interceptor` for turn loop interception
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
