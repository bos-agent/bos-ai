# BEP 6: Configuration Architecture Redesign

Status: **design accepted** — schema, template, and system design settled. Ready for implementation planning.

---

## Core Insight

The original configuration layout grew organically: agent specs lived under `[platform]`, extension configs were scattered across `[harness]` and `[platform]`, runtime settings lived in `[main]`, and the naming was inconsistent (`plugins` plural, `consolidator` singular). The config was a raw `dict[str, Any]` with no load-time validation.

BEP 6 restructures the TOML configuration around clean top-level sections, each with a single responsibility. Extension-point configurations use the exact `ep_<name>` key, so newly registered extension points automatically accept configuration without schema changes. Pydantic models provide eager load-time validation.

The target layout:

```text
[platform]          discovery, env, extension loading
[harness]           EP implementation selection (harness-aware EPs only)
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
6. `[harness]` selects which implementation to use per EP; `[exts]` provides the per-implementation config dicts.
7. External agent files (`.toml`, `.md`) merge into `[agents.<name>]` with last-wins semantics.

## Non-Goals

1. This BEP does not fully migrate `tools_config` injection — tools will eventually receive their config via EP defaults instead of through `Agent`.
2. This BEP does not redesign subagent configuration — `subagent_defaults` and `subagents` are removed from the harness; per-role subagent config moves to `SubagentPlugin` plugin-bindings in a future BEP.
3. This BEP does not preserve backward compatibility with the old `[harness]` or `[[platform.agents]]` format. There is no production usage to migrate.

---

## Design Principles

### Selection vs Configuration

Extension points are configured in two layers:

- **`[harness]`** selects which implementation to use for harness-aware EPs. The harness must know these EPs to initialize its services. This section uses `extra="forbid"` — new EPs require an explicit schema field.
- **`[exts.ep_*.<impl>]`** provides per-implementation configuration for all EPs. This section uses `extra="allow"` — newly registered EPs automatically accept configuration without schema changes. The harness loops through `[exts]` entries and merges each into the corresponding EP's registered defaults.

### Agent Config Merge Semantics

Agent configuration is resolved via **deep merge** with the following rules:

- **Dict values**: recursively merged (e.g., `plugin-bindings.SubagentPlugin` keys merge).
- **List values** (e.g., `tools.enabled`, `plugins.disabled`): **replaced** entirely by the per-agent list. Per-agent lists do not union with defaults.
- **Scalar values**: replaced when the per-agent value is not `None`.

The merge chain for a named agent is:

```
[agent.defaults]  →  [agents.<name>]
```

For the `_default` agent specifically, the Python `default_agent_spec` merges on top of `[agent.defaults]`. If `[agents._default]` is explicitly defined in TOML, it replaces the Python spec entirely (still merging on top of `[agent.defaults]`).

---

## Top-Level Sections

### `[platform]` — discovery and loading

Shrunk to the minimum needed to bootstrap the platform: env loading, extension module discovery, and external agent file scanning.

```toml
[platform]
envfile = ".env"
extensions = ["bos.exts", "./extensions"]
agent_dirs = ["./agents"]

[platform.envs]
BOS_CAPABILITY_LIMIT = "50"
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `envfile` | `str \| None` | `None` | Path to `.env` file, resolved relative to `bos_dir` |
| `envs` | `dict[str, str]` | `{}` | Inline environment variables |
| `extensions` | `list[str]` | `["bos.exts", "./extensions"]` | Module names or paths for extension discovery |
| `agent_dirs` | `list[str]` | `["./agents"]` | Directories scanned for external agent files. Entries resolved relative to `bos_dir` at config-load time. |

**Environment variable precedence**: inline `[platform.envs]` are loaded first, then `envfile` is loaded with `override=True`. The envfile wins on conflict.

**`bootstrap_platform()`** accepts the validated `PlatformConfig` (the `[platform]` section) and drives extension loading, env injection, and agent registration.

### `[harness]` — EP implementation selection (harness-aware)

Selects which registered implementation to use for each harness-aware extension point. Uses `extra="forbid"` — every key here must be known to the harness.

