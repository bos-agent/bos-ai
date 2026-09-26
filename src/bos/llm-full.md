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

## 1. What BOS is, and the two shapes it runs in

An **agent** is an LLM-driven turn loop: a system prompt, a model, a set of
**tools**, a set of **plugins** (which contribute more tools + prompt sections +
interceptors), and config knobs. That is the same object in both shapes below.
An agent kind can instead be backed by an **external runtime** — Claude Code or
Codex — whose turn loop is the vendor's, behind the same `AgentPort` (§13).
Everything pluggable is a named **extension** registered at an **extension
point**. Cross-cutting services — chat persistence, memory consolidation,
background jobs, message routing — are owned by the **harness** and selected by
name. Configuration is one TOML file (`.bos/config.toml`) plus optional Python
extensions and Markdown/TOML agent files.

How that agent is *run* is a choice between two shapes:

**A — your code calls the agent.** Nothing long-lived: the agent is built in the
calling process and driven one turn at a time. From Python that is `bos.sdk`
(§3), which needs only the base `bos-ai` install; from a shell it is
`boscli ask` (§14).

**B — a gateway process hosts actors.** A long-lived process holds one or more
**actors** — named, addressable, restartable mailboxes, each bound to an agent
kind — and the **channels** that bridge external clients (TUI, Telegram, Lark,
HTTP) to them. Started with `boscli gateway start`, or mounted inside your own
ASGI app with `GatewayMount` (§12). Needs `bos-ai[gateway]`.

Vocabulary that applies in both shapes:

| Term | What it is | Defined in |
| --- | --- | --- |
| **Agent** | An LLM-driven turn loop with tools, plugins, a system prompt, a model | `bos.core.agent` |
| **Harness** | Lifecycle owner of shared services (chat store, consolidator, jobs, mail) | `bos.core.harness.AgentHarness` |
| **Extension Point** | A named registry of interchangeable implementations | `bos.core.registry.ExtensionPoint` |
| **Extension** | One registered implementation at an extension point | `@ep_tool`, `@ep_channel`, … |
| **Plugin** | A bundle that adds tools + prompt sections + interceptors to an agent | `ep_plugin`, `HarnessPlugin`/`AgentPlugin` |
| **Skill** | A Markdown playbook the agent can load on demand | `SKILL.md`, `SkillsPlugin` |
| **Tool** | An async function the LLM can call | `@ep_tool` |

Vocabulary that exists only in shape B (§12):

| Term | What it is | Defined in |
| --- | --- | --- |
| **Actor** | A named, addressable, restartable runtime instance bound to one agent kind | `[runtime.actors.<name>]` |
| **Gateway** | The process that hosts actors + channels + an HTTP control plane | `bos.gateway`, `boscli gateway` |
| **Channel** | Bridges an external client to an actor's mailbox | `ep_channel`, `[[runtime.channels]]` |

---

## 2. Install & extras

```bash
uvx boscli ...                # the CLI; `pip install bos-ai` is the LIBRARY and
                              # provides no `boscli` command (see §2.1)
```

### 2.1 Two distributions, and the extras

`bos-ai` is the **library**; it ships no console script. `boscli` is a code-free
distribution that exists so `uvx boscli` resolves (tool runners assume the PyPI
name matches the command); it depends on `bos-ai[cli,litellm,search]` and points
its `boscli` entry point at `bos.cli.entry:main`. A `bos-ai[cli]` install without
the shim runs the same CLI as `python -m bos.cli`.

| Install | Adds |
|---|---|
| `bos-ai` | The library: `bos.core`, `bos.config`, plugins. ~14 MB |
| `bos-ai[litellm]` | The built-in LLM provider. Without it, calls fail with a message naming this extra; register your own with `@ep_provider` instead (§7.2) |
| `bos-ai[gateway]` | `starlette`+`uvicorn`+`httpx`+`websockets`: the gateway's ASGI app, the standalone process, and the Telegram/Lark channels |
| `bos-ai[search]` | `ddgs` + `beautifulsoup4`: the built-in web-search and page-fetch tools. The Tavily provider needs only an API key, not this extra |
| `bos-ai[lark]` | The Lark/Feishu SDK |
| `bos-ai[cli]` | The CLI's dependencies (implies `gateway`) |
| `bos-ai[codex]` | `openai-codex`, which ships the `codex` binary via `openai-codex-cli-bin`, plus `mcp` 2.x for the MCP egress (implies `gateway`): the Codex external agent runtime (BEP 19) |
| `bos-ai[claude-code]` | `claude-agent-sdk`, whose wheel bundles the `claude` CLI (~230 MB installed), plus `mcp` 2.x for the MCP egress (implies `gateway`): the Claude Code external agent runtime (BEP 19) |
| `bos-ai[all]` | Every extra above except `codex` and `claude-code`: each bundles a vendor CLI binary, which `all` does not pull in |

Built-in adapters whose extra is absent are **skipped with a warning naming the
extra**, not an import error — so `import bos.exts` succeeds on any install. A
config that then names a tool from a skipped module fails at resolution.

### 2.2 Quick start

**Shape A — call the agent.** No project, no process:

```bash
OPENAI_API_KEY=<key> boscli ask "how are you" --model openai/gpt-4o
```

From Python the same shape is `bos.sdk` — see §3.

**Shape B — run a gateway.** A real project:

```bash
mkdir my-agent && cd my-agent
boscli init            # guided setup: purpose, archetype, provider/model
boscli gateway start   # start the runtime (hosts actors + channels)
boscli tui             # connect the terminal UI
```

The model string is LiteLLM-style `provider/model` (e.g. `openai/gpt-4o`,
`gemini/gemini-2.5-flash`, `anthropic/claude-...`, `deepseek/deepseek-...`). The
provider prefix also selects a custom `@ep_provider` if one is registered under that
name (see §7.2); otherwise it falls back to LiteLLM, which reads the matching
`*_API_KEY` env var.

---

## 3. Calling an agent from your code

`Workspace` accepts a plain dict, so configuration can come from anywhere; the
file-discovery constructor is the CLI's convenience, not the only entry point.
There are **two** embedding shapes, and they are a choice made up front — not a
simple version and an advanced one:

