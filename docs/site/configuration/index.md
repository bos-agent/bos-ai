# Configuration

BOS is configured by **one TOML file** — `.bos/config.toml` — plus optional Python
extensions and Markdown/TOML agent files in conventional directories. This page is the
complete reference for that file: how it is discovered, every section, and the precedence
rules that decide a final value.

!!! tip "Generate a commented starting point"
    `boscli init` writes a runnable baseline. To see the **fully-commented reference**
    config, run `boscli init --minimal` in an empty directory — it emits the canonical
    template (`src/bos/config/template.toml`) with every option documented inline.

## The workspace and `bos_dir`

A **workspace** is any directory tree containing `.bos/config.toml`. The directory holding
`config.toml` (i.e. `.bos/`) is the **`bos_dir`**, and every relative path in the config —
`envfile`, `extensions`, `agent_dirs`, store directories — is resolved against it.

```
my-agent/
├── .bos/
│   ├── config.toml     # the one config file; .bos/ is the bos_dir
│   ├── .env            # secrets (if envfile = ".env")
│   ├── agents/         # external agent definitions (*.md / *.toml)
│   ├── extensions/     # project-local Python extensions
│   ├── skills/         # project-local skills (dirs containing SKILL.md)
│   ├── messages/       # JsonlChatStore persistence (default)
│   └── gateway.state   # runtime port/PID discovery file
└── (your project files)
```

`BOS_HOME` (default `~/.bos`) holds global state used when you run a built-in **preset**
rather than a project (e.g. `~/.bos/presets/<name>`).

## How the config is discovered

When you run a `boscli` command, it resolves the active config like this:

1. **Walk up** from the current directory through its ancestors; the first
   `.bos/config.toml` found wins. (A `.bos/` directory without `config.toml` is skipped and
   the walk continues.)
2. **`BOS_CONFIG`** environment variable — an absolute path to a config file. If a discovered
   file *and* `BOS_CONFIG` both exist and point at different files, BOS raises an
   *ambiguous config* error rather than guessing.
3. Otherwise: *"No BOS workspace found. Run `boscli init`, `cd` into a workspace, or set
   `BOS_CONFIG`."*

You can also point any command at a specific source with `-c/--config`:

```bash
boscli -c ./path/to/config.toml gateway start   # an explicit file
boscli -c team tui                               # a built-in preset name
```

A `-c` value that is an existing file path uses that file (its parent becomes `bos_dir`).
Otherwise it is treated as a built-in **preset** name, resolving to
`src/bos/config/presets/<name>.toml` with `bos_dir = ~/.bos/presets/<name>`. Built-in
presets: `default`, `team`.

## Sections at a glance

| Section | Purpose |
| --- | --- |
| `[platform]` | Environment loading and where to discover extensions & agents. |
| `[harness]` | Selects which implementation backs each shared service. |
| `[exts.<ep>.<impl>]` | Configures any registered extension (tools, providers, stores…). |
| `[agent.defaults]` | Defaults merged into every agent. |
| `[agents.<name>]` | A named agent definition. |
| `[runtime]` | The runtime: actors, gateway, channels. |

---

## `[platform]` — environment & discovery

```toml
[platform]
envfile = ".env"                          # dotenv file, resolved against bos_dir
extensions = ["bos.exts", "./extensions"] # modules to import + directories to scan
agent_dirs = ["./agents"]                 # directories scanned for *.md / *.toml agents

[platform.envs]                           # inline env vars (applied BEFORE the envfile)
BOS_MODEL = "openai/gpt-4o"
BOS_CAPABILITY_LIMIT = "50"
```

| Key | Default | Notes |
| --- | --- | --- |
| `envfile` | _(none)_ | Path to a dotenv file, loaded with `override=True`. |
| `envs` | `{}` | Inline env vars applied to `os.environ` **before** `envfile`. |
| `extensions` | `["bos.exts", "./extensions"]` | See resolution below. |
| `agent_dirs` | `["./agents"]` | Directories scanned for external agent files. |