```toml
[harness]
consolidator = "_default"
chat_store = "_default"
mail_route = "_default"
interceptors = ["logging", "rate_limit"]
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `consolidator` | `str` | `"_default"` | `ep_consolidator` implementation name |
| `chat_store` | `str` | `"_default"` | `ep_message_store` implementation name |
| `mail_route` | `str` | `"_default"` | `ep_mail_route` implementation name |
| `interceptors` | `list[str]` | `[]` | Ordered chain of `ep_turn_interceptor` names |

### `[exts.ep_*.<impl>]` — per-EP configuration

Configuration dicts keyed by EP name and implementation name. Each `ep_<name>` is discovered automatically via `extra="allow"` on the `ExtensionsConfig` model. The harness loops through all `[exts]` entries and merges each into the corresponding EP's registered Python defaults. This applies to `ep_provider` as well — the harness no longer passes a `providers_cfg` to `LLMClient`.

```toml
[exts.ep_provider.Codex]
model = "gpt-5o"

[exts.ep_consolidator._default]
model = "gemini/gemini-2.5-flash"

[exts.ep_message_store._default]
store_dir = "./messages"

[exts.ep_mail_route._default]
store_dir = "./mailboxes"

[exts.ep_skills_loader._default]
skill_dirs = ["/path/to/skills"]
```

The harness resolves `harness.consolidator` (e.g. `"_default"`) against `exts.ep_consolidator._default` to get the final config dict, then merges it into the EP's registered defaults.

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
| `enabled` | `list[str]` | `[]` | Tool allow-list. `["*"]` means all registered tools. Empty/not set = deny-by-default. |
| `disabled` | `list[str]` | `[]` | Tools subtracted from the allow-list. |
| `usages` | `dict[str, str]` | `{}` | Per-tool usage string overrides, passed to `Agent` as `tools_usage`. |

**Mapping to `Agent` constructor:**

| Config value | Agent ctor parameter |
|---|---|
| `tools.enabled` empty / not set | `tools=[]` (deny-by-default) |
| `tools.enabled = ["*"]` | `tools=None` (all tools enabled) |
| `tools.disabled` | `exclude_tools` |
| `tools.usages` | `tools_usage` |

The old `tools = "*"` magic literal is replaced by `enabled = ["*"]` — always a list, no special-case parsing. The conservative default is empty (deny all) — agents must explicitly opt into tools.

#### Plugins sub-model (`PluginsConfig`)

| Key | Type | Default | Purpose |
|---|---|---|---|
| `enabled` | `list[str]` | `[]` | Plugin allow-list, in activation order. A plugin is enabled if it appears in `enabled` OR `enabled` contains `"*"`. |
| `disabled` | `list[str]` | `[]` | Plugins subtracted from the allow-list. A plugin in `disabled` is always excluded, even if it matches `enabled`. |
| `prompts` | `dict[str, str]` | `{}` | Per-plugin prompt section overrides, passed to `Agent` as `plugins_prompt`. |

Both `enabled` and `disabled` lists **replace** (not union) their defaults counterpart during the deep merge. A plugin is active for an agent if and only if it passes both filters: present in `enabled` (or `enabled` contains `"*"`) AND not present in `disabled`.

#### Plugin bindings

`plugin-bindings.<Name>` (TOML alias `plugin-bindings`) holds the per-plugin configuration dict that the harness passes to `HarnessPlugin.bind()`. It is separate from `plugins.enabled`/`disabled` — the plugins sub-model answers "which plugins", plugin-bindings answers "how each plugin is configured".

At agent creation time, `plugin-bindings.<PluginName>` is read from the already-merged agent config (defaults + per-agent), so each agent instance gets the merged binding config without runtime merging.

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

Each named agent inherits from `[agent.defaults]` via a deep merge. External `.toml` and `.md` files from `agent_dirs` merge into the same table with last-wins semantics per key.

#### The `_default` agent

The `_default` agent is defined in Python via `default_agent_spec` (in `bos/core/defaults/agent_spec.py`). Its resolution is:

1. Start with `[agent.defaults]` as the base.
2. Merge `default_agent_spec` on top (provides the hardcoded system prompt, default tools/plugins, etc.).
3. If `[agents._default]` is explicitly defined in TOML, it **replaces** step 2 entirely (the TOML spec is used instead of the Python spec, still merging on top of `[agent.defaults]`).

Other named agents (`[agents.<name>]`) merge on top of `[agent.defaults]` only — no Python fallback.

### `[runtime]` — runtime selection, channels, actors

Replaces the old `[main]` section.

```toml
[runtime]
agent = "main"
location = "process"

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
| `location` | `str` | `"process"` | Where the runtime executes (`"process"` or `"docker"`). Was `main.runtime.kind` in the old format. |
| `channels` | `list[dict]` | `[]` | Channel configurations (array of tables). |
| `actors` | `dict[str, ActorConfig]` | `{}` | Named actor definitions keyed by actor identity. |