| | Mode 1 — call the agent | Mode 2 — mount the gateway |
|---|---|---|
| Entry point | `bos.sdk`: `BosApp`, `open_harness` | `bos.runner.GatewayMount` |
| Per turn | your code calls `agent.run(chat_id, text)` | the gateway's actors/channels drive it |
| Gives you | an `AgentPort` (a BOS `Agent`, or an external runtime — §13) | actors, channels, chat coordination, WS protocol |
| Install | base `bos-ai` | `bos-ai[gateway]` |
| Writable `bos_dir` | only for file-backed stores | always (`<bos_dir>/run/`: lock, state, cursors) |
| Example | `examples/embed_sdk.py` | `examples/embed_gateway_fastapi.py` |

**Mode 1** — `bos.sdk` holds the bootstrap sequence and the object over it:

```python
from bos.sdk import BosApp

async with BosApp(config_dict, bos_dir="/var/lib/myapp/.bos") as app:
    agent = app.agent()                      # cached; no arg → resolve_default_agent()
    result = await agent.run("chat-42", "hello")   # result.output
```

- `agent(kind=None)` is **sync** and returns a cached `AgentPort` — BOS's own
  `Agent`, or an external runtime (§13), which implements the port without being an
  `Agent`; every kind in `config.agents` is built during `__aenter__`. A kind only an
  `@ep_agent` factory registers — or a bare reserved runtime kind (§13.2) — needs
  `await app.build_agent(kind, agent_cfg=None)`; `agent_cfg` is the top config layer
  for that first build, and a kind already cached refuses one.
- There is **no `BosApp.ask()`** and no wrapper over `Agent.run()` (BEP 18
  §2.2.1): `run()` has ten parameters, so a façade would either mirror them
  forever or push callers down a layer. `app.harness` / `app.workspace` expose
  that lower layer as the *same* objects.
- Chat continuity is one `chat_id`, not a session object.
- **One live `BosApp` per process.** A second raises: `bootstrap_platform()`
  writes `os.environ` and rebuilds `AgentRegistry`, both process-global.
- **The two modes do not co-exist in one process** — mount a gateway *or* hold a
  `BosApp`, not both. Unlike the rule above this one **is not enforced**: nothing
  raises, because `GatewayMount` never touches `BosApp`'s guard. It bootstraps
  the same process-global registry on mount and on every `POST /api/restart`, so
  each side silently rebuilds the other's agents; already-built `Agent`s keep
  working, but any later `create_agent` — a restart, a new actor,
  `build_agent()` — resolves against the wrong workspace (`docs/BACKLOG.md` §4).
- `open_harness(workspace)` is the same bootstrap with nothing on top. Its order
  is the contract — `resolve_agents()` **then** `bootstrap_platform()`; reversed,
  every agent file is dropped with no error.

**Mode 2** — `GatewayMount(workspace_factory, *, public_base_url=None)`: the host
calls `mount.start()`/`mount.stop()` in its own lifespan and mounts
`mount.build_app()` (a Starlette app) once, before `start()`. The factory is a
callable because a restart re-reads config. Routes under the mount path:
`GET /api/status`, `GET /api/actors`, `POST /api/restart`, the upload endpoints,
`WS /ws`. BOS performs no authentication (BEP 17 §3.8); the host fronts it.

Set `[platform] extensions = []` and import only the adapters you want, instead
of `bos.exts` which loads every built-in. The contract is **`bos.sdk.__all__`**
(39 names): `BosApp`, `open_harness`, the agent surface (`AgentPort`, `Agent`, …), the ports plus every
type their own method signatures use, the nine `ep_*` points, and `Workspace` /
`RootConfig` / `validate_config`. A test enforces that every promised port is
implementable from promised names alone. `bos.sdk` re-exports rather than
redefines, so `bos.sdk.Agent is bos.core.Agent`. Everything else — including the
`_`-prefixed re-exports for extension authors, and `GatewayMount` itself, which
needs the `[gateway]` extra `bos.sdk` must not require — stays importable and is
explicitly unstable.

---

## 4. Configuration

All configuration is validated by Pydantic models in `bos.config.schema`.
The root is `RootConfig` with top-level sections `[platform]`, `[harness]`, `[exts]`,
`[agent]`, `[agents.*]`, `default_agent`, `[runtime]`. The canonical, fully-commented template is
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
  `bos_dir = ~/.bos/presets/<name>` (created on demand). The only built-in preset is
  `default`. (`resolve_config_source`, `presets_dir`.)

### 4.2 `default_agent` — which agent runs when none is named

A top-level key, not a section:

```toml
default_agent = "main"
```

It selects the agent kind a caller gets when it names none: a bare `boscli ask`
(§14) and `app.agent()` in `bos.sdk` (§3) both resolve through it. The chain
(`bos.config.workspace.Workspace.resolve_default_agent`):

1. **The key, if set.** It must name an `[agents.<name>]` entry or a kind some
   extension registered (`@ep_agent`); anything else raises, listing the agent
   kinds that are available.
2. Otherwise **the only `[agents.*]` entry**, if there is exactly one.
3. Otherwise **one named `main`**, if `[agents.main]` exists.
4. Otherwise an error listing the `[agents.*]` names it found, telling the caller
   to set `default_agent` or name an agent explicitly.

Steps 2–4 read only `[agents.*]` (inline or loaded from `agent_dirs`, §4.7), not
the registry — so a workspace whose only agent comes from an `@ep_agent` factory
has to name it, via this key or per call.

The chain deliberately never consults `[runtime].actors`. That table answers the
gateway's different question — which addressable runtime instances to run — and
routing through it is what made a project with no gateway read an error about
one (BEP 18 §3.5).

### 4.3 `[platform]` — environment & discovery

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
  built-ins + discovers entry points (§10).
- **`agent_dirs`** entries are scanned for `*.toml` and `*.md` files; each becomes a named
  agent (§4.7).

### 4.4 `[harness]` — select service implementations

Each key names a registered extension by name (`extra='forbid'` — unknown keys error).

```toml
[harness]
consolidator = "LLMConsolidator"   # ep_consolidator
chat_store   = "JsonlChatStore"    # ep_chat_store
mail_route   = "JsonlMailRoute"    # ep_mail_route
interceptors = []                  # ordered list of ep_turn_interceptor names/configs
```

Defaults shown are the `HarnessConfig` defaults. The harness instantiates each by name
at startup (`bos.core.harness.AgentHarness.__aenter__`). Built-in adapters are
registered by `bos.core.defaults` (imported at harness open time), so
these names resolve even without `bos.exts`.

There is **no provider key in `[harness]`** — provider selection happens per-model-string
via `ep_provider` (§7.2).

### 4.5 `[exts.<ep_name>.<impl_name>]` — configure extensions

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

