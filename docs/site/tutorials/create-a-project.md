# Tutorial 2 — Create a project

`boscli ask` is great for one-off queries, but a real agent needs a home:
persistent memory, a running runtime, a terminal UI, and a place to put custom
tools and agents. This tutorial walks through creating that project.

---

## Scaffold with `boscli init`

```bash
mkdir my-agent && cd my-agent
boscli init
```

The guided flow asks three questions:

1. **Purpose** — a short description of what your agent is for.
2. **Archetype** — `workspace` (plain directory with a `./extensions/` folder)
   or `package` (installable Python package under `src/<pkg>/`). For now choose
   **workspace**; see [Tutorial 6](package-extension.md) for the package route.
3. **Provider / model** — BOS detects which `*_API_KEY` env vars are set and
   offers them as choices, or you can type any `provider/model` string.

After answering, `boscli init`:

- Writes `.bos/config.toml` and `.bos/.env`.
- Creates starter directories and an example tool in `.bos/extensions/`.
- Optionally runs a one-sentence probe to verify your credentials work
  (`--no-probe` skips it).
- Optionally runs `git init`.

!!! tip "Want every config option documented?"
    Run `boscli init --minimal` in an empty directory to emit the
    fully-commented `template.toml` — it shows every key with its default and
    a short explanation.

---

## The generated layout

```
my-agent/
└── .bos/
    ├── config.toml        ← the one config file
    ├── .env               ← secrets (gitignored)
    ├── agents/            ← external agent files (*.md / *.toml)
    ├── extensions/        ← project-local Python extensions
    │   └── project_tools.py
    ├── skills/            ← project-local skills (dirs with SKILL.md)
    ├── messages/          ← chat persistence (JsonlChatStore, created on first run)
    ├── mailboxes/         ← actor-to-actor mail (JsonlMailRoute, created on first run)
    └── gateway.state      ← port/PID written by the gateway so clients can find it
```

All relative paths in `config.toml` (like `./extensions` and `./agents`) are
resolved relative to `.bos/` (the **bos_dir**), not the project root.

---

## Walking through `config.toml`

Here is a typical generated config for a workspace project:

```toml
[platform]
envfile = ".env"
extensions = ["bos.exts", "./extensions"]
agent_dirs = ["./agents"]

[agent.defaults]
model = "openai/gpt-4o"

[agent.defaults.tools]
enabled = ["*"]

[agent.defaults.plugins]
enabled = ["*"]

[agents.main]
description = "Main agent for my-agent"
system_prompt = """
You are the main agent of this project.
...
"""

[runtime]
main_actor = "main"

[runtime.gateway]
port = 0

[runtime.actors.main]
agent = "main"
display_name = "Main"
```

### `[platform]`

Controls environment loading and extension discovery.

| Key | What it does |
|-----|--------------|
| `envfile` | Dotenv file loaded at startup (`override=True` — takes priority over the shell). |
| `extensions` | Modules or paths to import. `"bos.exts"` loads all built-ins and reads `bos.exts` entry points; `"./extensions"` scans that directory for `.py` files. |
| `agent_dirs` | Directories scanned for `*.md` / `*.toml` agent files. Filename stem = agent name. |

### `[agent.defaults]` and `[agents.main]`

`[agent.defaults]` sets defaults for every agent. `[agents.main]` defines the
`main` agent that the `main` actor will run.

| Key | Meaning |
|-----|---------|
| `model` | LiteLLM-style `provider/model`. Precedence: per-agent → defaults → `BOS_MODEL` env. |
| `tools.enabled = ["*"]` | Give the agent access to all registered tools. |
| `plugins.enabled = ["*"]` | Enable all registered plugins (`MemoryPlugin`, `PlanPlugin`, `TaskPlugin`, `SkillsPlugin`, `SubagentPlugin`). |
| `system_prompt` | The agent's base system prompt. Plugins append sections at runtime. |

### `[runtime]` and `[runtime.actors.main]`

| Key | Meaning |
|-----|---------|
| `main_actor = "main"` | The default route target for messages that don't specify an actor. |
| `[runtime.gateway] port = 0` | Auto-assign a free port; the actual port is written to `gateway.state`. |
| `[runtime.actors.main] agent = "main"` | This actor runs the `main` agent kind. |

---

## Start the gateway

```bash
boscli gateway start
```

The gateway boots in the background, writing its port and PID to
`.bos/gateway.state`. You can inspect it:

```bash
boscli gateway status
```

---

## Connect the TUI

```bash
boscli tui
```

The terminal UI connects to the running gateway and opens an interactive chat
session. Type a message and press Enter — your agent will reply.

To stop the gateway:

```bash
boscli gateway stop
```

---

## Putting secrets in `.env`

Your API key should go in `.bos/.env`, not in `config.toml` or your shell
profile:

```bash
# .bos/.env
OPENAI_API_KEY=sk-...
```

The file is gitignored by default. `boscli init` adds the key you used during
setup automatically.

---

## What you learned

- `boscli init` scaffolds a complete workspace with config, extensions, agents,
  and skills directories.
- `.bos/config.toml` is the single config file; all relative paths resolve
  against `.bos/`.
- `[platform]` controls env loading and extension discovery; `[agent.defaults]`
  and `[agents.<name>]` define agents; `[runtime]` owns actors and the gateway.
- `boscli gateway start` boots the runtime; `boscli tui` connects the terminal UI.
- Secrets live in `.bos/.env` and are loaded at startup.

**Next:** [Add a custom tool](custom-tool.md) — drop a Python file into
`extensions/` and give your agent a new capability.