**How `extensions` entries resolve:** each entry is checked as a path relative to `bos_dir`.
If it exists, it is loaded as a **directory/file path** (Python files are scanned and
imported, running their decorators); otherwise it is imported as a **module name**.
`"bos.exts"` is the module that imports every built-in extension and discovers the
`bos.exts` entry-point group (installed packages). See
[Packaging & entry points](../extending/entry-points.md).

**Environment order:** `[platform.envs]` first, then `envfile`. Anything in the envfile wins
because it is loaded with override.

---

## `[harness]` — select service implementations

The harness owns shared, process-wide services. Each key names a **registered extension by
name**. Unknown keys are rejected.

```toml
[harness]
consolidator = "LLMConsolidator"   # ep_consolidator
chat_store   = "JsonlChatStore"    # ep_chat_store
mail_route   = "JsonlMailRoute"    # ep_mail_route
job_runner   = "InProcJobRunner"   # ep_job_runner
interceptors = []                  # ordered list of ep_turn_interceptor names/configs
```

The values above are the defaults. Built-in adapters are always registered, so these names
resolve even in a minimal config. To configure a chosen implementation, use its
`[exts.<ep>.<impl>]` table (below). There is intentionally **no provider key here** —
provider selection happens per model string (see
[Providers, stores & services](../extending/stores-and-providers.md)).

`interceptors` is an ordered chain; each entry is either a registered interceptor name or a
`{ name = "...", ... }` table whose extra keys become that interceptor's config.

---

## `[exts.<ep>.<impl>]` — configure any extension

This is the universal knob for passing configuration into a registered extension.
`<ep>` is an extension-point name (`ep_*` for core points, `pep_*` for plugin-defined
points); `<impl>` is the registered implementation. The table's keys are merged into that
extension's defaults and passed to it as keyword arguments when it runs.

```toml
[exts.ep_consolidator.LLMConsolidator]
model = "gemini/gemini-2.5-flash"

[exts.ep_chat_store.JsonlChatStore]
store_dir = "./messages"

[exts.ep_provider.litellm]
# provider-wide defaults

[exts.ep_tool.GrepSearch]
extend_ignore = ["*.min.js"]

[exts.pep_skills_loader.FileSystemSkillsLoader]   # a plugin-defined extension point
skill_dirs = ["skills"]
```

!!! note
    `[exts]` accepts configuration for **any** registered extension point — even ones added
    by third-party packages — with no schema change. An `[exts.<ep>]` whose name matches no
    registered extension point is logged and ignored, so a typo fails quietly rather than
    crashing. See [Extending BOS](../extending/index.md) for the list of extension points.

---

## `[agent.defaults]` and `[agents.<name>]` — agents

Both use the same `AgentConfig` schema. `[agent.defaults]` is merged into every agent;
`[agents.<name>]` defines a specific agent.

```toml
[agent.defaults]
# model = "openai/gpt-4o"
max_tokens = 131072
max_iterations = 80
# reasoning_effort = "medium"        # low | medium | high
# tool_noise_filter = "strip_all"    # strip_all | keep_all

[agent.defaults.tools]
enabled = ["*"]                      # "*" = all registered tools
# disabled = ["WriteFile"]
# usages = { ReadFile = "custom guidance shown to the model" }

[agent.defaults.plugins]
enabled = ["*"]                      # "*" = all registered plugins (minus disabled)

[agents.researcher]
description = "Researches codebases."
system_prompt = "You research codebases and report findings."
model = "openai/gpt-4o"
[agents.researcher.tools]
enabled = ["ReadFile", "GrepSearch", "WebSearch"]
[agents.researcher.plugin-bindings.SubagentPlugin]
enabled = ["writer"]
```