### 4.6 `[agent.defaults]` and `[agents.<name>]` — agents

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
| `_parent` | `str` | Inherit from another agent — `[agents.*]`, an `@ep_agent` factory, or a reserved runtime (`claude-code`, `codex`, §13) — deep-merged underneath; see §4.8. |

**Model precedence** (see `bos.core.llm.LLMClient`): `[agents.<name>].model` → `[agent.defaults].model`
→ `BOS_MODEL` env → `[exts.ep_provider.<provider>].model`.

**Tool resolution**: an agent sees a merged, filtered view over `[agent-local plugin tools,
global ep_tool registry]` (local wins on name clash). `enabled` is an include list
(`"*"` / `None` = all), `disabled` is an exclude list. (`bos.core.harness`,
`ResolvedToolSet` / `create_agent`.) An explicit `enabled = []` is an *empty* include list —
no tools at all, and likewise no plugins under `[…plugins]`. That is not the same as omitting
the key, which inherits `[agent.defaults]`; see §4.8.

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

An agent backed by an external runtime — a reserved kind (`claude-code`, `codex`), or one
whose inheritance-resolved spec carries `external_runtime` — starts from `{}` instead of
`[agent.defaults]` (§13.2).

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
the global floor under the chain, except for an externally-backed agent (above). `_parent` may reference
another `[agents.*]` agent (inline or external file), an `@ep_agent` factory agent (e.g. `BOS`; its spec
is passed to the resolver as `factory_specs`), or a reserved runtime kind, supplied the same way from
`_EXTERNAL_RUNTIME_SPECS` (§13.2) — not `[agent.defaults]`. The parent's `agent_name` is not inherited.
A cycle or unknown parent raises at bootstrap. The directive is stripped before registration and never reaches the `Agent` constructor.
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

### 4.9 `[runtime]` — the gateway

`[runtime]`, with `[runtime.gateway]`, `[runtime.actor_resolver]`,
`[runtime.actors.<name>]` and `[[runtime.channels]]`, configures shape B only.
It is documented in full in §12.1. The one in-process path that reads it is
`boscli ask --actor <name>` (§14).

---

## 5. Project layout & home directories

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

## 6. Extension points (the registry model)

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

### 6.1 The core extension points (`bos.core.contract`)

| Extension point | Kind | Contract / returns |
| --- | --- | --- |
| `ep_tool` | function | Async tool the LLM can call; `parameters` JSON-schema required. |
| `ep_provider` | function | `async (messages, **kwargs) -> LLMResponse`; selected by model prefix. |
| `ep_agent` | factory | Sync/async fn returning an agent-spec dict (validatable as `AgentConfig`). |
| `ep_chat_store` | factory/class | Builds a `ChatStore` (persistence + context assembly). |
| `ep_consolidator` | factory | Builds a `Consolidator` (summarization/memory). |
| `ep_turn_interceptor` | factory | Builds a `TurnInterceptor` (per-turn hooks). |
| `ep_mail_route` | factory | Builds a `MailRoute` (`bind(address)->MailBox`, `deliver(env)`). |
| `ep_channel` | factory/class | Builds a `Channel` (bridges clients to a mailbox). |
| `ep_plugin` | class/factory | A `HarnessPlugin` (adds tools/prompt/interceptors). |

Plugin-defined example: `pep_skills_loader` (in `bos.plugins.skills.plugin`).

---

## 7. Writing extensions

All extension authoring is "import a decorator from `bos.core`, decorate a function or
class." Discovery happens because the module is imported — via `[platform].extensions`
(a path dir scanned, or a module name imported) or via the `bos.exts` entry point (§10).

### 7.1 Tools — `@ep_tool`

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

### 7.2 Providers — `@ep_provider`

A provider is `async (messages, **kwargs) -> LLMResponse`. The LLM client
(`bos.core.llm.LLMClient`) **dispatches by model prefix**:

```python
# bos.core.llm.LLMClient — simplified
model = kwargs.get("model") or os.getenv("BOS_MODEL")
provider_name, sep, model_name = model.partition("/")     # "myco/m-1" -> ("myco","m-1")
if not sep or not ep_provider.has(provider_name):
    provider_name, model_name = "litellm", model           # fall back, keep full string
return await ep_provider.invoke(provider_name, kwargs | {"messages": messages, "model": model_name})
```

So: register `@ep_provider(name="myco")` and any agent whose `model = "myco/..."` routes
to it. (A provider is a model backend for BOS's own turn loop; running Claude Code or Codex
as the agent is a different mechanism — §13.) If the prefix is not a registered provider
(e.g. `openai/gpt-4o`), it falls back to the built-in `litellm` provider with the full model
string. Provider defaults come from
`[exts.ep_provider.<name>]`. The only built-in provider is `litellm`, which is also the
default fallback (registered by `bos.core.defaults`); it reaches every provider LiteLLM
supports, so a custom `@ep_provider` is only needed for non-LiteLLM backends.

### 7.3 Chat stores — `@ep_chat_store`

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

### 7.4 Consolidators, mail routes, job runners, interceptors

- **`@ep_consolidator`** → a `Consolidator` (summarizes history / drives memory). The
  harness builds it with `{model: BOS_CONSOLIDATOR_MODEL, llm}`. Default `LLMConsolidator`;
  config via `[exts.ep_consolidator.LLMConsolidator]` (e.g. `model`).
- **`@ep_mail_route`** → a `MailRoute`: `bind(address) -> MailBox` and `async deliver(env)`.
  Default `JsonlMailRoute`. This is point-to-point message routing between actors/channels.
- **`@ep_turn_interceptor`** → a `TurnInterceptor` with `async intercept(stage, context)`.
  Configure an ordered chain via `[harness].interceptors` (list of names or `{name=…, …}`
  tables). Plugin interceptors run best-effort first, then the configured chain
  (`_CompositePluginInterceptor`). Raise `AbortTurn` to stop a turn.

### 7.5 Channels — `@ep_channel`

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

### 7.6 Agent factories — `@ep_agent`

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

## 8. Plugins

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

### 8.1 Lifecycle (who calls what, when)

From `bos.core.harness` (`_bind_plugins_for_agent` / `_instantiate_and_setup_plugin` /
`create_agent`):

1. When an agent is created (a BOS `Agent`; an external runtime binds no plugins, §13), the
   harness computes its enabled plugin set:
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

### 8.2 Minimal plugin template

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

### 8.3 Plugins can define their own extension points (`pep_`)

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

### 8.4 Built-in plugins

Registered when `bos.exts` is loaded; the default agent enables
`MemoryPlugin, PlanPlugin, TaskPlugin, SkillsPlugin, SubagentPlugin`.

