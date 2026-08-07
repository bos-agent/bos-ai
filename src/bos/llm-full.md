# BOS — Full Reference for AI Agents

> A single, dense, self-contained reference to how BOS works: configuration,
> extension points, plugins, channels, skills, the CLI, the runtime, and Python
> packaging. Written for an AI coding agent (or a human) that needs the complete
> picture in one file.

> **About this document.** This is written for an agent working inside a **BOS
> workspace** — a project that has installed the `bos-ai` package — **not** inside
> the BOS source repository. So:
>
> - **Configure and build** against the installed package: import from `bos` (e.g.
>   `from bos.core import ep_tool`) and edit your workspace's `.bos/config.toml`.
> - **To inspect BOS internals**, this doc cites BOS modules by their **importable
>   dotted name** (e.g. `bos.config.workspace`). You can read the source two ways:
>     - Locally: it ships with the installed package. Find it with
>       `python -c "import bos, os; print(os.path.dirname(bos.__file__))"`.
>     - On GitHub: `https://github.com/bos-agent/bos-ai/blob/main/src/<dotted/path>.py`
>       (e.g. `bos.config.workspace` → `.../src/bos/config/workspace.py`).
> - **Do not** assume any `src/bos/...` path exists in your workspace; those are
>   locations in the BOS repository, linked above.

BOS (`bos-ai` on PyPI) is a **lightweight, extensible framework for building and running
multi-agent systems**.
Install it and you have a working agent in one command; grow it into a multi-agent
project by editing one TOML file and dropping Python/Markdown files into a few
conventional directories.

- Package: `bos-ai`, import root `bos`, CLI entry point `boscli` (`bos.cli.entry:main`).
- Python `>=3.13`. LLM access is via `litellm` by default (any provider it supports).
- Public API surface: the `bos.core` package. Symbols with a leading underscore are
  exported for extension authors but are **not** stable.

---

## 1. Mental model

A running BOS deployment is a **gateway process** that hosts one or more **actors**.
Each actor is a long-lived, addressable mailbox bound to an **agent** (an LLM-driven
loop). **Channels** bridge the outside world (TUI, Telegram, Lark, HTTP) to an
actor's mailbox. Cross-cutting services — chat persistence, memory consolidation,
background jobs, message routing — are owned by the **harness** and selected by name.

```
                 ┌─────────────────────────── gateway process ───────────────────────────┐
 external client │   channel ──▶ mailbox ──▶ actor ──▶ agent (LLM loop) ──▶ tools/plugins  │
 (TUI/Telegram)  │     ▲                        │              │                           │
                 │     └──────── reply ─────────┘        harness services:                 │
                 │                                       chat_store, consolidator,         │
                 │                                       mail_route, job_runner            │
                 └────────────────────────────────────────────────────────────────────────┘
```

Everything pluggable is a named **extension** registered at an **extension point**.
The agent itself is assembled from: a system prompt, a model, a set of **tools**, a
set of **plugins** (which contribute more tools + prompt sections + interceptors),
and config knobs. Configuration is one TOML file (`.bos/config.toml`) plus optional
Python extensions and Markdown/TOML agent files.

Key vocabulary:

| Term | What it is | Defined in |
| --- | --- | --- |
| **Agent** | An LLM-driven turn loop with tools, plugins, a system prompt, a model | `bos.core.agent` |
| **Actor** | A named, addressable, restartable runtime instance bound to one agent kind | `[runtime.actors.<name>]` |
| **Gateway** | The process that hosts actors + channels + an HTTP control plane | `bos.gateway`, `boscli gateway` |
| **Channel** | Bridges an external client to an actor's mailbox | `ep_channel`, `[[runtime.channels]]` |
| **Harness** | Lifecycle owner of shared services (chat store, consolidator, jobs, mail) | `bos.core.harness.AgentHarness` |
| **Extension Point** | A named registry of interchangeable implementations | `bos.core.registry.ExtensionPoint` |
| **Extension** | One registered implementation at an extension point | `@ep_tool`, `@ep_channel`, … |
| **Plugin** | A bundle that adds tools + prompt sections + interceptors to an agent | `ep_plugin`, `HarnessPlugin`/`AgentPlugin` |
| **Skill** | A Markdown playbook the agent can load on demand | `SKILL.md`, `SkillsPlugin` |
| **Tool** | An async function the LLM can call | `@ep_tool` |

---

## 2. Install & quick start

```bash
pip install bos-ai            # or: uvx boscli ...   (no install)

# One-shot, no project:
OPENAI_API_KEY=<key> boscli ask "how are you" --model openai/gpt-4o

# A real project:
mkdir my-agent && cd my-agent
boscli init            # guided setup: purpose, archetype, provider/model
boscli gateway start   # start the runtime (hosts actors + channels)
boscli tui             # connect the terminal UI
```

The model string is LiteLLM-style `provider/model` (e.g. `openai/gpt-4o`,
`gemini/gemini-2.5-flash`, `anthropic/claude-...`, `deepseek/deepseek-...`). The
provider prefix also selects a custom `@ep_provider` if one is registered under that
name (see §6.2); otherwise it falls back to LiteLLM, which reads the matching
`*_API_KEY` env var.

Optional extras: `pip install 'bos-ai[search]'` (Tavily web search),
`'bos-ai[lark]'` (Lark/Feishu channel), `'bos-ai[all]'`.

---

## 3. Project layout & home directories

A **workspace** is any directory tree containing `.bos/config.toml`. Commands walk
up from the current directory to find it (see §4.1).

```
my-agent/
├── .bos/
│   ├── config.toml        # the one config file (the "bos_dir" is .bos/)
│   ├── .env               # secrets, if [platform].envfile = ".env"
│   ├── agents/            # external agent definitions (*.md / *.toml)  [agent_dirs]
│   ├── extensions/        # project-local Python extensions             [extensions]
│   ├── skills/            # project-local skills (dirs with SKILL.md)
│   ├── messages/          # JsonlChatStore persistence (default)
│   ├── mailboxes/         # JsonlMailRoute persistence (default)
│   └── gateway.state      # runtime port/PID discovery file
└── (your project files)
```

- **`bos_dir`** = the directory containing `config.toml` (i.e. `.bos/`). All relative
  paths in config (`envfile`, `extensions`, `agent_dirs`, store dirs) resolve against
  `bos_dir`. (`bos.config.workspace`.)
