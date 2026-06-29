# Writing Plugins

A **plugin** is the right unit when you want to bundle tools together with system-prompt sections, interceptors, and shared state that is initialised once and reused across agents.  Plugins register at `ep_plugin` and follow a two-class design: one shared process-level instance (`HarnessPlugin`) and one per-agent instance (`AgentPlugin`).

---

## The two roles

| Class | Lives | Responsibility |
|-------|-------|----------------|
| `HarnessPlugin` | Once per process (lazily on first use) | Holds shared resources; calls `setup`, `teardown`, `validate_config`, `bind` |
| `AgentPlugin` | Once per agent instance | Registers agent-local tools; provides a system-prompt section; returns interceptors |

---

## Full lifecycle

```
1. Bootstrap: ep_plugin.invoke(name) → HarnessPlugin instance created
2. await setup(services)             → shared resources initialised; called once, cached
3. Per agent creation:
   a. cfg = default_config() | plugin-bindings[name]  (+ injected agent_name)
   b. validate_config(cfg)
   c. agent_plugin = bind(cfg)              → AgentPlugin instance
   d. agent_plugin.register_tools(local_registry)   → agent-local tools
   e. agent_plugin.get_interceptors()       → collected into the turn chain
4. Each turn:
   - await agent_plugin.get_system_prompt_section(context) → appended to the system prompt
5. Harness shutdown: await teardown() in reverse setup order
```

### `PluginServices` fields

`setup(services: PluginServices)` receives:

| Field | Type | Notes |
|-------|------|-------|
| `bos_dir` | `Path` | `.bos/` directory for this workspace |
| `workspace` | `Path` | Workspace root |
| `llm` | `LLMClient` | For making LLM calls from within a plugin |
| `consolidator` | `Consolidator` | Memory/summary service |
| `chat_store` | `ChatStore` | Conversation persistence |
| `events` | `EventBus \| None` | Publish/subscribe event bus |
| `jobs` | `JobRunner \| None` | Off-turn job runner |
| `agent_runner` | `AgentRunner \| None` | Run a disposable sub-agent (BEP 12) |

---

## Minimal plugin template

```python
# .bos/extensions/my_plugin.py
from collections.abc import Mapping, Sequence
from typing import Any

from bos.core.contract import AgentPlugin, PluginServices, ep_plugin
from bos.core.registry import ToolRegistry


@ep_plugin(name="MyPlugin")
class MyHarnessPlugin:
    @property
    def name(self) -> str:
        return "MyPlugin"

    def default_config(self) -> Mapping[str, Any]:
        # Defaults merged under every agent's plugin-bindings.MyPlugin
        return {"greeting": "hi"}

    async def setup(self, services: PluginServices) -> None:
        self._services = services
        # Initialise shared resources here (DB connections, HTTP clients, …)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config.get("greeting", ""), str):
            raise TypeError("MyPlugin: 'greeting' must be a string")

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        return MyAgentPlugin(greeting=config.get("greeting", "hi"))

    async def teardown(self) -> None:
        pass  # Release shared resources here


class MyAgentPlugin:
    def __init__(self, greeting: str) -> None:
        self._greeting = greeting

    @property
    def name(self) -> str:
        return "MyPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        greeting = self._greeting

        @registry(
            name="Greet",
            description="Return a personalised greeting.",
            parameters={
                "type": "object",
                "properties": {"who": {"type": "string", "description": "Name to greet."}},
                "required": ["who"],
            },
        )
        async def greet(who: str) -> str:
            return f"{greeting}, {who}!"

    async def get_system_prompt_section(self, context: Any) -> str | None:
        # Return a string to append to the system prompt this turn, or None.
        return None

    def get_interceptors(self) -> Sequence[Any]:
        return []
```

### How the pieces contribute

- **`register_tools`** — registers agent-local tools using the same `@registry(...)` signature as `@ep_tool`.  Local tools take precedence over global `ep_tool` registrations on name clash.
- **`get_system_prompt_section`** — called once per turn.  Return an XML block, a plain string, or `None`.  Sections from all enabled plugins are appended to the base system prompt.
- **`get_interceptors`** — return a list of `TurnInterceptor` objects to run best-effort before the configured interceptor chain.  Raise `AbortTurn` inside an interceptor to stop a turn early.

---

## Enable / disable and config

Enable a plugin in an agent (or globally in `[agent.defaults]`):

```toml
[agents.main.plugins]
enabled = ["MyPlugin"]          # or ["*"] for all registered plugins

[agents.main.plugin-bindings.MyPlugin]
greeting = "hello"
```

> **Note:** The TOML key is `plugin-bindings` (with a hyphen), matching the `AgentConfig` field name.

`"*"` in `enabled` expands to **all registered `ep_plugin` names** minus `disabled`.  This means newly registered plugins from packages are automatically included when an agent uses `enabled = ["*"]`.

---

## Plugins can define their own extension points (`pep_`)

A plugin may expose its own pluggable sub-implementations.  `SkillsPlugin` does this to support alternative skill loaders:

```python
from bos.core.registry import ExtensionPoint

# Declare the extension point — prefix with pep_ to distinguish from core ep_ points
pep_skills_loader = ExtensionPoint(
    name="pep_skills_loader",
    description="Skills loader implementations.",
)

@pep_skills_loader(name="FileSystemSkillsLoader")
class FileSystemSkillsLoader:
    ...
```

Users select and configure the implementation via the same universal mechanism:

```toml
[exts.pep_skills_loader.FileSystemSkillsLoader]
skill_dirs = ["skills"]
```

---

## Built-in plugins

These are registered when `bos.exts` is loaded and enabled by default in the built-in `BOS` agent:

| Plugin | What it adds | Key `plugin-bindings.<Plugin>` options |
|--------|-------------|---------------------------------------|
| `MemoryPlugin` | Persistent memory + recall tools; off-turn consolidation | `maxims` (categories, default `["user","self","rules"]`), `consolidation.{enabled,retention_days,model}` (consolidation off by default). Memory is isolated per agent identity — there is no `scope` option. |
| `PlanPlugin` | Planning tools and a system-prompt section | — |
| `TaskPlugin` | Async task creation/scheduling tools (BEP 11) | — |
| `SkillsPlugin` | `LoadSkill` tool + skill discovery | `skill_dirs`, `allow`, `exclude`, `loader`, `preload` |
| `SubagentPlugin` | `AskSubagent` tool to delegate work to named agents | `enabled` (list or `"*"`), `disabled`, `task_template` |

`SubagentPlugin.enabled` is the allow-list of agent kinds the agent may delegate to.  It requires `services.agent_runner` and raises during `setup` if absent.

---

## See also

- [Tools](tools.md) — standalone `@ep_tool` registration without a plugin
- [Packaging & entry points](entry-points.md) — ship a plugin in an installable package
- [Configuration](../configuration/index.md) — full `[agents]` and `[agent.defaults]` reference