| Plugin | Adds | Key config (`plugin-bindings.<Plugin>`) |
| --- | --- | --- |
| `MemoryPlugin` | Persistent memory + recall tools; consolidation via `boscli memory consolidate` | `maxims` (categories, default `["user","self","rules"]`), `consolidation.model`. No `scope` key — memory is isolated per agent identity (passing `scope` raises). |
| `PlanPlugin` | Planning tool(s) and prompt section | — |
| `TaskPlugin` | In-conversation task list: `TaskCreate`/`TaskUpdate`/`TaskList`/`TaskGet` | — |
| `SkillsPlugin` | `LoadSkill` tool + skill discovery (§9) | `skill_dirs`, `allow`, `exclude`, `loader`, `preload` |
| `SubagentPlugin` | `AskSubagent` tool to delegate to named agents | `enabled` (list/`"*"`), `disabled`, `task_template` |

`SubagentPlugin.enabled` is the allow-list of agent kinds the agent may delegate to; `"*"`
means all registered agents (no implicit exclusions — including the agent itself if it is
registered, so use explicit allow/deny lists to shape topology). It requires
`services.agent_runner` (raises in `setup` if absent). The plugin's own default allow-list
is empty (`[]`) — enabling the plugin does not by itself register `AskSubagent`; a binding
is required. The built-in `BOS` agent ships `plugin-bindings.SubagentPlugin.enabled =
["*"]` by default, and scaffolded projects ship the same binding on `[agents.main]`.

---

## 9. Skills

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
- Ship skills from a package via the `bos.skills` entry point (§10).

---

## 10. Python packaging & entry points

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
  collects its package directory; these slot in at the `__builtin__` position (§9).
- **`boscli.commands`** — `bos.cli.entry` (`_LazyGroup`) loads each entry point; the value
  must be a `click.Command`/`click.Group`. Group-vs-group collisions **merge** subcommands
  (built-in wins on inner collisions); a plugin colliding with a built-in non-group command
  is skipped with a warning.

**Zero-packaging alternative**: instead of an entry point, list the module in config:
`extensions = ["bos.exts", "your_package"]`, or drop `.py` files into `./extensions`. Run
through the project venv (`uv run boscli ...`) so the package is importable.

---

## 11. Bootstrap & discovery order

`Workspace.bootstrap_platform()` runs this exact sequence (`bos.config.workspace`):

1. **Env**: apply `[platform.envs]` → load `[platform.envfile]` (`override=True`).
2. **Extensions**: for each `[platform].extensions` entry, load as a path (scan/import dir)
   if it exists relative to `bos_dir`, else import as a module. `bos.exts` pulls in all
   built-ins and discovers the `bos.exts` entry-point group.
3. **`[exts]` defaults**: for each `[exts.<ep>.<impl>]`, `ExtensionPoint.lookup(ep)` then
   `update_defaults(impl, cfg)` (deep-merged into the extension's defaults).
4. **Agents**: resolve `[agents.*]` `_parent` inheritance, invoke every `@ep_agent` factory
   once, merge `[agent.defaults] → _parent chain → factory → [agents.<name>]` (`{}` in place
   of `[agent.defaults]` for an externally-backed agent, §13.2), and register each into
   `AgentRegistry`. The built-in `BOS` agent is among the factories (loaded via
   `bos.exts`); there is no separate fallback step.

Then the harness opens (`AgentHarness.__aenter__`): `bos.core.defaults` self-registers
built-in adapters, the `[harness]`-named services and the `EventBus` are
instantiated, and `PluginServices` is assembled. Agents are built lazily by
`create_agent`.

---

## 12. The gateway: actors, channels, message flow

Shape B. The gateway is a process that hosts one or more **actors**; each actor
is a long-lived, addressable mailbox bound to an **agent**. **Channels** bridge
the outside world (TUI, Telegram, Lark, HTTP) to an actor's mailbox. Run it with
`boscli gateway start`, or mount it in your own ASGI app with `GatewayMount`
(§3).

```
                 ┌─────────────────────────── gateway process ───────────────────────────┐
 external client │   channel ──▶ mailbox ──▶ actor ──▶ agent (LLM loop) ──▶ tools/plugins  │
 (TUI/Telegram)  │     ▲                        │              │                           │
                 │     └──────── reply ─────────┘        harness services:                 │
                 │                                       chat_store, consolidator,         │
                 │                                       mail_route, events                │
                 └────────────────────────────────────────────────────────────────────────┘
```

- **`boscli gateway start`** boots the gateway process: it builds the harness, registers an
  actor per `[runtime.actors.<name>]` (each an addressable `agent@<name>` mailbox bound to
  its agent kind), starts each configured `[[runtime.channels]]`, and serves an HTTP
  control plane on `[runtime.gateway].host:port`. `port = 0` auto-assigns; the actual
  port/PID is written to `gateway.state` so clients (TUI, `gateway status`, HTTP) can
  discover it. BOS performs no authentication of its own: the default bind is loopback,
  and an operator who binds elsewhere fronts it with their own auth.
- **Actors** are long-lived and restartable (`restart_on_error`, `max_restarts`). `main_actor`
  is the default route/mention target. Other actors are reachable by `@name` mentions
  (`actor_resolver.mention_prefix`, default `@`).
- **End-to-end flow**: external client → channel `run(mailbox)` posts an `Envelope` into the
  target actor's mailbox → the actor runs its agent turn (tools, plugins, LLM) → the reply
  is delivered back through the mailbox/`MailRoute` → the channel pushes it to the client.

### 12.1 `[runtime]` — actors, gateway, channels

```toml
[runtime]
main_actor = "main"            # which actor is the default mention/route target

[runtime.gateway]
host = "127.0.0.1"
port = 0                       # 0 = auto-assign a free port (discover via gateway.state)
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

Rules validated at config resolution (`bos.config.workspace`, the `resolve_gateway_*`
methods):

- Actor names must match `[A-Za-z_][A-Za-z0-9_-]*` (mention-safe). The TOML key **is** the
  actor's identity and memory scope.
- `runtime.main_actor` must exist in `runtime.actors`.
- Each channel needs a unique `channel_id`; `target_actor` must be a defined actor; `type`
  may not be `HttpChannel`, which is gateway infrastructure.
- An actor address is `agent@<name>`; a channel address is `channel@<channel_id>`.

That `type` names a **registered** `ep_channel` is checked later and elsewhere — at channel
start, by `ep_channel.get(cfg.type)` in `bos.gateway.channels.channel_manager`. A typo in
`type` therefore survives config validation and fails when the gateway starts the channel.

> Migration note: `[main]` was removed; use `[runtime]` + `[runtime.actors]`.
> `runtime.agent` / `runtime.default_actor` are removed (use `runtime.actors` /
> `runtime.main_actor`). (`bos.config.schema.validate_config`.)

### 12.2 Multi-agent patterns

- **Delegation (one inbox)**: keep a single `main` actor; bind `SubagentPlugin` with
  `enabled = ["researcher", "writer"]`. The main agent calls the `AskSubagent` tool to run a
  specialist on a self-contained brief and gets its result back. Subagents run with a fresh
  chat-id (no shared history).
- **Direct addressing (many inboxes)**: give a specialist its own actor
  (`[runtime.actors.researcher] agent = "researcher"`); users reach it with `@researcher`.
- Combine both: a specialist can be both an actor (directly addressable) and an allowed
  subagent of `main` — a `[runtime.actors.<name>]` entry plus a slot in
  `SubagentPlugin.enabled`.

---

## 13. External agent runtimes — Claude Code & Codex

An agent kind can be backed by a vendor's own agent harness instead of BOS's turn loop:
**Claude Code** (`claude-agent-sdk` 0.2.159, driving the `claude` CLI 2.1.281 it bundles)
or **Codex** (`openai-codex` 0.156.1, driving `codex app-server`). Each implements
`AgentPort` (`bos.core.agent.contract`: `name`, `request_stop`, `ask`, `run`) plus the
harness-side `ExternalRuntime` (`aclose`, `resolved_config`), without being an `Agent`: no
`LLM`, no plugins, no BOS tools, no interceptors, no consolidator or compaction. It works
wherever an agent kind does — `boscli ask --agent`, `[runtime.actors.*]`, `AskSubagent`,
`BosApp.agent()`. Classes: `bos.extensions.runtimes.claude_code.ClaudeCodeAgent`,
`bos.extensions.runtimes.codex.CodexAgent`; shared config/session/persistence helpers in
`bos.extensions.runtimes._shared`. Design: BEP 19.

### 13.1 Install & runtime shape

| | Claude Code | Codex |
|---|---|---|
| Extra (not in `all`) | `bos-ai[claude-code]` | `bos-ai[codex]` |
| Vendor process | one `claude` CLI child **per turn** (`ClaudeSDKClient`, `resume=`), disconnected when the turn ends | one `codex app-server` child **per agent**, spawned lazily by `_ensure_client` on the first turn (or first `native_messages`), closed by `aclose()` at harness exit |
| Login | the OS user's Claude Code login (`claude` → `/login`, or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`) | `codex login` |