| Field | Type / default | Notes |
| --- | --- | --- |
| `system_prompt` | `str` | Base prompt; plugins append sections at runtime. |
| `model` | `str` | LiteLLM-style `provider/model`. |
| `agent_name` | `str` | Identity used for memory scoping. |
| `reasoning_effort` | `low \| medium \| high` | Passed to the model if supported. |
| `max_tokens` | `int` = `131072` | Context budget before compaction. |
| `max_iterations` | `int` = `80` | Max tool-call iterations per turn. |
| `tool_noise_filter` | `strip_all \| keep_all` | How prior tool output is retained. |
| `history_attribution` | `bool` = `false` | Tag history with the speaking actor. |
| `tools` | `{ enabled, disabled, usages }` | `enabled=["*"]` = all; `usages` overrides per-tool guidance. |
| `plugins` | `{ enabled, disabled }` | `enabled=["*"]` = all registered plugins. |
| `plugin-bindings` | `{ <Plugin>: {…} }` | Per-plugin config (key is hyphenated in TOML). |
| `_parent` | `str` | Inherit from another `[agents.*]` agent — see [Agent inheritance](#agent-inheritance-_parent). |

!!! info "Note the hyphen"
    Per-plugin configuration uses `plugin-bindings` (with a hyphen) in TOML, e.g.
    `[agents.main.plugin-bindings.SubagentPlugin]`.

### Tool resolution

An agent sees a merged, filtered view over **[agent-local plugin tools, the global tool
registry]** (local tools win on a name clash). `enabled` is an include list (`"*"` or
omitted = all); `disabled` is an exclude list applied on top.

### Model precedence

A model value is resolved in this order (highest first):

```
[agents.<name>].model  >  [agent.defaults].model  >  BOS_MODEL  >  [exts.ep_provider.<provider>].model
```

### Agent inheritance (`_parent`)

An `[agents.<name>]` table can set `_parent = "<other agent>"` to inherit that agent's
resolved spec. The parent is deep-merged **underneath** the child — the child's own keys
win, using the same merge rules as everywhere else (dicts merge, lists/scalars replace).
Use it to share a base across a family of related agents instead of repeating it.

```toml
[agents.leader]
system_prompt = "You coordinate a team."
model = "anthropic/claude-opus-4-8"
[agents.leader.tools]
enabled = ["ReadFile", "AskSubagent"]

[agents.niceleader]
_parent = "leader"                                # inherits model, tools, plugins, …
system_prompt = "You coordinate a team, warmly."  # overrides just this field
```

- **Multi-level**: chains resolve transitively (`c` → `b` → `a`).
- **Floor still applies**: `[agent.defaults]` is merged in under the `_parent` chain for every agent.
- **Scope**: `_parent` may name only another `[agents.*]` agent (inline or an external file) — not an
  `@ep_agent` factory agent or `[agent.defaults]`.
- **Errors**: a cycle (`a` → `b` → `a`) or an unknown parent fails fast at startup with a clear message.

!!! warning "Lists replace, they don't union"
    If the child sets `tools.enabled = ["ReadFile"]`, it **replaces** the parent's list entirely.
    To extend a parent's list, restate the full list in the child.

### External agent files (`agent_dirs`)

Every `*.toml` / `*.md` file in an `agent_dirs` directory defines one agent. The **filename
stem is the agent name** unless the file sets `name` explicitly. An external file
**replaces** an inline `[agents.<name>]` of the same name entirely (no merge). Files load
alphabetically within a directory, directories in list order; later wins.

=== "Markdown agent (`agents/writer.md`)"

    ```markdown
    ---
    description: Writes documentation.
    model: openai/gpt-4o
    tools:
      enabled: [ReadFile, WriteFile]
    ---
    You are a meticulous technical writer. Produce clear, accurate docs.
    ```

    Frontmatter fields map to config keys; the body becomes the `system_prompt`. Putting
    `system_prompt` in the frontmatter is rejected (the body is the prompt). If the
    frontmatter is invalid, the whole file is used as the `system_prompt`. The parser
    supports a small YAML subset — flat scalars and one-level keys with **inline** lists
    (`enabled: [ReadFile, WriteFile]`); for deeper structure, use a `.toml` agent file.

=== "TOML agent (`agents/researcher.toml`)"

    ```toml
    description = "Researches codebases."
    model = "openai/gpt-4o"
    system_prompt = "You research codebases and report findings."
    [tools]
    enabled = ["ReadFile", "GrepSearch", "WebSearch"]
    ```

### Agent resolution chain

For each agent name, the final spec is a deep merge in this order:

```
[agent.defaults]  →  ( _parent chain )  →  @ep_agent factory result (if any)  →  [agents.<name>] / external file
```

The built-in `BOS` agent is a normal builtin `@ep_agent` extension (a general assistant with
memory, planning, tasks, skills, and sub-agent delegation), registered when `bos.exts` is on
your `[platform.extensions]` (the default). It resolves through this same chain — `[agents.BOS]`
composes over it, or inherit from it with `_parent = "BOS"`. There is no hidden default-agent
fallback: drop `bos.exts` and you must define your own agent.

---

## `[runtime]` — actors, gateway, channels

```toml
[runtime]
main_actor = "main"            # the default route/mention target

[runtime.gateway]
host = "127.0.0.1"
port = 0                       # 0 = auto-assign a free port; discover via gateway.state
api_key_env = "BOS_GATEWAY_API_KEY"
# upload_dir = ".bos/uploads/http"
# max_upload_bytes = 20971520

[runtime.actor_resolver]
mention_prefix = "@"           # how channels resolve @actor mentions

[runtime.actors.main]
agent = "main"                 # which registered agent kind this actor runs
display_name = "Main"
# restart_on_error = true
# max_restarts = 5
[runtime.actors.main.agent_cfg]               # per-actor overrides (same shape as [agent.defaults])
# model = "openai/gpt-4o"
[runtime.actors.main.agent_cfg.plugin-bindings.SubagentPlugin]
enabled = ["researcher"]

[[runtime.channels]]           # array of tables: zero or more persistent channels
type = "TelegramChannel"       # a registered ep_channel name
channel_id = "telegram+main"   # unique id
display_name = "Telegram"
target_actor = "main"          # must exist in [runtime.actors]; defaults to main_actor
settings = { token_env = "TELEGRAM_BOT_TOKEN" }
```

**Actors** are the long-lived, addressable runtime instances. The **TOML key is the actor's
identity and memory scope** and must match `[A-Za-z_][A-Za-z0-9_-]*` (mention-safe).
`agent` selects which registered agent kind the actor runs; `agent_cfg` holds per-actor
overrides in the same shape as `[agent.defaults]`.

**`[runtime.gateway]`** configures the gateway's HTTP control plane. `port = 0` lets the OS
assign a free port; the actual port and PID are written to `gateway.state` so clients (the
TUI, `boscli gateway status`) can find the running gateway. The control plane is
authenticated with the key in the environment variable named by `api_key_env`.

**`[[runtime.channels]]`** is an array of tables — declare one per persistent channel. Rules:

- `channel_id` must be unique.
- `target_actor` must be a defined actor (defaults to `main_actor`).
- `type` must be a registered channel (`ep_channel`); `HttpChannel` is reserved gateway
  infrastructure and cannot be configured here.
- `settings` is a free-form table handed to the channel; secrets are conventionally
  referenced by env-var name (`token_env`, `app_id_env`, …) rather than inlined.

See the [Connect a channel](../tutorials/channels.md) tutorial and
[Writing channels](../extending/channels.md).

!!! warning "Removed config keys"
    `[main]` is no longer supported — use `[runtime]` with `[runtime.actors]`. Within
    `[runtime]`, `agent` and `default_actor` were removed; use `actors` and `main_actor`.

---

## Where to go next

- **[Extending BOS](../extending/index.md)** — write tools, plugins, channels, providers,
  and stores, and configure them through `[exts.<ep>.<impl>]`.
- **[Concepts](../concepts/index.md)** — how the runtime, agents, actors, memory, and skills
  fit together.
- **Full reference** — [`llm-full.md`](https://github.com/bos-agent/bos-ai/blob/main/src/bos/llm-full.md)
  is a single dense reference covering every mechanism, intended for ingestion by an AI agent.
  `boscli init` drops a copy into every project.