- **`BOS_HOME`** (default `~/.bos`) holds global state: `~/.bos/agents/<name>` for the
  default preset, `~/.bos/presets/<name>` when running a built-in preset. (`_get_bos_home`.)

---

## 4. Configuration

All configuration is validated by Pydantic models in `bos.config.schema`.
The root is `RootConfig` with top-level sections `[platform]`, `[harness]`, `[exts]`,
`[agent]`, `[agents.*]`, `[runtime]`. The canonical, fully-commented template is
the repo's `bos/config/template.toml` (what `boscli init --minimal` emits).

### 4.1 Config discovery & selection

Resolution order (`bos.config.workspace`, `_resolve_config` / `find_discovered_config`):

1. Walk from the current directory up through ancestors; the first `.bos/config.toml`
   wins. (A `.bos/` without `config.toml` is skipped; the walk continues.)
2. `BOS_CONFIG` env var (absolute path). If **both** a discovered file and `BOS_CONFIG`
   exist and differ → hard error (ambiguous).
3. Otherwise error: *"No BOS workspace found."*

`boscli -c/--config <X>` (or `BOS_CONFIG`) accepts either:

- An existing **file path** → `bos_dir` is the file's parent.
- A built-in **preset name** → resolves to the packaged `bos/config/presets/<name>.toml`, with
  `bos_dir = ~/.bos/presets/<name>` (created on demand). Built-in presets: `default`,
  `team`. (`resolve_config_source`, `presets_dir`.)

### 4.2 `[platform]` — environment & discovery

```toml
[platform]
envfile = ".env"                          # dotenv file, resolved against bos_dir
extensions = ["bos.exts", "./extensions"] # modules to import + dirs to scan
agent_dirs = ["./agents"]                 # dirs scanned for *.md / *.toml agents

[platform.envs]                           # inline env vars (applied before envfile)
BOS_MODEL = "openai/gpt-4o"
BOS_CAPABILITY_LIMIT = "50"
```

- **Defaults** (when `[platform]` is absent): `extensions = ["bos.exts", "./extensions"]`,
  `agent_dirs = ["./agents"]`. (`PlatformConfig` in `bos.config.schema`; applied in `bos.config.workspace`.)
- **Env loading order**: `[platform.envs]` is applied to `os.environ` first, then the
  `envfile` is loaded with `override=True`. (`bos.config.workspace.bootstrap_platform`.)
- **`extensions`** entries are each resolved: if the entry exists as a path relative to
  `bos_dir`, it is loaded as a **directory/file path** (Python files scanned & imported);
  otherwise it is imported as a **module name**. (`bos.config.workspace.bootstrap_platform`;
  loaders `_load_ext_paths` / `_load_ext_modules`.) `"bos.exts"` is the module that imports all
  built-ins + discovers entry points (§9).
- **`agent_dirs`** entries are scanned for `*.toml` and `*.md` files; each becomes a named
  agent (§4.7).

### 4.3 `[harness]` — select service implementations

Each key names a registered extension by name (`extra='forbid'` — unknown keys error).

```toml
[harness]
consolidator = "LLMConsolidator"   # ep_consolidator
chat_store   = "JsonlChatStore"    # ep_chat_store
mail_route   = "JsonlMailRoute"    # ep_mail_route
job_runner   = "InProcJobRunner"   # ep_job_runner
interceptors = []                  # ordered list of ep_turn_interceptor names/configs
```

Defaults shown are the `HarnessConfig` defaults. The harness instantiates each by name
at startup (`bos.core.harness.AgentHarness.__aenter__`). Built-in adapters are
registered by `bos.core.defaults` (imported at harness open time), so
these names resolve even without `bos.exts`.

There is **no provider key in `[harness]`** — provider selection happens per-model-string
via `ep_provider` (§6.2).

### 4.4 `[exts.<ep_name>.<impl_name>]` — configure extensions

This is the universal mechanism for passing config/defaults into any registered
extension. `<ep_name>` is an extension-point name (`ep_*` for core, `pep_*` for
plugin-defined); `<impl_name>` is the registered implementation. The table becomes the
**defaults** merged into that extension and passed as keyword arguments when it is
invoked. (`bos.config.workspace.bootstrap_platform` → `ExtensionPoint.update_defaults`; consumed by
`ExtensionPoint.invoke` via `_compact(ext.defaults, kwargs)`.)

`[exts]` has `extra='allow'`, so newly registered extension points accept config with no
schema change. An `[exts.<ep_name>]` whose name matches no registered extension point is
logged and ignored. Examples:

```toml
[exts.ep_consolidator.LLMConsolidator]
model = "gemini/gemini-2.5-flash"

[exts.ep_chat_store.JsonlChatStore]
store_dir = "./messages"

[exts.ep_tool.WebSearch]                 # tool-specific runtime config
priority = ["tavily", "duckduckgo"]
timeout_seconds = 15
[exts.ep_tool.WebSearch.tavily]
api_key_env = "TAVILY_API_KEY"

[exts.ep_provider.litellm]               # provider defaults
# model = "..."

[exts.pep_skills_loader.FileSystemSkillsLoader]  # plugin-defined EP
skill_dirs = ["skills"]
```

### 4.5 `[agent.defaults]` and `[agents.<name>]` — agents

`[agent.defaults]` provides defaults merged into every agent. `[agents.<name>]` defines a
named agent (its `AgentConfig`). Both use the same `AgentConfig` schema (`extra='allow'`).

```toml
[agent.defaults]
# system_prompt = "..."        # usually set per-agent instead
# model = "openai/gpt-4o"      # precedence below
# agent_name = "bos"
# reasoning_effort = "medium"  # low | medium | high
max_tokens = 131072
max_iterations = 80
# tool_noise_filter = "strip_all"   # strip_all | keep_all
# history_attribution = false

[agent.defaults.tools]
enabled = ["*"]                # "*" = all registered tools
# disabled = ["WriteFile"]
# usages = { ToolName = "override usage text" }

[agent.defaults.plugins]
enabled = ["*"]                # "*" = all registered plugins (minus disabled)
# disabled = []

[agent.defaults.plugin-bindings.SubagentPlugin]   # per-plugin config (note the hyphen)
enabled = ["researcher", "writer"]

[agents.researcher]
description = "Researches codebases."
system_prompt = "You research codebases and report findings."
model = "openai/gpt-4o"
[agents.researcher.tools]
enabled = ["ReadFile", "GrepSearch", "WebSearch"]
```