Both extras add `mcp>=2,<3` and `bos-ai[gateway]` (§13.8). Dispatch is
`bos.core.harness.EXTERNAL_AGENT_KINDS` (`"claude-code"`, `"codex"` → dotted class paths),
imported by `_load_external_runtime` on first build; a missing extra raises *"The 'codex'
agent runtime needs its optional dependency. Install it with: pip install
'bos-ai[codex]'"*. `AgentHarness.create_agent` branches **before** plugin binding when
`kind` is reserved or the merged config carries `external_runtime`, builds the runtime with
the harness's `chat_store`, `workspace`, the MCP accessor and the shared
`StructuredValidator`, and appends it to `_owned` (closed on harness exit).

### 13.2 Declaring one

`_parent = "claude-code"` / `"codex"` in `[agents.<name>]` or an agent file makes a named
instance: `bos.config.workspace` passes `_EXTERNAL_RUNTIME_SPECS`
(`{"codex": {"external_runtime": "codex"}, …}`) to `_resolve_agent_inheritance` as
pseudo-factory parents, so the child inherits `external_runtime`, and `kind`/`name` stay the
child's own (`george` reports `george`).

```markdown
---
_parent: codex
permission: workspace-write
cwd: services/api
mcp_tools: [CreateTicket]
---
You are George, the implementer for the payments service.
```

- `[agents.codex]` / `[agents.claude-code]` define an agent named after the runtime, whose
  spec every `_parent = "<runtime>"` child also inherits. Without such a table the reserved
  kinds are **not registered** (no phantom agent in `AgentRegistry`): `boscli ask --agent
  codex` and `app.agent("codex")` fail as unknown; build them with
  `await app.build_agent("codex", agent_cfg={...})` or `harness.create_agent("codex", {...})`.
- `agent_cfg` may carry `_parent` (a reserved runtime or any registered agent), resolved by
  `bos.core.harness._resolve_agent_cfg_parent`; refused for an already-registered `kind`, and
  when its runtime contradicts `kind` or an `external_runtime` in the same `agent_cfg`.
  `_parent` in `[runtime.actors.*].agent_cfg` is refused at config load.
- A hand-written `external_runtime` in `[agents.*]` is rejected at bootstrap (use `_parent`).
- **`[agent.defaults]` is not merged** into an externally-backed agent: the registration loop
  in `Workspace.bootstrap_platform` starts it from `{}` (reserved name, or resolved
  `external_runtime`).
- Precedence, low → high: the reserved kind → `[agents.<runtime>]` → the agent's own spec →
  `[runtime.actors.<a>].agent_cfg` → the `agent_cfg` passed to `build_agent`/`create_agent`.
- Config is validated at construction — `BosApp.__aenter__` for kinds in `config.agents`, an
  actor's start, `boscli ask`, `build_agent` — and construction never starts a vendor
  process.

### 13.3 Config keys

Validated strictly by `_shared.parse_external_config` (unknown key → `ValueError` naming it
and the known set).