`extra="allow"` — Docker settings (`image`, `workspace_dir`, etc.) pass through for now without dedicated schema.

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
├── harness: HarnessConfig            [extra="forbid"]
├── exts: ExtensionsConfig           [extra="allow"]
├── agent: AgentSection              [extra="forbid"]
│   └── defaults: AgentConfig        [extra="allow"]
├── agents: dict[str, AgentConfig]   [extra="allow" per entry]
└── runtime: RuntimeConfig           [extra="allow"]
    ├── agent: str
    ├── location: str
    ├── channels: list[dict]
    └── actors: dict[str, ActorConfig]  [extra="allow" per entry]
```

### Validation strategy

- **`extra="forbid"`** on `PlatformConfig`, `HarnessConfig`, `AgentSection`, `ToolsConfig`, `PluginsConfig` — catches key typos in well-known sections.
- **`extra="allow"`** on `ExtensionsConfig`, `AgentConfig`, `ActorConfig`, `RuntimeConfig`, `RootConfig` — these sections are inherently extensible (new EPs, plugin keys, actor overrides).
- Validation is **eager**: `validate_config()` is called immediately after TOML parsing in `from_discovery()` and `resolve_config_source()`.

### KeyedConfigs

`KeyedConfigs` is a reusable `RootModel[dict[str, dict[str, Any]]]` for the `{name: {config_key: value}}` pattern used by extension configs and plugin bindings.

---

## Agent Spec Resolution

### Inline agents

`[agents.<name>]` tables are validated as `AgentConfig` by Pydantic during `RootConfig` validation. The TOML key is the agent name.

### External agent files

Files under `agent_dirs` (`.toml` or `.md`) are scanned alphabetically within each directory. Agent directories are resolved relative to `bos_dir` at config-load time. Each external spec is validated via `validate_agent_config()`.

**`.toml` files**: parsed as TOML, validated against `AgentConfig`. The name is derived from:

1. An explicit `name` field in the file (if present).
2. The filename stem (`researcher.toml` → `"researcher"`).

**`.md` files**: YAML-like frontmatter (delimited by `---`) provides the agent spec fields; the body becomes `system_prompt`. The same name derivation rules apply. If frontmatter is invalid, the entire file content is used as `system_prompt` with a warning.

### Merge order

1. Inline `[agents.<name>]` tables from the config file.
2. External files from `agent_dirs`, sorted alphabetically within each directory.

Later entries deep-merge on top of earlier entries for the same agent name.

---

## Harness Changes

### Constructor signature

The `AgentHarness.__init__` signature is slimmed down to only what it directly needs to create services:

```python
class AgentHarness:
    def __init__(
        self,
        *,
        bos_dir: str | Path,
        workspace: str | Path,
        harness_config: dict[str, Any],  # [harness]
    ):
```

Removed parameters: `mail_route`, `chat_store`, `consolidator`, `providers`, `interceptors`, `tools`, `subagent_defaults`, `subagents`, `enabled_plugins`, `platform_plugins`, `agent_defaults`, `agents_config`, `exts_defaults`. All of these are handled upstream in `bootstrap_platform()` or derived from `harness_config`.

### Provider configuration

Providers are no longer passed to `LLMClient` as a `providers_cfg`. Instead, provider configuration lives in `[exts.ep_provider.<impl>]` and is merged into the EP's registered defaults by `bootstrap_platform()`. `LLMClient()` is instantiated with no provider config — it pulls config from EP defaults.

### Subagent configuration (removed)

`subagent_defaults` and `subagents` are removed from the harness entirely. The known gaps until `SubagentPlugin` migrates this:

- `task_template` message formatting is lost — subagent tasks pass through raw.
- Per-role subagent overrides are lost — subagents created via `create_agent(role)` rely on `AgentRegistry` defaults only.

These will be restored when `SubagentPlugin` reads `task_template` from its own `plugin-bindings` config and per-role overrides move to `[agents.<role>]` entries.

---

## System Design

### Architecture Overview

The configuration flows through three main components: `Workspace` (discovery and loading), `bootstrap_platform()` (extension and agent registration), and `AgentHarness` (service lifecycle and agent creation). The runner consumes `RuntimeConfig` directly.

```text
┌─────────────────────────────────────────────────────────┐
│                    Config Loading                        │
│                                                         │
│  bos.toml  ──┐                                          │
│  .bos/      ──┤  tomllib  →  validate_config()  →  RootConfig
│  preset     ──┤                                          │
│  -c file    ──┘                                          │
└──────────────────────┬──────────────────────────────────┘
                       │ RootConfig
                       ▼