`AgentConfig` fields (`bos.config.schema`):

| Field | Type / default | Notes |
| --- | --- | --- |
| `system_prompt` | `str` | The agent's base prompt. Plugins append sections at runtime. |
| `model` | `str` | LiteLLM-style `provider/model`. |
| `agent_name` | `str` | Identity used for memory scoping etc. |
| `reasoning_effort` | `low\|medium\|high` | Passed to the model if supported. |
| `max_tokens` | `int` = 131072 | Context budget before compaction. |
| `max_iterations` | `int` = 80 | Max tool-call iterations per turn. |
| `tool_noise_filter` | `strip_all\|keep_all` | How prior tool output is kept in context. |
| `history_attribution` | `bool` = false | Tag history with the speaking actor. |
| `tools` | `{enabled, disabled, usages}` | `enabled=["*"]` for all; `usages` overrides per-tool guidance. |
| `plugins` | `{enabled, disabled}` | `enabled=["*"]` for all registered plugins. |
| `plugin-bindings` | `{<Plugin>: {…}}` | Per-plugin settings; key is hyphenated in TOML. |
| `_parent` | `str` | Inherit from another `[agents.*]` agent (deep-merged underneath; see §4.8). |

**Model precedence** (see `bos.core.llm.LLMClient`): `[agents.<name>].model` → `[agent.defaults].model`
→ `BOS_MODEL` env → `[exts.ep_provider.<provider>].model`.

**Tool resolution**: an agent sees a merged, filtered view over `[agent-local plugin tools,
global ep_tool registry]` (local wins on name clash). `enabled` is an include list
(`"*"` / `None` = all), `disabled` is an exclude list. (`bos.core.harness`,
`ResolvedToolSet` / `create_agent`.) An explicit `enabled = []` is an *empty* include list —
no tools at all, and likewise no plugins under `[…plugins]`. That is not the same as omitting
the key, which inherits `[agent.defaults]`; see §4.8.

### 4.6 `[runtime]` — actors, gateway, channels

```toml
[runtime]
main_actor = "main"            # which actor is the default mention/route target

[runtime.gateway]
host = "127.0.0.1"
port = 0                       # 0 = auto-assign a free port (discover via gateway.state)
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
[runtime.actors.main.agent_cfg]      # per-actor overrides (same shape as [agent.defaults])
# model = "openai/gpt-4o"
[runtime.actors.main.agent_cfg.plugin-bindings.SubagentPlugin]
enabled = ["researcher"]

[[runtime.channels]]           # array-of-tables: zero or more persistent channels
type = "TelegramChannel"       # registered ep_channel name
channel_id = "telegram+main"   # unique id
display_name = "Telegram"
target_actor = "main"          # must exist in [runtime.actors]; defaults to main_actor
settings = { token_env = "TELEGRAM_BOT_TOKEN" }
```

Rules (validated in `bos.config.workspace`, the `resolve_gateway_*` methods):

- Actor names must match `[A-Za-z_][A-Za-z0-9_-]*` (mention-safe). The TOML key **is** the
  actor's identity and memory scope.
- `runtime.main_actor` must exist in `runtime.actors`.
- Each channel needs a unique `channel_id`; `target_actor` must be a defined actor;
  `type` must be a registered `ep_channel` (and may not be `HttpChannel`, which is gateway
  infrastructure).
- An actor address is `agent@<name>`; a channel address is `channel@<channel_id>`.

> Migration note: `[main]` was removed; use `[runtime]` + `[runtime.actors]`.
> `runtime.agent` / `runtime.default_actor` are removed (use `runtime.actors` /
> `runtime.main_actor`). (`bos.config.schema.validate_config`.)

### 4.7 External agent files (`agent_dirs`)

Every `*.toml` / `*.md` file in an `agent_dirs` directory defines one agent. The
**filename stem is the agent name** unless the file sets `name` explicitly. External files
**replace** an inline `[agents.<name>]` of the same name entirely (no merge). Files load
alphabetically within a dir, dirs in list order; later wins. (`bos.config.workspace`,
`resolve_agents` / `_load_external_agent_candidate`.)

- **TOML agent file** (`agents/researcher.toml`): a flat `AgentConfig` table.
- **Markdown agent file** (`agents/writer.md`): YAML-ish frontmatter → config fields, and
  the body becomes `system_prompt`. If frontmatter is invalid, the whole file is used as
  `system_prompt`. Putting `system_prompt` in frontmatter is rejected (the body is the
  prompt). (`bos.config.workspace._load_external_agent_markdown`.) The frontmatter parser supports a **small
  YAML subset**: flat scalars, and one-level keys whose value is an **inline** list
  (`enabled: [ReadFile, WriteFile]`) or scalar. Deeper block nesting (e.g. a `tools:` block
  with an indented `enabled:` block list) is **not** supported and silently falls back to
  using the whole file as `system_prompt` — use inline lists, or a `.toml` agent file for
  richer structure. (`bos.config.workspace._parse_simple_yaml_mapping`.)

```markdown
---
description: Writes documentation.
model: openai/gpt-4o
tools:
  enabled: [ReadFile, WriteFile]
---
You are a meticulous technical writer. Produce clear, accurate docs.
```

### 4.8 Agent resolution chain (precedence)

For each agent name, the final spec is a deep merge in this order (`bos.config.workspace.bootstrap_platform`):

```
[agent.defaults]  →  ( _parent chain, root-first )  →  @ep_agent factory result (if any)  →  [agents.<name>] / external file
```

**What counts as "set".** Only fields a term actually specifies take part in the merge; a
field it omits inherits from the left. An explicit **empty list** *is* a value and replaces
what it inherits (`enabled = []` → nothing enabled), but an explicit **null** on an optional
field (`model`, `system_prompt`, `agent_name`, `reasoning_effort`, `tool_noise_filter`) is
read as "not configured" and inherits instead of clearing. TOML cannot spell null; Markdown
frontmatter and `@ep_agent` factories can, so a bare `model:` leaves an inherited `model`
standing rather than wiping it. Clearing an inherited optional is not expressible.
(`bos.config.schema._agent_config_to_dict`.)