| Key | Runtime | Default | Meaning |
|---|---|---|---|
| `permission` | both | **required** | `read-only` \| `workspace-write` \| `full-access` (§13.4). |
| `cwd` | both | `"."` | str, resolved against the harness workspace root (a project's root, the parent of `.bos/`; `"."` — the process cwd — for `BosApp(dict, bos_dir=…)`), symlinks resolved; must stay inside it. The confinement root. |
| `system_prompt` | both | — | **Appended** to the runtime's prompt: Claude Code `{"type": "preset", "preset": "claude_code", "append": …}`; Codex `developer_instructions`. An agent file's body. |
| `base_instructions` | both | — | **Replaces** it (Claude Code: plain-str `system_prompt`; Codex: `base_instructions`). Mutually exclusive with `system_prompt`. |
| `model` | both | runtime default | Native model name, verbatim. Per turn, `llm_args["model"]` overrides it (`boscli ask --model`; `BOS_MODEL` does not reach it). |
| `auth` | both | `"subscription"` | or `"api_key"` (§13.5). |
| `timeout_seconds` | both | `None` | Number; bounds each attempt (§13.9). |
| `mcp_tools` | both | `[]` | List of `ep_tool` names exposed over MCP (§13.8); `"*"` and a bare string refused. |
| `native_options` | both | `{}` | Vendor settings (below). |
| `setting_sources` | Claude Code | `[]` | List from `user`/`project`/`local`: which CLI settings files load. `project`/`local` log a WARNING (repo hooks, `apiKeyHelper` run on the host outside sandbox and `permission`). Unknown to Codex. |
| `max_iterations` | Claude Code | `None` | Positive int → the CLI's `max_turns`; spending it closes the turn with `MAX_ITERATION_CONTENT`, committed. Dropped for Codex. |

- **`native_options`, Claude Code** — an allowlist over `ClaudeAgentOptions` fields
  (`claude_code._ALLOWED`): `fallback_model`, `max_budget_usd`, `betas`, `thinking`,
  `max_thinking_tokens`, `task_budget`. Every other field is BOS-owned (`_BOS_OWNED`:
  `permission_mode`, `sandbox`, `settings`, `setting_sources`, `hooks`, `can_use_tool`,
  `tools`, `mcp_servers`, `strict_mcp_config`, `env`, `resume`, `extra_args`, …) or refused
  (`_REFUSED`: `cli_path`, `add_dirs`, `plugins`, `agents`, `user`, session fields, …); a
  test partitions `dataclasses.fields(ClaudeAgentOptions)` into the three sets. Refused at
  construction, each key with its reason.
- **`native_options`, Codex** — keywords to `thread_start`/`thread_resume`; a `config`
  sub-table is merged into the `config=` override (itself merged over `~/.codex/config.toml`).
  Refused (`codex._RESERVED_NATIVE_OPTIONS`): `sandbox`, `approval_mode`, `cwd`, `model`,
  `developer_instructions`, `base_instructions`, `config.mcp_servers`,
  `config.sandbox_workspace_write` (nested or dotted spelling). A denylist found by probing,
  not provably complete; an unknown keyword surfaces as the vendor's `TypeError` on the
  first turn.
- **Dropped** with one DEBUG line (`_shared._DROPPED_KEYS`): `tools`, `exclude_tools`,
  `tools_usage`, `plugins`, `plugin-bindings`, `max_tokens`, `max_iteration_handoff`,
  `shutdown_handoff`, `tool_noise_filter`, `history_attribution`, `reasoning_effort` (the
  config key; `llm_args["reasoning_effort"]` is forwarded), `description`, `agent_name`,
  `kind`, `name`, and `max_iterations` for Codex. (`description` still reaches
  `AgentRegistry.describe()`, which `AskSubagent` lists.)

### 13.4 `permission`

Bounds the filesystem, **not** the tools: every `mcp_tools` entry is callable at every level
(§13.8). No approval ever waits on a person.

**Claude Code** — BOS builds the confinement (`permission_mode` is only an approval policy):
a `tools=` allowlist per level (`claude_code._TOOL_LEVELS`; an unoffered tool is not
callable), a `PreToolUse` hook on every call (`ClaudeCodeAgent._hook`: denies tools outside
the allowlist; below `full-access`, resolves each file-tool path — `Read`/`Write`/`Edit`
`file_path`, `NotebookEdit` `notebook_path` — against `cwd` and denies it outside `cwd`,
inside the CLI config dir `CLAUDE_CONFIG_DIR` or `~/.claude`, or under `<cwd>/.claude` when
`setting_sources` opts into repo settings), a `can_use_tool` that answers by policy, and the
CLI's bash sandbox.

| `permission` | `permission_mode` | Built-in tools offered | Bash |
|---|---|---|---|
| `read-only` | `default` | `Read` | not offered |
| `workspace-write` | `acceptEdits` | `Read`, `Write`, `Edit`, `NotebookEdit`, `Bash` | sandbox `{"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True}`: writes confined to `cwd` and a per-turn `TMPDIR` (removed after the turn); `dangerouslyDisableSandbox` still runs sandboxed |
| `full-access` | `bypassPermissions` | all except `ListAgents`, `SendMessage`, `AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode` | no sandbox; file tools unconfined; `WebFetch`/`WebSearch` offered only here |

`workspace-write` is refused at construction where the sandbox cannot run
(`_bash_sandbox_unavailable`): Linux without `bwrap`/`socat` on `PATH`, macOS without
`/usr/bin/sandbox-exec`, always on Windows. A "Sandbox disabled" line on the CLI's stderr
ends the turn (`_SandboxDisabledError`). **Unconfined**: bash *reads* at `workspace-write`;
an `@path` in the prompt, which the CLI reads itself (outside `cwd`, any level); `full-access`.
Concurrent `workspace-write` agents sharing a `cwd` can rarely fail (never escape) a
sandboxed command on shared mount points under `<cwd>/.claude/` — give each its own `cwd`.
The CLI inherits BOS's `os.environ`; `_INHERITED_ENV_OVERRIDES` switches off variables that
would load plugins, hooks, settings, MCP servers or skills, and auto-memory.

**Codex** — `permission` picks Codex's OS sandbox (`codex._SANDBOXES`: `read-only`,
`workspace-write`, `danger-full-access`), which is the whole boundary; every level runs
`ApprovalMode.deny_all` (approval policy `never`, no reviewer), so escalations are refused
by Codex itself, and `_deny_approval` (installed over the SDK's auto-accepting handler)
refuses any request that reaches BOS. Reads are unconfined at every level;
`workspace-write` can also write `/tmp` and `$TMPDIR` (Codex's default). Codex reads the
operator's `~/.codex/config.toml`.

### 13.5 Auth

`auth = "subscription"` (default) = the runtime's existing login; `"api_key"` skips BOS's
check. BOS stores no token.

- **Claude Code, at construction** (reads the environment, spawns nothing): refuses
  `subscription` while any of `_SUBSCRIPTION_BYPASS_VARS` is non-empty — `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `ANTHROPIC_PROFILE`,
  `ANTHROPIC_CONFIG_DIR`, `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`,
  `ANTHROPIC_UNIX_SOCKET`, `CLAUDE_CODE_SIMPLE`, `CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY,
  ANTHROPIC_AWS,ANTHROPIC_GOOGLE_CLOUD,MANTLE,GATEWAY}` — or while
  `/home/claude/.claude/remote/.api_key` exists. `CLAUDE_CODE_OAUTH_TOKEN` is allowed.
  `ANTHROPIC_BASE_URL` is refused by host (`_base_url_route`): the CLI keeps the login and
  sends it to whatever host that names (observed live), so `subscription` needs the host
  to be `api.anthropic.com` (the CLI's own first-party check: `new URL(v).host`, default
  port dropped) or the value empty; a value with no readable http(s) host is refused too.
  The message names the host, never the URL. A proxy takes `auth = "api_key"` plus its own
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (with neither, the CLI still sends the login).
  `[platform].envfile`/`envs` count (they write `os.environ`). No login check exists: a
  missing login fails the first turn in the CLI. Any Claude Code agent is also refused
  while `managed-mcp.json` exists (`/etc/claude-code/` on Linux): the CLI would refuse the
  `--strict-mcp-config` BOS always sends.
- **Codex, on the first turn** (or first `native_messages`): `_preflight_auth` calls
  `account()` (bounded 30 s) once per agent; no account → *"auth="subscription" but no Codex
  account is logged in. Run `codex login`, or set auth="api_key" to opt out of this check."*

### 13.6 Sessions, persistence, `get_messages`

- **One native session per `chat_id`** (Claude session / Codex thread). The id is written into
  the metadata of the assistant message BOS commits (`external_runtime`, `native_session_id`,
  `native_turn_id`, `usage`; `_shared.commit_external_turn`) and read back by scanning the
  chat store newest-first (`_shared.read_native_session_id`, `active_only=False`), so it
  survives restarts. An unresumable session raises (*"… could not be resumed, and BOS does not
  silently start a fresh session under the same chat_id"*). A chat whose newest external turn
  names another runtime gets a new session with a WARNING; switching back starts another.
  A second concurrent turn on one `chat_id` raises (*"already has a turn running on chat"*).
  `boscli ask` and `AskSubagent` use a fresh `chat_id` per call.
- **Two messages per turn** — user message + final answer; no tool calls, results or
  thinking. Failed, timed-out, `AbortTurn`-ed or schema-exhausted turns commit nothing (the
  exchange may remain in an existing native session); a `request_stop()`-ed turn commits its
  partial text; Claude Code's `max_turns` closure commits `MAX_ITERATION_CONTENT`.
- **`BosApp.get_messages(chat_id, *, source="auto"|"bos"|"native")`** (`bos.sdk._app`):
  `"bos"` = the chat store's active window, guaranteed; `"native"` = the runtime's
  `native_messages(chat_id)` — user/assistant messages only, `metadata` `source`,
  `native_turn_id`, `native_item_id`, promising nothing (pruned/compacted by the vendor);
  `"auto"` (default) = native when the chat's stored metadata names a runtime, else bos.
  Routed to the one built agent whose `resolved_config["external_runtime"]` matches; none or
  several → `RuntimeError` (call `app.agent(k).native_messages(chat_id)`). Claude Code reads
  `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/…` JSONL via `get_session_messages` (no CLI;
  `native_turn_id` is `None`; an empty read for a recorded session raises). Codex calls
  `thread.read(include_turns=True)` on its app-server (may spawn it and run the auth
  preflight; bounded by `timeout_seconds`), drops `commentary`, and emits one `system` gap
  marker (`metadata["items_view"]`) per turn not fully loaded.

### 13.7 Project docs

- **Claude Code**: unless `setting_sources` includes `project`, the CLI loads no memory file,
  so BOS reads `<cwd>/CLAUDE.md` itself (`claude_code._root_claude_md`) every turn and appends
  it after `system_prompt` under `# CLAUDE.md in the working directory`. Only that file (no
  `@` imports, parents, subdirs, `CLAUDE.local.md`); a regular file inside `cwd` or it is
  skipped with a WARNING; truncated at 40,000 bytes with a marker line; never added to
  `base_instructions`. Off when `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `CLAUDE_CODE_SAFE_MODE` or
  `CLAUDE_CODE_SIMPLE` is `1`/`true`/`yes`/`on` in BOS's environment.
- **Codex** reads `AGENTS.md` itself; no suppression is offered (`project_doc_max_bytes = 0`
  via `native_options.config` is accepted but did not suppress it live).

### 13.8 MCP egress (`mcp_tools`)

`bos.extensions.runtimes.mcp_egress.BosToolMcpServer`: one streamable-HTTP MCP server per
harness on `127.0.0.1:<ephemeral>`, built lazily by `AgentHarness._ensure_tool_mcp_server`
the first time an agent has a resolvable `mcp_tools` name. Each agent gets a bearer token
(`register_agent`); `tools/list` and `tools/call` are scoped to that token's grant, and a
call runs `ep_tool.invoke(name, args)` in the BOS process (a raise becomes an `isError`
result). Unknown names: one WARNING each at the first turn, skipped
(`mcp_egress.unregistered_tools`; `boscli inspect agent <name>` reports them before any
turn). The token reaches the child only via its environment (`BOS_MCP_BEARER_<random>`),
readable by the agent's own shell. Claude Code: `mcp_servers={"bos-tools": {"type": "http",
…}}`, `strict_mcp_config=True` (no repo `.mcp.json`, no operator servers), tools named
`mcp__bos-tools__<Tool>`, and the hook/`can_use_tool` let through exactly the granted names;
a CLI init reporting the server not connected logs a WARNING. Codex: `config={"mcp_servers":
{"bos-tools": {"url": …, "bearer_token_env_var": …, "default_tools_approval_mode":
"approve"}}}` —
the only pre-approved server under `never`; an operator `[mcp_servers.bos-tools]` in
`~/.codex/config.toml` is merged in and can break the config load (stdio entry, or
`bearer_token`).

### 13.9 Running a turn

- **`run()`/`ask()`** as in §3; `AgentResult.iterations` is `1`; `usage` keys (both):
  `input_tokens` (cached included), `cached_input_tokens`, `cache_write_input_tokens`,
  `output_tokens`, `total_tokens`, `reasoning_output_tokens` (Claude Code: when reported).
- **Content**: Claude Code — image path read and base64-encoded by BOS, `FilePart` sent as
  text `[attachment: <value> (<mime>)]`; Codex — `ImageInput`/`LocalImageInput`, `FilePart`
  → `MentionInput` (a url-sourced `FilePart` raises).
- **`llm_args`**: `model` → per-turn native model; `reasoning_effort` → the runtime's effort.
- **`event_sink`**: Claude Code — `ToolUseBlock` → `tool`/`start`, matching `ToolResultBlock` →
  `tool`/`finish` (`fail` if `is_error`), `TextBlock` → `response`/`finish`, `ResultMessage` →
  `turn`/`finish` (`turn`/`fail`, `stage=detail=max_iteration`, on `max_turns`); sub-agent
  messages and the synthetic `StructuredOutput` tool skipped. Codex — command-execution and
  MCP-tool items → `tool`/`start`·`finish` (`tool_name` = the command, or the tool), agent
  messages → `response`/`finish` (commentary and final answer indistinguishable),
  `turn/completed` → `turn`/`finish`; other items skipped. Both emit one `turn`/`finish` per
  schema attempt.
- **`schema=`**: Claude Code `output_format={"type": "json_schema", …}`, Codex
  `output_schema=`; the reply is always re-validated with the injected `StructuredValidator`,
  retried with a correction message up to `max_schema_retries`, then `StructuredOutputError`.
- **`interrupt` callback**: a truthy return is delivered into the running turn (Claude Code
  `query()` mid-turn; Codex `steer()`); a raised `AbortTurn` interrupts the vendor turn and
  returns `ABORTED_TURN_CONTENT`, `finish_reason="aborted"`.
- **`request_stop()`**: interrupts the running turn and returns its latest text (committed);
  one-way — a later turn returns `SHUTDOWN_CONTENT`, `finish_reason="shutdown"`, before any
  vendor call.
- **`timeout_seconds`**: per attempt (each schema retry gets its own window) plus setup
  (Claude Code `connect()`; Codex `thread_start`/`thread_resume`/`thread.turn`); raises
  `TimeoutError` naming the phase; mid-turn expiry interrupts first. `None` = no deadline.
  `aclose()` drains in-flight turns for 10 s, then closes clients regardless.
- **`finish_reason`** (verbatim): Claude Code — the CLI's `terminal_reason` (e.g.
  `completed`), else `stop_reason`; `max_turns`; a stop gives `aborted_tools` /
  `aborted_streaming`. Codex — `TurnStatus` (`completed`; `interrupted` on a stop). An
  interrupted or failed turn BOS did not ask for raises.

---

## 14. CLI reference (`boscli`)

Global options: `-c/--config <path|preset>` (or `BOS_CONFIG`), `-l/--log-level <LEVEL>`
(or `BOS_LOG_LEVEL`, default `ERROR`). Commands are lazy-loaded; third parties add more via
`boscli.commands` (§10).

| Command | Purpose | Notable options |
| --- | --- | --- |
| `ask "<prompt>"` | One-shot, in-process agent turn. | `--stdin`, `--model`, `--agent <kind>`, `--actor <name>`, `--no-steps`, `-w/--workspace` |
| `init` | Guided project scaffolding. | `--minimal` (emit commented template), `--name`, archetype (workspace/package), `--no-probe` |
| `gateway start` | Start the runtime. | (subgroup) |
| `gateway stop` / `status` / `restart` | Control a running gateway. | uses `gateway.state` |
| `tui` | Connect the terminal UI to a gateway. | |
| `doctor` | Health checks (config, paths, env, credentials). | |
| `inspect` | Introspect harness/config/runtime state. | (subcommands) |
| `memory` | Memory backend admin (list/show/etc.). | (subcommands) |

`boscli ask` bypasses the gateway: it builds the workspace + harness and runs one agent
turn **in-process**, printing the final reply to stdout (progress streams to stderr on a
TTY). It honors `--model` / `BOS_MODEL` per invocation. Agent selection has three explicit
paths (BEP 18 §3.6) and a bare `ask` **never** consults the actor table: bare →
`Workspace.resolve_default_agent()` (top-level `default_agent` — §4.2 — else the only agent,
else one named `main`, else an error naming the available kinds); `--agent <kind>` → that
kind, plain; `--actor <name>` → `actors[name].agent` **with** `actors[name].agent_cfg`
applied, which is the only in-process path that reads the actor table. `--agent` and
`--actor` are mutually exclusive.

`boscli init` flow: prompt for purpose → pick **archetype** (`workspace` = plain project with
`./extensions`; `package` = installable `src/<pkg>/` whose extensions register via the
`bos.exts` entry point) → choose provider/model (detects API keys) → scaffold files →
optional credential probe (one LLM call unless `--no-probe`) → optional `git init`.

Useful env vars: `BOS_HOME` (default `~/.bos`), `BOS_CONFIG`, `BOS_MODEL`,
`BOS_CONSOLIDATOR_MODEL`, `BOS_CAPABILITY_LIMIT` (max skills/subagents
listed in the prompt, default 50), `BOS_LOG_LEVEL`, plus provider `*_API_KEY`s.

---

## 15. Quick reference

**Config sections**: `default_agent` (top-level key) · `[platform]` (env/discovery) · `[harness]` (service impls) ·
`[exts.<ep>.<impl>]` (extension config) · `[agent.defaults]` + `[agents.<name>]` (agents) ·
`[runtime]` / `[runtime.gateway]` / `[runtime.actors.<name>]` / `[[runtime.channels]]` (runtime).

**Decorators** (`from bos.core import …`): `ep_tool`, `ep_provider`, `ep_agent`,
`ep_chat_store`, `ep_consolidator`, `ep_turn_interceptor`, `ep_mail_route`,
`ep_channel`, `ep_plugin`.

**Entry-point groups**: `bos.exts` (extensions) · `bos.skills` (skills) · `boscli.commands`
(CLI).

**Reserved agent kinds**: `claude-code`, `codex` — external runtimes (§13); valid `_parent`
targets, registered as agents only when `[agents.<kind>]` exists.

**Default harness impls**: `LLMConsolidator`, `JsonlChatStore`, `JsonlMailRoute`.
**Default plugins**: `MemoryPlugin`, `PlanPlugin`, `TaskPlugin`,
`SkillsPlugin`, `SubagentPlugin`.

**Which agent runs by default** depends on the path, and there is no single answer:
`resolve_default_agent()` (§4.2 — `default_agent`, else the only agent, else `main`, else an
error) answers it for `boscli ask` and `bos.sdk`, and never falls back to a hard-coded name.
The literal `"BOS"` is a fallback in exactly one place — `get_main_agent_kind()`, for a
gateway config with no `[runtime.actors]` at all. `BOS` is otherwise just the conventional
name of the built-in `@ep_agent` assistant that the shipped preset points `default_agent` at.

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