┌──────────────────────────────────────────────────────────┐
│                     Workspace                             │
│                                                          │
│  • Stores RootConfig directly (replaces raw dict)         │
│  • resolve_agents() — loads external .toml/.md files     │
│    from agent_dirs, deep-merges into root_config.agents   │
│  • bootstrap_platform() — drives the bootstrap flow      │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│                 bootstrap_platform()                      │
│                 (moved to config.workspace)               │
│                                                          │
│  1. Load envs: [platform.envs] → [platform.envfile]     │
│  2. Load extensions: [platform.extensions]               │
│  3. Merge EP defaults: loop [exts.ep_*.<impl>],          │
│     call ep_<name>.update_defaults(<impl>, config)       │
│  4. Register agents:                                     │
│     for (name, cfg) in agents:                           │
│       AgentRegistry.register(name, deep_merge(            │
│         agent_defaults, cfg))                            │
│     if not AgentRegistry.has("_default"):                 │
│       AgentRegistry.register("_default", deep_merge(      │
│         agent_defaults, default_agent_spec))             │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│                    AgentHarness                           │
│                                                          │
│  __init__(bos_dir, workspace, harness_config)            │
│                                                          │
│  __aenter__:                                             │
│    1. Create services from harness_config:               │
│       - mail_route  ← ep_mail_route.invoke(name, cfg)    │
│       - chat_store  ← ep_message_store.invoke(name, cfg) │
│       - consolidator ← ep_consolidator.invoke(name, cfg) │
│       - llm         ← LLMClient()                        │
│       - interceptor ← ChainInterceptor(interceptor_names)│
│    2. Build PluginServices(llm, consolidator, ...)       │
│                                                          │
│  create_agent(kind):                                     │
│    1. Look up AgentRegistry.get_defaults(kind)           │
│    2. _bind_plugins_for_agent() — lazy setup + bind      │
│    3. Return Agent(kind, tools, plugins, ...)            │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│                      Runner                              │
│                                                          │
│  Receives RuntimeConfig directly:                        │
│  • runtime.agent   → main Agent kind                     │
│  • runtime.actors  → actor definitions                   │
│  • runtime.channels → channel configurations             │
│  • runtime.location → process / docker                   │
└──────────────────────────────────────────────────────────┘
```

### Config Loading Pipeline

All config sources flow through the same validation gate:

```text
bos.toml / .bos/config.toml  ─┐
presets/<name>.toml           ─┤  tomllib.loads()
-c /path/to/file.toml         ─┘       │
                                        ▼
                                 validate_config()
                                        │
                                        ▼
                                    RootConfig
```

`validate_config()` is called immediately after TOML parsing in `from_discovery()` and `resolve_config_source()`. The validated `RootConfig` is stored directly on `Workspace` — the old `self.config: dict[str, Any]` is replaced.

### Workspace

- `Workspace.__init__` accepts a validated `RootConfig` (no longer a raw dict).
- `Workspace.resolve_agents()` is a separate explicit step that scans `platform.agent_dirs`, loads external `.toml`/`.md` files, validates each via `validate_agent_config()`, and deep-merges them into `root_config.agents`.
- Convenience accessors (`get_main_agent_kind()`, `resolve_channels()`, etc.) are replaced with direct `root_config.runtime.*` access.

### bootstrap_platform() — relocated to config.workspace

`bootstrap_platform()` moves from `core.harness` to `config.workspace`. It takes the validated `RootConfig` sections and drives the pre-harness bootstrap:

1. **Environment loading**: inline `[platform.envs]` loaded first; `[platform.envfile]` loaded with `override=True` (envfile wins).
2. **Extension loading**: modules and paths from `[platform.extensions]` loaded via `_load_ext_modules` / `_load_ext_paths`.
3. **EP defaults merge**: the bootstrap iterates through `[exts]` entries. Each `ep_<name>.<impl>` path maps to `ExtensionPoint.update_defaults(<impl>, config)`. This pre-loads implementation defaults so the harness can call `ep_<name>.invoke(<impl>)` without passing config at call site.
4. **Agent registration** (see Addendum: `ep_agent`):
   - Each `ep_agent` factory is invoked exactly once; its returned spec is validated as `AgentConfig`.
   - For each agent name in `ep_agent` factories ∪ `root_config.agents`: `AgentRegistry.register(name, deep_merge(agent_defaults, deep_merge(factory_spec, cfg)))` where either middle/last term may be absent.
   - If `_default` does not exist after registration: `AgentRegistry.register("_default", deep_merge(agent_defaults, default_agent_spec))`.

### AgentHarness — slimmed down

The harness constructor takes only what it directly needs:

```python
class AgentHarness:
    def __init__(
        self,
        *,
        bos_dir: str | Path,
        workspace: str | Path,
        harness_config: dict[str, Any],  # [harness]
    ):