**Inheritance (`_parent`).** An `[agents.<name>]` table may set `_parent = "<other agent>"` to
inherit that agent's resolved spec, deep-merged underneath it (same merge semantics: dicts merge,
lists/scalars replace). Chains resolve transitively (`c` → `b` → `a`); `[agent.defaults]` remains
the global floor under the chain. `_parent` may reference only another `[agents.*]` agent (inline or
external file) — not an `@ep_agent` factory or `[agent.defaults]`. A cycle or unknown parent raises at
bootstrap. The directive is stripped before registration and never reaches the `Agent` constructor.
(`bos.config.workspace._resolve_agent_inheritance`.)

```toml
[agents.leader]
system_prompt = "You coordinate a team."
[agents.leader.tools]
enabled = ["ReadFile", "AskSubagent"]

[agents.niceleader]
_parent = "leader"                               # inherits model/tools/plugins/…
system_prompt = "You coordinate a team, warmly."  # overrides just this
```

The built-in `BOS` agent is a normal builtin `@ep_agent` extension
(`bos.extensions.agents.bos`, a general assistant with memory/plan/task/skills/subagent
plugins), registered when `bos.exts` is loaded (the default `[platform.extensions]`). It
resolves through the normal chain above — `[agents.BOS]` composes over it, or inherit from it
via `_parent = "BOS"`. There is no implicit default-agent fallback beyond the conventional
`"BOS"` name presets reference; drop `bos.exts` and you must define your own agent.

---

## 5. Extension points (the registry model)

Everything pluggable is a named **extension** at an **extension point**. The machinery is
in `bos.core.registry`.

- **`ExtensionPoint(name, description, validate=None)`** — a registry of implementations.
  - Public names (no leading `_`) are recorded in a global lookup; a **duplicate public
    name raises at construction time** (crashes startup). Names starting with `_` are
    private (not configurable, not in the lookup, may be instantiated repeatedly).
  - **Naming convention** (not enforced): core points in `bos.core.contract` are `ep_<name>`;
    plugin-defined points are `pep_<name>` (plugin extension point). `[exts.<name>]` keys use
    these names; `ExtensionPoint.lookup(name)` resolves them.
  - `register(ext)`, `get(name)`, `has(name)`, `describe()`, `await invoke(name, kwargs)`,
    `update_defaults(name, defaults)`.
  - **`invoke`** calls the registered function with `_compact(ext.defaults, kwargs)` and
    awaits if it is async. So `[exts.<ep>.<impl>]` config + call-site kwargs are merged in.

- **`Extension`** — dataclass `{name, fn, description, defaults, metadata}`.

- **`@extension_point(name=..., description=..., defaults=..., **metadata)`** — the decorator
  form. Used directly as `@ep_tool(...)`, `@ep_channel(...)`, etc. The decorated object may
  be a function **or a class** (for stateful extensions like channels, chat stores, plugins).

- **`ToolRegistry(ExtensionPoint)`** — `ep_tool` is a `ToolRegistry`. It validates tools at
  registration (`default_validate`): `parameters` JSON-schema is required; schema property
  names must be a subset of the function signature (unless the fn takes `**kwargs`);
  `result_serializer ∈ {auto, json, str}`. It serializes results and builds OpenAI tool
  schemas (`to_openai_schema`, `build_openai_schema`).

### 5.1 The core extension points (`bos.core.contract`)

| Extension point | Kind | Contract / returns |
| --- | --- | --- |
| `ep_tool` | function | Async tool the LLM can call; `parameters` JSON-schema required. |
| `ep_provider` | function | `async (messages, **kwargs) -> LLMResponse`; selected by model prefix. |
| `ep_agent` | factory | Sync/async fn returning an agent-spec dict (validatable as `AgentConfig`). |
| `ep_chat_store` | factory/class | Builds a `ChatStore` (persistence + context assembly). |
| `ep_consolidator` | factory | Builds a `Consolidator` (summarization/memory). |
| `ep_turn_interceptor` | factory | Builds a `TurnInterceptor` (per-turn hooks). |
| `ep_job_runner` | factory | Builds a `JobRunner` (off-critical-path jobs, BEP 11). |
| `ep_mail_route` | factory | Builds a `MailRoute` (`bind(address)->MailBox`, `deliver(env)`). |
| `ep_channel` | factory/class | Builds a `Channel` (bridges clients to a mailbox). |
| `ep_plugin` | class/factory | A `HarnessPlugin` (adds tools/prompt/interceptors). |

Plugin-defined example: `pep_skills_loader` (in `bos.plugins.skills.plugin`).

---

## 6. Writing extensions

All extension authoring is "import a decorator from `bos.core`, decorate a function or
class." Discovery happens because the module is imported — via `[platform].extensions`
(a path dir scanned, or a module name imported) or via the `bos.exts` entry point (§9).

### 6.1 Tools — `@ep_tool`

```python
from bos.core import ep_tool

@ep_tool(
    name="WordCount",
    description="Count the words in a text.",            # shown to the model
    parameters={                                          # JSON schema, REQUIRED
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to count."}},
        "required": ["text"],
    },
    usage="Longer guidance shown in the system prompt for when/how to use this tool.",
    parallel_safe=True,            # may run concurrently with other parallel-safe tools
    result_serializer="auto",      # auto | json | str
)
async def word_count(text: str) -> str:
    return f"{len(text.split())} words"
```

Anatomy & rules (`bos.core.registry`, `bos.extensions.tools.filesystem`):

- **Async preferred**; sync functions also work (`invoke` awaits as needed). CPU/blocking
  work should go through `asyncio.to_thread` (the filesystem tools do this).
- **`parameters`** is mandatory JSON schema. Its property names must be a subset of the
  function's parameters unless the function accepts `**kwargs`.
- **Return value** is serialized to the string the model sees: `auto` → `json.dumps` for
  JSON-ish types else `str()`; `json` → always `json.dumps`; `str` → always `str()`.
- **`usage`** (metadata) is the long-form guidance surfaced to the model; `description` is
  the short schema description. `parallel_safe` (metadata) gates concurrent execution.
