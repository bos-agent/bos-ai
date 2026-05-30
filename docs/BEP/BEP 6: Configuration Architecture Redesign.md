# BEP 6: Configuration Architecture Redesign

Status: **design accepted** — schema and template settled, partial implementation in progress.

---

## Core Insight

The original configuration layout grew organically: agent specs lived under `[platform]`, extension configs were scattered across `[harness]` and `[platform]`, runtime settings lived in `[main]`, and the naming was inconsistent (`plugins` plural, `consolidator` singular). The config was a raw `dict[str, Any]` with no load-time validation.

BEP 6 restructures the TOML configuration around clean top-level sections, each with a single responsibility. Extension-point configurations use the exact `ep_<name>` key, so newly registered extension points automatically accept configuration without schema changes. Pydantic models provide eager load-time validation.

The target layout:

```text
[platform]          discovery, env, extension loading
[ext]               EP implementation selection
[exts.ep_*.<impl>]  per-EP, per-implementation configuration
[agent.defaults]     global agent defaults
[agents.<name>]     named agent overrides (TOML tables, not a list)
[runtime]           agent selection, channels, named actors
```

---

## Goals

1. One top-level section per concern — platform discovery, extension config, agent config, runtime config.
2. Extension configs use the exact `ep_<name>` key so new EPs auto-discover without schema changes.
3. Eager Pydantic validation at config load time with clear error messages.
4. `[agents.<name>]` as TOML tables keyed by name — no `name` field inside the spec, no list dedup.
5. Agent `tools` and `plugins` use structured `{enabled, disabled, usages}` / `{enabled, disabled, prompts}` sub-models instead of magic `"*"` values.
6. `[ext]` selects which implementation to use per EP; `[exts]` provides the per-implementation config dicts.
7. External agent files (`.toml`, `.md`) merge into `[agents.<name>]` with last-wins semantics.

## Non-Goals

1. This BEP does not fully migrate `tools_config` injection — tools will eventually receive their config via EP defaults instead of through `Agent`.
2. This BEP does not redesign subagent configuration — that moves to `SubagentPlugin` plugin-bindings in a future BEP.
3. This BEP does not preserve backward compatibility with the old `[harness]` or `[[platform.agents]]` format. There is no production usage to migrate.

---

## Top-Level Sections

### `[platform]` — discovery and loading

Shrunk to the minimum needed to bootstrap the platform: env loading, extension module discovery, and external agent file scanning.

```toml
[platform]
envfile = ".env"
extensions = ["bos.exts", "./extensions"]

[platform.envs]
BOS_CAPABILITY_LIMIT = "50"
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `envfile` | `str \| None` | `None` | Path to `.env` file, resolved relative to `bos_dir` |
| `envs` | `dict[str, str]` | `{}` | Inline environment variables |
| `extensions` | `list[str]` | `["bos.exts", "./extensions"]` | Module names or paths for extension discovery |
| `agent_dirs` | `list[str]` | `["./agents"]` | Directories scanned for external agent files |

### `[ext]` — EP implementation selection

Selects which registered implementation to use for each extension point. `[exts]` provides the per-implementation configuration.

```toml
[ext]
consolidator = "_default"
chat_store = "_default"
interceptors = ["logging", "rate_limit"]
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `consolidator` | `str` | `"_default"` | `ep_consolidator` implementation name |
| `chat_store` | `str` | `"_default"` | `ep_message_store` implementation name |
| `interceptors` | `list[str]` | `[]` | Ordered chain of `ep_turn_interceptor` names |

`extra="allow"` — newly registered EPs accept a selection key without schema changes.

### `[exts.ep_*.<impl>]` — per-EP configuration

Configuration dicts keyed by EP name and implementation name. Each `ep_<name>` is discovered automatically via `extra="allow"` on the `ExtensionsConfig` model.

```toml
[exts.ep_provider.Codex]
model = "gpt-5o"

[exts.ep_consolidator._default]
model = "gemini/gemini-2.5-flash"

[exts.ep_message_store._default]
store_dir = "./messages"

[exts.ep_mail_route._default]
store_dir = "./mailboxes"
```