```

Removed: `mail_route`, `chat_store`, `consolidator`, `providers`, `interceptors`, `tools`, `subagent_defaults`, `subagents`, `enabled_plugins`, `platform_plugins`, `agent_defaults`, `agents_config`, `exts_defaults`.

**`__aenter__`**: creates services from `harness_config`:
- `mail_route` ← `ep_mail_route.invoke(harness_config.mail_route)`
- `chat_store` ← `ep_message_store.invoke(harness_config.chat_store)`
- `consolidator` ← `ep_consolidator.invoke(harness_config.consolidator)`
- `llm` ← `LLMClient()` (no more `providers_cfg` needed)
- `interceptor` ← `ChainInterceptor(harness_config.interceptors)`
- `PluginServices` constructed with the above services

**`create_agent(kind)`**: looks up `AgentRegistry.get_defaults(kind)`, extracts `plugins.enabled`/`plugins.disabled` and `plugin-bindings`, then calls `_bind_plugins_for_agent()`. Lazy plugin setup: first time a plugin is needed, it's instantiated via `ep_plugin.invoke(name)`, `setup()` called with `PluginServices`, and cached in `_harness_plugins`. Then `bind(plugin-bindings.<Name>)` is called to produce the per-agent `AgentPlugin`.

### Plugin Enable / Bind Flow

The old `plugins = {Name: {enabled: true/false}}` dict pattern is replaced by flat lists:

1. `plugins.enabled` — ordered list of plugin names to enable. `["*"]` enables all.
2. `plugins.disabled` — subtracts from the enabled set. Always excludes, even with `"*"`.
3. Both lists **replace** (not union) during agent-level deep merge.

At `create_agent(kind)` time:
- Plugin setup (`setup()`) is lazy — first agent requesting a plugin triggers instantiation.
- `bind()` receives the merged `plugin-bindings.<PluginName>` from the agent's registered config, producing the per-agent `AgentPlugin` instance with the correct configuration.

### Runner Integration

The runner receives `RuntimeConfig` directly:

- `runtime.agent` → main agent kind for the primary actor
- `runtime.actors` → named actor definitions (each selecting an agent kind + optional overrides)
- `runtime.channels` → channel configurations (array of tables, each with name, bind_address, target_address, and extra options)
- `runtime.location` → where the runtime executes (`"process"` or `"docker"`)

Channel resolution and actor-to-agent wiring remain the same in structure — only the config source changes from old `main.*` to new `runtime.*`.

---

## Migration from old config

| Old key | New key |
|---|---|
| `[main]` | `[runtime]` |
| `main.agent` | `runtime.agent` |
| `main.runtime.kind` | `runtime.location` |
| `[[main.channels]]` | `[[runtime.channels]]` |
| `[main.actors.<name>]` | `[runtime.actors.<name>]` |
| `[main.runtime]` | *(pass-through via `runtime.extra="allow"` — Docker settings not yet placed)* |
| `[platform.agent_defaults]` | `[agent.defaults]` |
| `[[platform.agents]]` (list) | `[agents.<name>]` (table) |
| `[platform.enabled_plugins]` | `[agent.defaults.plugins].enabled` |
| `[platform.plugins.<Name>]` | `[agent.defaults.plugin-bindings.<Name>]` |
| `[harness.consolidator]` | `[exts.ep_consolidator._default]` |
| `[harness.message_store]` | `[exts.ep_message_store._default]` |
| `[harness.mail_route]` | `[exts.ep_mail_route._default]` |
| `[harness.providers]` | `[exts.ep_provider.<impl>]` |
| `[harness.tools.<Name>]` | `[exts.ep_tool.<Name>]` |
| `[harness.skills_loader]` | `[exts.ep_skills_loader._default]` |
| `[harness.interceptors]` | `[harness].interceptors` (selection) + `[exts.ep_turn_interceptor.<name>]` (config) |
| `[harness.subagent_defaults]` | *(removed — deferred to SubagentPlugin future BEP)* |
| `[[harness.subagents]]` | *(removed — deferred to SubagentPlugin future BEP)* |
| `tools = "*"` | `tools.enabled = ["*"]` |
| `tools = ["ReadFile"]` | `tools.enabled = ["ReadFile"]` |
| `plugins = {Name: {enabled: true}}` | `plugins.enabled = ["Name"]` + `plugin-bindings.Name = {...}` |


## Addendum: `ep_agent` — Agent Spec Factories

### Motivation

Extension packages previously registered agents by calling `AgentRegistry.register(...)` at import time (the pattern documented in `bos/exts.py`). This bypassed `AgentConfig` validation and the `[agent.defaults]` merge, offered no config override path, overwrote same-named agents silently with ordering-dependent precedence, and was the only extensible surface in the framework not modeled as an `ExtensionPoint`.

### Design

A new core extension point `ep_agent` (defined in `bos.core.contract`) holds **agent spec factories**: sync or async functions that return an agent spec dict. The returned spec must be validatable by `AgentConfig` — it is the exact same shape as a `[agents.<name>]` TOML table, keeping one canonical agent schema regardless of source.

```python
from bos.core import ep_agent