- **`ToolContext` injection**: declare a parameter named `context: ToolContext | None`. The
  harness passes it; it carries the parent turn (e.g. `context.parent` for spawning
  subagents). (See `AskSubagent` in `bos.plugins.subagent`.)
- **Per-tool config** comes from `[exts.ep_tool.<Name>]`, merged in as defaults/kwargs.
  Example: the filesystem search tools read `replace_ignore` / `extend_ignore` /
  `remove_ignore` from `[exts.ep_tool.GrepSearch]`.
- **Enable/disable** per agent via `[…tools].enabled / .disabled`; override guidance via
  `[…tools].usages`.

Built-in tool families: filesystem (`ReadFile`, `WriteFile`, `EditFile`, `GlobSearch`,
`GrepSearch`), system, knowledge/web search.

### 6.2 Providers — `@ep_provider`

A provider is `async (messages, **kwargs) -> LLMResponse`. The LLM client
(`bos.core.llm.LLMClient`) **dispatches by model prefix**:

```python
# bos.core.llm.LLMClient — simplified
model = kwargs.get("model") or os.getenv("BOS_MODEL")
provider_name, sep, model_name = model.partition("/")     # "codex/gpt-5" -> ("codex","gpt-5")
if not sep or not ep_provider.has(provider_name):
    provider_name, model_name = "litellm", model           # fall back, keep full string
return await ep_provider.invoke(provider_name, kwargs | {"messages": messages, "model": model_name})
```

So: register `@ep_provider(name="codex")` and any agent whose `model = "codex/..."` routes
to it. If the prefix is not a registered provider (e.g. `openai/gpt-4o`), it falls back to
the built-in `litellm` provider with the full model string. Provider defaults come from
`[exts.ep_provider.<name>]`. The only built-in provider is `litellm`, which is also the
default fallback (registered by `bos.core.defaults`); it reaches every provider LiteLLM
supports, so a custom `@ep_provider` is only needed for non-LiteLLM backends.

### 6.3 Chat stores — `@ep_chat_store`

A chat store owns conversation persistence **and** context assembly (token estimation,
summary handling, tool-noise filtering). Register a class; its `__init__` receives
`[exts.ep_chat_store.<Name>]` config plus harness context (`bos_dir`, `workspace_dir`).

The `ChatStore` protocol it must implement (see `InMemChatStore` in
`bos.extensions.chat_stores.in_memory` for a complete, minimal example):

```
commit_turn(chat_id, messages, *, turn_id) -> ChatCommit
get_context(chat_id, *, tokenizer_model=None, filter_mode=None) -> ContextResult
get_compaction_messages(chat_id, *, filter_mode=None) -> list[Message]
estimate_tokens(chat_id, *, tokenizer_model=None, filter_mode=None) -> TokenEstimate
save_summary(chat_id, summary) / get_summary(chat_id) -> Message | None
get_messages(chat_id, *, active_only=True) -> list[Message]
get_revision(chat_id) -> int
get_messages_since(chat_id, *, revision) -> list[Message]
list_chats() -> dict[str, ChatMeta]
```

Built-ins: `JsonlChatStore` (default, persistent under `bos_dir`), `InMemChatStore`.
Select via `[harness].chat_store`.

### 6.4 Consolidators, mail routes, job runners, interceptors

- **`@ep_consolidator`** → a `Consolidator` (summarizes history / drives memory). The
  harness builds it with `{model: BOS_CONSOLIDATOR_MODEL, llm}`. Default `LLMConsolidator`;
  config via `[exts.ep_consolidator.LLMConsolidator]` (e.g. `model`).
- **`@ep_mail_route`** → a `MailRoute`: `bind(address) -> MailBox` and `async deliver(env)`.
  Default `JsonlMailRoute`. This is point-to-point message routing between actors/channels.
- **`@ep_job_runner`** → a `JobRunner` (BEP 11): `start`, `submit(job)`, `bind_trigger`,
  `drain`, `status/list/retry/cancel`. Default `InProcJobRunner`; built with `{bus: EventBus}`.
  Used for off-turn work like memory consolidation. Triggers: `session_close | idle | manual`.
- **`@ep_turn_interceptor`** → a `TurnInterceptor` with `async intercept(stage, context)`.
  Configure an ordered chain via `[harness].interceptors` (list of names or `{name=…, …}`
  tables). Plugin interceptors run best-effort first, then the configured chain
  (`_CompositePluginInterceptor`). Raise `AbortTurn` to stop a turn.

### 6.5 Channels — `@ep_channel`

A channel bridges an external client to an actor's mailbox. The `Channel` protocol
(`bos.core.contract`):

```python
class Channel(Protocol):
    channel_id: str
    display_name: str | None
    target_actor: str
    identity_key: str | None
    async def run(self, mailbox: MailBox) -> None: ...
```

- `run(mailbox)` is the channel's long-lived loop: read external input, send it into
  `mailbox`, await the actor's reply, and push it back to the client. It runs for the life
  of the gateway.
- **`BaseChannel[SettingsT]`** is an optional helper that stores
  `channel_id / target_actor / settings / display_name / runtime` and implements the
  property boilerplate; subclass it and implement `run`. Set `SettingsType` to a settings
  dataclass/model to get typed `settings` parsing.
- **Settings flow**: `[[runtime.channels]].settings` (a TOML table) is handed to the channel
  factory; conventionally tokens are referenced by env-var name (`token_env`,
  `app_id_env`, …) rather than inlined.
- **Instantiation**: the gateway resolves each `[[runtime.channels]]` entry
  (`workspace.resolve_gateway_channels`), looks up the `ep_channel` by `type`, constructs it,
  binds it to `target_actor`'s mailbox, and runs it.

Built-in channels: `TelegramChannel`, `LarkChannel` (needs `bos-ai[lark]`). `HttpChannel`
is gateway infrastructure (the control-plane API), not a user channel.

### 6.6 Agent factories — `@ep_agent`

A code-defined alternative to `[agents.<name>]`. A sync/async function returns an agent-spec
dict (validatable as `AgentConfig`); it is invoked **once per bootstrap** and receives its
`[exts.ep_agent.<name>]` config as keyword arguments. The result merges as
`[agent.defaults] → factory result → [agents.<name>]`, so users can still override it.