The harness resolves `ext.consolidator` (e.g. `"_default"`) against `exts.ep_consolidator._default` to get the final config dict, then merges it into the EP's registered defaults.

### `[agent.defaults]` — global agent defaults

Agent-level settings that apply to all agents unless overridden. The structured `tools` and `plugins` sub-models replace the old flat `tools = "*"` / `plugins = {Name: {enabled: true}}` pattern.

```toml
[agent.defaults]
system_prompt = "You are the BOS agent."
model = "gpt-4o"
agent_name = "bos"
reasoning_effort = "medium"
max_tokens = 131072
max_iterations = 60
tool_noise_filter = "keep_signatures"

[agent.defaults.tools]
enabled = ["*"]
disabled = ["WriteFile", "AskSubagent"]
usages = {}

[agent.defaults.plugins]
enabled = ["MemoryPlugin", "PlanPlugin", "SubagentPlugin"]
disabled = ["SkillsPlugin"]
prompts = {}

[agent.defaults.plugin-bindings.SubagentPlugin]
task_template = """
--- Sub-agent Instructions ---
You are a background sub-agent `{role}`. Work only on the delegated task,
use only the tools available to you, do not ask the user questions, and finish with
a concise result. You can only modify or create files under {workspace}.
--- Task Description ---
{task}
"""
```

#### Tools sub-model (`ToolsConfig`)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `enabled` | `list[str]` | `[]` | Tool allow-list. `["*"]` means all registered tools. |
| `disabled` | `list[str]` | `[]` | Tools subtracted from the allow-list. |
| `usages` | `dict[str, str]` | `{}` | Per-tool usage string overrides. |

The old `tools = "*"` magic literal is replaced by `enabled = ["*"]` — always a list, no special-case parsing.

#### Plugins sub-model (`PluginsConfig`)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `enabled` | `list[str]` | `[]` | Plugin allow-list, in activation order. |
| `disabled` | `list[str]` | `[]` | Plugins subtracted from the allow-list. |
| `prompts` | `dict[str, str]` | `{}` | Per-plugin prompt section overrides. |

#### Plugin bindings

`plugin-bindings.<Name>` (TOML alias `plugin-bindings`) holds the per-plugin configuration dict that the harness passes to `HarnessPlugin.bind()`. It is separate from `plugins.enabled`/`disabled` — the plugins sub-model answers "which plugins", plugin-bindings answers "how each plugin is configured".

### `[agents.<name>]` — named agent overrides

Named agents as TOML tables keyed by name. The TOML key is the agent name — there is no `name` field inside the spec. This is a structural change from the old `[[platform.agents]]` list-of-tables format.

```toml
[agents.main]
system_prompt = "You are the main BOS agent."

[agents.main.plugins]
enabled = ["PlanPlugin", "SkillsPlugin"]

[agents.main.plugin-bindings.SkillsPlugin]
skill_dirs = ["/path/to/skills"]

[agents.researcher]
system_prompt = "You research codebases."
tools = { enabled = ["ReadFile", "GrepSearch", "WebSearch"] }
```

Each named agent inherits from `[agent.defaults]` via a deep merge in the harness (defaults → per-agent overrides). External `.toml` and `.md` files from `agent_dirs` merge into the same table with last-wins semantics per key.

### `[runtime]` — runtime selection, channels, actors

Replaces the old `[main]` section.

```toml
[runtime]
agent = "main"

[runtime.actors.main]
agent = "main"
display_name = "Main"

[runtime.actors.bob]
agent = "researcher"
display_name = "Bob"

[[runtime.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
host = "127.0.0.1"
port = 5920
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `agent` | `str` | `"_default"` | The main agent kind for the runtime. |
| `channels` | `list[dict]` | `[]` | Channel configurations (array of tables). |
| `actors` | `dict[str, ActorConfig]` | `{}` | Named actor definitions keyed by actor identity. |

#### Actor config (`ActorConfig`)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `agent` | `str` | *(required)* | Which registered agent kind to instantiate. |
| `display_name` | `str \| None` | `None` | Human-facing label for UI rendering. |

`extra="allow"` — extra keys pass through as agent-level overrides (e.g. `tools`, `system_prompt`).

---

## Pydantic Schema

### Model hierarchy

```text
RootConfig
├── platform: PlatformConfig          [extra="forbid"]
├── ext: ExtConfig                    [extra="forbid"]
├── exts: ExtensionsConfig           [extra="allow"]
├── agent: AgentSection              [extra="forbid"]
│   └── defaults: AgentConfig        [extra="allow"]
├── agents: dict[str, AgentConfig]   [extra="allow" per entry]
└── runtime: RuntimeConfig           [extra="allow"]
    ├── agent: str
    ├── channels: list[dict]
    └── actors: dict[str, ActorConfig]  [extra="allow" per entry]