@ep_agent(name="weather_agent", description="Weather forecasting agent")
def weather_agent(region: str = "us") -> dict:
    return {
        "system_prompt": f"You report weather for {region}.",
        "model": "gemini-2.5-flash",
        "tools": {"enabled": ["GetWeather"]},
    }
```

**Static metadata, lazy spec.** `name` and `description` are registration metadata. Discovery surfaces (`AgentRegistry.describe()`, CLI agent listings, the subagent role list) read metadata only and never invoke factories.

**`[exts.ep_agent.<name>]` = factory parameters, not spec overrides.** Standard EP semantics apply unchanged: bootstrap step 3 merges the section into the extension's registered defaults, and the values are passed into the factory as keyword arguments (e.g. `region = "eu"` above). The factory interprets them however it likes; users who want to override the *resulting spec* use `[agents.<name>]`.

**Invocation: exactly once per bootstrap.** Factories run in bootstrap step 4 — after environment loading (step 1) and the `[exts]` defaults merge (step 3) — so a factory sees the project's env vars, working directory, and its merged parameters. Context sensitivity comes from per-project bootstrap, not per-call; within a process the resolved spec is stable. `bootstrap_platform()` is sync at all call sites, so async factories are run via `asyncio.run()`.

**Validation at startup.** Each returned spec is validated via `AgentConfig` immediately after invocation. An invalid spec crashes bootstrap with the factory name in the error — the same gate external agent files go through, moved as early as possible.

**Merge chain.** For each agent name:

```
[agent.defaults]  →  ep_agent factory result  →  [agents.<name>]
```

Either of the last two terms may be absent; a pure-TOML agent is the degenerate case with no factory term. The `[agents.<name>]` entry deep-merges per the BEP 6 merge semantics (dicts merge recursively, lists replace, scalars replace) — so a user can partially override a packaged agent (e.g. just its `model`) from config. This supersedes the previous behavior where a same-named config agent replaced an extension-registered agent wholesale and only by accident of load order.

**`_default`.** A factory may register `_default`; it participates in the chain like any named agent. The Python `default_agent_spec` remains the fallback used only when no `_default` was produced by factories or TOML.

**`AgentRegistry` becomes internal.** It remains the resolved-spec store read by `AgentHarness.create_agent()`, but it is written only by `bootstrap_platform()`. Direct `AgentRegistry.register()` calls from extension packages are no longer a documented or supported registration path.

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-30 | Initial draft | Improve the configuration architecture clarity and apply reliable validations |
| 2026-05-30 | Detailed design | Added merge semantics, harness ctor changes, subagent removal, clarified mapping tables, fixed contradictions with code |
| 2026-05-30 | System design | Added architecture overview, config loading pipeline, bootstrap relocation, harness slim-down, plugin enable/bind flow, runner integration |
| 2026-05-30 | Rename [ext] → [harness] | TOML section, Pydantic model, and all references renamed for clarity |
| 2026-05-30 | Design accepted | Full design approved — ready for implementation planning |
| 2026-06-12 | Addendum: `ep_agent` agent spec factories | Replace direct `AgentRegistry.register` from extension packages with a core EP; one validated agent schema, config-driven factory params, deterministic merge chain |