```python
from bos.core import ep_agent

@ep_agent(name="weather_agent", description="Weather forecasting agent")
def weather_agent(region: str = "us") -> dict:
    return {
        "system_prompt": f"You report weather for {region}.",
        "model": "gemini/gemini-2.5-flash",
        "tools": {"enabled": ["GetWeather"]},
    }
```

```toml
[exts.ep_agent.weather_agent]   # passed as kwargs to the factory
region = "eu"
```

Two factory agents ship built-in (registered whenever `bos.exts` is on
`[platform.extensions]`, the default):

- **`BOS`** — the general-purpose assistant (memory, planning, tasks, skills, subagents).
- **`bos_config`** — the BOS project configuration specialist. Delegate configuration
  changes (`.bos/config.toml`, `[agents.*]`, `[exts.*]`, `[runtime.*]`, agent/skill
  registration) to it: it isolates edits in a scratch git worktree, validates with
  `boscli doctor` plus one live smoke turn (`boscli ask`), merges back only on success,
  and never restarts the gateway — it reports `uv run boscli gateway restart` as the
  user's final step. Any agent whose `SubagentPlugin` binding allow-list includes it
  (e.g. `enabled = ["*"]` — shipped by default in the built-in `BOS` agent and in
  scaffolded projects' `[agents.main]`) can reach it via
  `AskSubagent(role="bos_config", ...)`; run it directly with
  `boscli ask --agent bos_config "..."`.

```toml
[exts.ep_agent.bos_config]
workflow = "in_place"   # default "worktree"; "in_place" edits directly with
                        # timestamped backups (validation still applies)
```

Both are overridable via `[agents.BOS]` / `[agents.bos_config]` and inheritable via
`_parent`, like any factory agent.

---

## 7. Plugins

A **plugin** bundles tools + system-prompt sections + interceptors and attaches them to an
agent. There are two cooperating roles (`bos.core.contract`):

- **`HarnessPlugin`** (one per process, holds shared state) — registered at `ep_plugin`:
  - `name` → the plugin's name (the key used in config).
  - `default_config() -> Mapping` → defaults merged under every binding.
  - `async setup(services: PluginServices)` → called once, lazily, the first time any agent
    needs the plugin. `PluginServices` carries `bos_dir, workspace, llm, consolidator,
    chat_store, events, jobs, agent_runner`.
  - `validate_config(config)` → raise on bad config.
  - `bind(config) -> AgentPlugin` → produce a **per-agent** instance from merged config.
  - `async teardown()` → reverse-order cleanup at harness exit.
- **`AgentPlugin`** (one per agent) — what `bind` returns:
  - `name`.
  - `register_tools(registry: ToolRegistry)` → register agent-local tools (use the
    `registry(...)` decorator, same signature as `@ep_tool`).
  - `async get_system_prompt_section(context) -> str | None` → a prompt section appended this
    turn (often XML listing capabilities).
  - `get_interceptors() -> Sequence[TurnInterceptor]`.

### 7.1 Lifecycle (who calls what, when)

From `bos.core.harness` (`_bind_plugins_for_agent` / `_instantiate_and_setup_plugin` /
`create_agent`):

1. When an agent is created, the harness computes its enabled plugin set:
   `plugins.enabled` (with `"*"` expanding to **all registered `ep_plugin` names** minus
   `disabled`) minus `plugins.disabled`.
2. For each enabled plugin not yet instantiated: `ep_plugin.invoke(name)` builds the
   `HarnessPlugin`, then `await setup(services)` runs once and the instance is cached.
3. Per agent: `cfg = default_config() | plugin-bindings[name]`, plus an injected
   `agent_name`. Then `validate_config(cfg)` and `agent_plugin = bind(cfg)`.
4. The agent's tools = local registry (filled by every plugin's `register_tools`) overlaid
   on the global `ep_tool` registry, then include/exclude filtered.
5. Each turn, the agent asks every plugin for a `get_system_prompt_section`; plugin
   interceptors run best-effort ahead of the configured chain.
6. At harness shutdown, `teardown()` runs in reverse setup order.

### 7.2 Minimal plugin template

`SubagentPlugin` (`bos.plugins.subagent`) is a compact, complete reference. The smallest
shape:

```python
from collections.abc import Mapping, Sequence
from typing import Any
from bos.core.contract import AgentPlugin, PluginServices, ep_plugin
from bos.core.registry import ToolRegistry

@ep_plugin(name="MyPlugin")
class MyHarnessPlugin:
    @property
    def name(self) -> str: return "MyPlugin"
    def default_config(self) -> Mapping[str, Any]: return {"greeting": "hi"}
    async def setup(self, services: PluginServices) -> None: self._services = services
    def validate_config(self, config: Mapping[str, Any]) -> None: ...
    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        return MyAgentPlugin(config.get("greeting", "hi"))
    async def teardown(self) -> None: ...

class MyAgentPlugin:
    def __init__(self, greeting: str) -> None: self._greeting = greeting
    @property
    def name(self) -> str: return "MyPlugin"
    def register_tools(self, registry: ToolRegistry) -> None:
        @registry(
            name="Greet",
            description="Return a greeting.",
            parameters={"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]},
        )
        async def greet(who: str) -> str:
            return f"{self._greeting}, {who}!"
    async def get_system_prompt_section(self, context) -> str | None:
        return None
    def get_interceptors(self) -> Sequence[Any]:
        return []
```

Enable it: `extensions` must import the module that defines it, then in an agent:

```toml
[agents.main.plugins]
enabled = ["MyPlugin"]
[agents.main.plugin-bindings.MyPlugin]
greeting = "hello"
```

### 7.3 Plugins can define their own extension points (`pep_`)

A plugin may expose its own pluggable sub-implementations. `SkillsPlugin` does this:

```python
from bos.core.registry import ExtensionPoint
pep_skills_loader = ExtensionPoint(
    name="pep_skills_loader",
    description="Skills loader implementations.",
)

@pep_skills_loader(name="FileSystemSkillsLoader")
class FileSystemSkillsLoader: ...
```

Users then select/configure via `[exts.pep_skills_loader.<Impl>]`, exactly like core EPs.

### 7.4 Built-in plugins