```

### Validation strategy

- **`extra="forbid"`** on `PlatformConfig`, `ExtConfig`, `AgentSection`, `ToolsConfig`, `PluginsConfig` — catches key typos in well-known sections.
- **`extra="allow"`** on `ExtensionsConfig`, `AgentConfig`, `ActorConfig`, `RuntimeConfig`, `RootConfig` — these sections are inherently extensible (new EPs, plugin keys, actor overrides).
- **`extra="allow"`** on `RootConfig` — preserves unknown top-level keys for forward compatibility.
- Validation is **eager**: `validate_config()` is called immediately after TOML parsing in `from_discovery()` and `resolve_config_source()`.

### KeyedConfigs

`KeyedConfigs` is a reusable `RootModel[dict[str, dict[str, Any]]]` for the `{name: {config_key: value}}` pattern used by extension configs and plugin bindings.

---

## Agent Spec Resolution

### Inline agents

`[agents.<name>]` tables are validated as `AgentConfig` by Pydantic during `RootConfig` validation. The TOML key is the agent name.

### External agent files

Files under `agent_dirs` (`.toml` or `.md`) are scanned alphabetically within each directory. For `.toml` files, the content is parsed as TOML and validated against `AgentConfig`. The name is derived from:

1. An explicit `name` field in the file (if present).
2. The filename stem (`researcher.toml` → `"researcher"`).

The validated spec is deep-merged into the `agents` dict with last-wins semantics per key.

For `.md` files, YAML-like frontmatter (delimited by `---`) provides the agent spec fields; the body becomes `system_prompt`.

### Merge order

1. Inline `[agents.<name>]` tables from the config file.
2. External files from `agent_dirs`, sorted alphabetically within each directory.

Later entries deep-merge on top of earlier entries for the same agent name.

---

## Migration from old config

| Old key | New key |
|---|---|
| `[main]` | `[runtime]` |
| `main.agent` | `runtime.agent` |
| `[[main.channels]]` | `[[runtime.channels]]` |
| `[main.actors.<name>]` | `[runtime.actors.<name>]` |
| `[main.runtime]` | *(deferred — Docker settings not yet placed)* |
| `[platform.agent_defaults]` | `[agent.defaults]` |
| `[[platform.agents]]` (list) | `[agents.<name>]` (table) |
| `[platform.enabled_plugins]` | `[agent.defaults.plugins].enabled` |
| `[platform.plugins.<Name>]` | `[agent.defaults.plugin-bindings.<Name>]` |
| `[harness.consolidator]` | `[exts.ep_consolidator._default]` |
| `[harness.message_store]` | `[exts.ep_message_store._default]` |
| `[harness.tools.<Name>]` | `[exts.ep_tool.<Name>]` |
| `[harness.mail_route]` | `[exts.ep_mail_route._default]` |
| `[harness.skills_loader]` | `[exts.ep_skills_loader._default]` |
| `[harness.interceptors]` | `[ext].interceptors` (selection) + `[exts.ep_turn_interceptor.<name>]` (config) |
| `[harness.subagent_defaults]` | `[agent.defaults.plugin-bindings.SubagentPlugin]` |
| `tools = "*"` | `tools.enabled = ["*"]` |
| `tools = ["ReadFile"]` | `tools.enabled = ["ReadFile"]` |
| `plugins = {Name: {enabled: true}}` | `plugins.enabled = ["Name"]` + `plugin-bindings.Name = {...}` |


## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-30 | Initial draft | Improve the configuration architecture clarity and apply reliable validations |
