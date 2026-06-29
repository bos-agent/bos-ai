# Extending BOS: Overview

BOS is designed to be extended at every layer. Whether you need a new tool the agent can call, a custom channel that bridges a new messaging platform, or a plugin that bundles multiple capabilities, the same mechanism applies: register an implementation at a named **extension point**.

---

## The extension-point model

An **extension point** is a named registry of interchangeable implementations.  The machinery lives in `bos.core.registry.ExtensionPoint`.  Every registered name in a public extension point must be globally unique — a duplicate name raises at process startup, catching conflicts early.

When you decorate a function or class with an extension-point decorator such as `@ep_tool(...)`, the decorated object is registered immediately on import.  BOS discovers your module by importing it (see [Loading extensions](#loading-extensions)), at which point the decorator's side-effect fires.

The `ExtensionPoint.invoke(name, kwargs)` method merges your config-file defaults with the call-site arguments and calls (or awaits) the registered function.  This is how `[exts.<ep>.<impl>]` configuration flows in — you never have to parse config yourself.

---

## Naming conventions

| Prefix | Meaning |
|--------|---------|
| `ep_`  | Core extension point, defined in `bos.core.contract` |
| `pep_` | Plugin-defined extension point (declared by a plugin, not core) |

The `ep_` / `pep_` names are also the keys used in the `[exts]` config section.

---

## Core extension points

| Extension point | What you register | Selected / used by |
|---|---|---|
| `ep_tool` | An async function the LLM can call | Agents, via `[…tools].enabled` |
| `ep_provider` | `async (messages, **kwargs) -> LLMResponse`; selected by model-string prefix | `LLMClient` dispatch |
| `ep_agent` | Factory returning an agent-spec dict | Bootstrap, merged into `[agents.<name>]` |
| `ep_chat_store` | Class/factory producing a `ChatStore` | `[harness].chat_store` |
| `ep_consolidator` | Factory producing a `Consolidator` | `[harness].consolidator` |
| `ep_turn_interceptor` | Factory producing a `TurnInterceptor` | `[harness].interceptors` chain |
| `ep_job_runner` | Factory producing a `JobRunner` | `[harness].job_runner` |
| `ep_mail_route` | Factory producing a `MailRoute` | `[harness].mail_route` |
| `ep_channel` | Class/factory producing a `Channel` | `[[runtime.channels]].type` |
| `ep_plugin` | Class/factory producing a `HarnessPlugin` | `[…plugins].enabled` |

Plugin-defined example: `pep_skills_loader` (declared in `SkillsPlugin`).

---

## Configuring any extension: `[exts.<ep>.<impl>]`

Every extension point shares a single, uniform configuration mechanism.  Add a TOML table whose path is `[exts.<ep_name>.<impl_name>]`, and its keys are deep-merged into that extension's defaults before it is called:

```toml
# Pass defaults to a specific tool
[exts.ep_tool.GrepSearch]
extend_ignore = ["*.generated.py"]

# Configure which model the consolidator uses
[exts.ep_consolidator.LLMConsolidator]
model = "gemini/gemini-2.5-flash"

# Pass a region default to a custom agent factory
[exts.ep_agent.weather_agent]
region = "eu"

# Configure a plugin-defined extension point
[exts.pep_skills_loader.FileSystemSkillsLoader]
skill_dirs = ["skills"]
```

`[exts]` uses `extra='allow'`, so newly registered extension points accept config without any schema changes.  An `[exts.<ep>]` block whose name matches no registered extension point is logged and ignored.

---

## Loading extensions

BOS imports your extension module through one of three routes — all of which cause the decorator side-effects to fire:

### 1. The `./extensions` directory (zero-config, project-local)

Drop any `.py` file into the `./extensions/` directory inside your `bos_dir`.  BOS scans and imports every Python file it finds there automatically.  No config change required — this path is in the default `extensions` list.

```
my-agent/
└── .bos/
    └── extensions/
        └── my_tools.py   # @ep_tool decorators here are auto-discovered
```

### 2. Module name in `[platform].extensions`

List any importable module (installed in the project venv) as an entry in `[platform].extensions`:

```toml
[platform]
extensions = ["bos.exts", "./extensions", "my_package.bos_extensions"]
```

Each entry that exists as a path relative to `bos_dir` is loaded as a directory/file; otherwise it is imported as a module name.

### 3. The `bos.exts` entry point (installable packages)

For a distributable package, declare a `bos.exts` entry point in `pyproject.toml`.  When `bos.exts` is imported (the default first entry in `extensions`), it iterates this group and loads each module:

```toml
[project.entry-points."bos.exts"]
my-tools = "my_package.bos_extensions"
```

See [Packaging & entry points](entry-points.md) for the full picture.

---

## What to read next

| Topic | Page |
|---|---|
| Writing tools the LLM can call | [Tools](tools.md) |
| Bundling tools + prompt sections into a plugin | [Plugins](plugins.md) |
| Bridging a new messaging platform | [Channels](channels.md) |
| Providers, stores & other service extensions | [Providers, stores & services](stores-and-providers.md) |
| Shipping as an installable package | [Packaging & entry points](entry-points.md) |
| Configuration reference | [Configuration](../configuration/index.md) |