Registered when `bos.exts` is loaded; the default agent enables
`MemoryPlugin, PlanPlugin, TaskPlugin, SkillsPlugin, SubagentPlugin`.

| Plugin | Adds | Key config (`plugin-bindings.<Plugin>`) |
| --- | --- | --- |
| `MemoryPlugin` | Persistent memory + recall tools; off-turn consolidation | `maxims` (categories, default `["user","self","rules"]`), `consolidation.{enabled,retention_days,model}` (`enabled` defaults `false`). No `scope` key — memory is isolated per agent identity (passing `scope` raises). |
| `PlanPlugin` | Planning tool(s) and prompt section | — |
| `TaskPlugin` | Async task creation/scheduling tools (BEP 11) | — |
| `SkillsPlugin` | `LoadSkill` tool + skill discovery (§8) | `skill_dirs`, `allow`, `exclude`, `loader`, `preload` |
| `SubagentPlugin` | `AskSubagent` tool to delegate to named agents | `enabled` (list/`"*"`), `disabled`, `task_template` |

`SubagentPlugin.enabled` is the allow-list of agent kinds the agent may delegate to; `"*"`
means all registered agents (no implicit exclusions — including the agent itself if it is
registered, so use explicit allow/deny lists to shape topology). It requires
`services.agent_runner` (raises in `setup` if absent). The plugin's own default allow-list
is empty (`[]`) — enabling the plugin does not by itself register `AskSubagent`; a binding
is required. The built-in `BOS` agent ships `plugin-bindings.SubagentPlugin.enabled =
["*"]` by default, and scaffolded projects ship the same binding on `[agents.main]`.

---

## 8. Skills

A **skill** is a Markdown playbook the agent loads on demand (progressive disclosure): the
system prompt lists only `name + description`; the `LoadSkill` tool returns the full body.

- **`SKILL.md`** format — YAML-ish frontmatter (`name`, `description`) then the instruction
  body:

  ```markdown
  ---
  name: coding-discipline
  description: Behavioral guidelines to reduce common LLM coding mistakes. Load before writing code.
  ---
  ## 1. Think Before Coding
  ...
  ```

- **Directory layout**: each skill is a directory containing `SKILL.md`. `skill_dirs` lists
  parent directories; the skill **name is the directory name** (frontmatter `description`
  feeds the listing).
- **`skill_dirs` default**: `["__builtin__", "skills"]`. The **`__builtin__` sentinel**
  expands (in place) to the packaged `bos.skills` dirs **followed by** `bos.skills`
  entry-point contributions from installed packages; then any explicit dirs (e.g. `skills`)
  are resolved against `bos_dir`. Later dirs win on name clashes, so **workspace skills
  override built-ins and package-contributed skills**. (`bos.plugins.skills.fs_skill_loader._get_skill_dirs`.)
- **`preload`**: skill names to inline fully into the system prompt at startup (skip the
  `LoadSkill` round-trip). **`allow` / `exclude`**: filter which skills are visible/loadable.
- Built-in skills: `coding-discipline`, `python`, `skill-creator`.
- Ship skills from a package via the `bos.skills` entry point (§9).

---

## 9. Python packaging & entry points

BOS reads three entry-point groups. Declare them in your package's `pyproject.toml`. They
are discovered when `bos.exts` is imported (the config default), so an **installed** package
extends BOS with no config edits.

```toml
[project]
name = "bos-weather-tools"
dependencies = ["bos-ai"]

# 1) Extensions: importing the target module runs its @ep_tool / @ep_channel / … decorators.
[project.entry-points."bos.exts"]
weather = "bos_weather_tools.tools"

# 2) Skills: the target is a package whose directory holds skill subdirs (each with SKILL.md).
[project.entry-points."bos.skills"]
weather = "bos_weather_tools.skills"

# 3) CLI commands: the target is a click.Command / click.Group added under `boscli`.
[project.entry-points."boscli.commands"]
weather = "bos_weather_tools.cli:commands"
```

Mechanics:

- **`bos.exts`** — `bos.exts._discover_entry_point_extensions()` iterates
  `entry_points(group="bos.exts")` and calls `ep.load()` on each; a failing one is logged
  and skipped. The loaded module's import side effects (decorators) do the registration.
- **`bos.skills`** — `fs_skill_loader._contributed_skill_dirs()` loads each entry point and
  collects its package directory; these slot in at the `__builtin__` position (§8).
- **`boscli.commands`** — `bos.cli.entry` (`_LazyGroup`) loads each entry point; the value
  must be a `click.Command`/`click.Group`. Group-vs-group collisions **merge** subcommands
  (built-in wins on inner collisions); a plugin colliding with a built-in non-group command
  is skipped with a warning.

**Zero-packaging alternative**: instead of an entry point, list the module in config:
`extensions = ["bos.exts", "your_package"]`, or drop `.py` files into `./extensions`. Run
through the project venv (`uv run boscli ...`) so the package is importable.

---

## 10. Bootstrap & discovery order

`Workspace.bootstrap_platform()` runs this exact sequence (`bos.config.workspace`):

1. **Env**: apply `[platform.envs]` → load `[platform.envfile]` (`override=True`).
2. **Extensions**: for each `[platform].extensions` entry, load as a path (scan/import dir)
   if it exists relative to `bos_dir`, else import as a module. `bos.exts` pulls in all
   built-ins and discovers the `bos.exts` entry-point group.
3. **`[exts]` defaults**: for each `[exts.<ep>.<impl>]`, `ExtensionPoint.lookup(ep)` then
   `update_defaults(impl, cfg)` (deep-merged into the extension's defaults).
4. **Agents**: resolve `[agents.*]` `_parent` inheritance, invoke every `@ep_agent` factory
   once, merge `[agent.defaults] → _parent chain → factory → [agents.<name>]`, and register
   each into `AgentRegistry`. The built-in `BOS` agent is among the factories (loaded via
   `bos.exts`); there is no separate fallback step.

Then the harness opens (`AgentHarness.__aenter__`): `bos.core.defaults` self-registers
built-in adapters, the `[harness]`-named services are instantiated, the `EventBus` +
`JobRunner` start, and `PluginServices` is assembled. Agents are built lazily by
`create_agent`.

---

## 11. The runtime: gateway, actors, channels, message flow

- **`boscli gateway start`** boots the gateway process: it builds the harness, registers an
  actor per `[runtime.actors.<name>]` (each an addressable `agent@<name>` mailbox bound to
  its agent kind), starts each configured `[[runtime.channels]]`, and serves an HTTP
  control plane on `[runtime.gateway].host:port`. `port = 0` auto-assigns; the actual
  port/PID is written to `gateway.state` so clients (TUI, `gateway status`, HTTP) can
  discover it. The control-plane API is authenticated by the key in the env var named by
  `api_key_env` (default `BOS_GATEWAY_API_KEY`).
- **Actors** are long-lived and restartable (`restart_on_error`, `max_restarts`). `main_actor`
  is the default route/mention target. Other actors are reachable by `@name` mentions
  (`actor_resolver.mention_prefix`, default `@`).
- **End-to-end flow**: external client → channel `run(mailbox)` posts an `Envelope` into the
  target actor's mailbox → the actor runs its agent turn (tools, plugins, LLM) → the reply
  is delivered back through the mailbox/`MailRoute` → the channel pushes it to the client.
- **`boscli ask`** bypasses the gateway: it builds the workspace + harness and runs one agent
  turn **in-process**, printing the final reply to stdout (progress streams to stderr on a
  TTY). It honors `--model` / `BOS_MODEL` per invocation.

---

## 12. CLI reference (`boscli`)

Global options: `-c/--config <path|preset>` (or `BOS_CONFIG`), `-l/--log-level <LEVEL>`
(or `BOS_LOG_LEVEL`, default `ERROR`). Commands are lazy-loaded; third parties add more via
`boscli.commands` (§9).

| Command | Purpose | Notable options |
| --- | --- | --- |
| `ask "<prompt>"` | One-shot, in-process agent turn. | `--stdin`, `--model`, `--agent <kind>`, `-w/--workspace` |
| `init` | Guided project scaffolding. | `--minimal` (emit commented template), `--name`, archetype (workspace/package), `--no-probe` |
| `gateway start` | Start the runtime. | (subgroup) |
| `gateway stop` / `status` / `restart` | Control a running gateway. | uses `gateway.state` |
| `tui` | Connect the terminal UI to a gateway. | |
| `doctor` | Health checks (config, paths, env, credentials). | |
| `inspect` | Introspect harness/config/runtime state. | (subcommands) |
| `memory` | Memory backend admin (list/show/etc.). | (subcommands) |

`boscli init` flow: prompt for purpose → pick **archetype** (`workspace` = plain project with
`./extensions`; `package` = installable `src/<pkg>/` whose extensions register via the
`bos.exts` entry point) → choose provider/model (detects API keys) → scaffold files →
optional credential probe (one LLM call unless `--no-probe`) → optional `git init`.

Useful env vars: `BOS_HOME` (default `~/.bos`), `BOS_CONFIG`, `BOS_MODEL`,
`BOS_CONSOLIDATOR_MODEL`, `BOS_GATEWAY_API_KEY`, `BOS_CAPABILITY_LIMIT` (max skills/subagents
listed in the prompt, default 50), `BOS_LOG_LEVEL`, plus provider `*_API_KEY`s.

---

## 13. Multi-agent patterns

- **Delegation (one inbox)**: keep a single `main` actor; bind `SubagentPlugin` with
  `enabled = ["researcher", "writer"]`. The main agent calls the `AskSubagent` tool to run a
  specialist on a self-contained brief and gets its result back. Subagents run with a fresh
  chat-id (no shared history).
- **Direct addressing (many inboxes)**: give a specialist its own actor
  (`[runtime.actors.researcher] agent = "researcher"`); users reach it with `@researcher`.
- Combine both: a specialist can be both an actor (directly addressable) and an allowed
  subagent of `main`. See the `team` preset (`presets/team.toml`).

---

## 14. Quick reference

**Config sections**: `[platform]` (env/discovery) · `[harness]` (service impls) ·
`[exts.<ep>.<impl>]` (extension config) · `[agent.defaults]` + `[agents.<name>]` (agents) ·
`[runtime]` / `[runtime.gateway]` / `[runtime.actors.<name>]` / `[[runtime.channels]]` (runtime).

**Decorators** (`from bos.core import …`): `ep_tool`, `ep_provider`, `ep_agent`,
`ep_chat_store`, `ep_consolidator`, `ep_turn_interceptor`, `ep_job_runner`, `ep_mail_route`,
`ep_channel`, `ep_plugin`.

**Entry-point groups**: `bos.exts` (extensions) · `bos.skills` (skills) · `boscli.commands`
(CLI).

**Default harness impls**: `LLMConsolidator`, `JsonlChatStore`, `JsonlMailRoute`,
`InProcJobRunner`. **Default plugins**: `MemoryPlugin`, `PlanPlugin`, `TaskPlugin`,
`SkillsPlugin`, `SubagentPlugin`. **Default agent kind**: `BOS`.

**Where to read the BOS source** (browse on GitHub at
`https://github.com/bos-agent/bos-ai/tree/main/<path>`, or open the installed package
locally — see *About this document* at the top):

| Concern | Module / path |
| --- | --- |
| Extension points | [`src/bos/core/registry.py`](https://github.com/bos-agent/bos-ai/blob/main/src/bos/core/registry.py), [`src/bos/core/contract.py`](https://github.com/bos-agent/bos-ai/blob/main/src/bos/core/contract.py) |
| Configuration | [`src/bos/config/`](https://github.com/bos-agent/bos-ai/tree/main/src/bos/config) (incl. [`template.toml`](https://github.com/bos-agent/bos-ai/blob/main/src/bos/config/template.toml)) |
| Built-in extensions | [`src/bos/extensions/`](https://github.com/bos-agent/bos-ai/tree/main/src/bos/extensions) |
| Plugins | [`src/bos/plugins/`](https://github.com/bos-agent/bos-ai/tree/main/src/bos/plugins) |
| Gateway / runtime | [`src/bos/gateway/`](https://github.com/bos-agent/bos-ai/tree/main/src/bos/gateway) |
| CLI | [`src/bos/cli/`](https://github.com/bos-agent/bos-ai/tree/main/src/bos/cli) |

For point-in-time design rationale, see the
[BEPs](https://github.com/bos-agent/bos-ai/tree/main/docs/BEP) in the repository.
