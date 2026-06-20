# BEP 4: Micro-Kernel and Plugin Architecture

Status: **design accepted** — architecture-level contract is settled and ready for implementation design.

---

## Core Insight

`ReActAgent` currently owns both the agent loop and several concrete capabilities: memory, task tracking, skill loading, and subagent delegation. In `src/bos/core/agent.py`, those capabilities are registered as agent-local tools through nested closures that capture agent state directly.

This makes the agent loop harder to reuse, test, and extend because capability logic is coupled to the kernel. BEP 4 moves capability logic into plugins while keeping the kernel responsible for the turn loop, prompt compilation, local tool registry, tool invocation boundary, persistence, and interceptor execution.

The target architecture is:

```text
Agent kernel
  turn loop
  prompt compiler
  local tool registry
  tool invocation boundary
  interceptor runner
  persistence/history/consolidation

Harness plugins
  harness-scoped lifecycle/resources

Agent plugins
  agent-scoped config/state/tools/prompt/interceptors
```

`ReActAgent` has **zero hardcoded LLM tools**. Default capabilities such as memory, tasks, skills, and subagent delegation are normal plugins.

---

## Goals

1. Remove capability-specific tool implementations from `ReActAgent`.
2. Let different agents bind the same plugin with different settings.
3. Keep plugin-bound tools agent-scoped by registering them into the agent-local `ToolRegistry`.
4. Keep process-global `ep_tool` available as public BOS API for standalone global tools.
5. Expose harness/platform capabilities to plugins through narrow typed services, not through raw `AgentHarness` access.
6. Preserve deterministic prompt, interceptor, and tool-registration order.
7. Make lifecycle and error handling explicit enough for implementation.

## Non-Goals

1. This BEP does not define implementation phases. Temporary staging notes live in `docs/debate/BEP 4: Implementation Staging Notes.md`.
2. This BEP does not preserve a production compatibility layer. There is no production usage to migrate.
3. This BEP does not settle cache-control mechanics for ephemeral plugin context.

---

## Configuration Spec

Workspace plugin defaults live under `platform.plugins`. These defaults do not enable a plugin by themselves.

```toml
[platform]
# enabled_plugins = ["MemoryPlugin", "TaskPlugin", "SkillsPlugin", "SubagentPlugin"]

[platform.plugins.MemoryPlugin]
maxims = ["user", "self"]
scope = "workspace"
```

Plugins are disabled by default. A plugin is enabled for an agent when either:

1. its name appears in `platform.enabled_plugins`, or
2. that agent sets `enabled = true` in its plugin table.

An agent can disable a globally enabled plugin with `enabled = false`.

```toml
[platform]
enabled_plugins = ["MemoryPlugin", "TaskPlugin"]

[platform.plugins.MemoryPlugin]
maxims = ["user", "self"]

[[platform.agents]]
name = "coding"
system_prompt = "You write code."

# Overrides global MemoryPlugin defaults for this agent.
[platform.agents.plugins.MemoryPlugin]
maxims = ["coding", "repo", "rules"]
scope = "agent:coding"

# Enables SkillsPlugin for this agent only.
[platform.agents.plugins.SkillsPlugin]
enabled = true
allow = ["code-review", "repo-navigation"]

[[platform.agents]]
name = "stateless"

# Disables globally enabled MemoryPlugin for this agent.
[platform.agents.plugins.MemoryPlugin]
enabled = false
```

External agent files may use a top-level `plugins` table. During workspace loading, it is treated as agent-local plugin config:

```toml
# agents/coding.toml
name = "coding"
system_prompt = "You write code."

[plugins.MemoryPlugin]
enabled = true
maxims = ["coding", "repo"]
```

This normalizes to the same internal shape as:

```toml
[[platform.agents]]
name = "coding"

[platform.agents.plugins.MemoryPlugin]
enabled = true
maxims = ["coding", "repo"]
```

### Config Resolution

For each agent and plugin, config is resolved by **shallow merge** in this order:

```text
plugin code defaults
< platform.plugins.<PluginName>
< agent-local plugins.<PluginName>
```

Scalars, lists, and dictionaries all replace at the top level. There is no recursive merge in BEP 4.

### Enablement and Ordering

Resolved plugin order is deterministic:

1. globally enabled plugins in `platform.enabled_plugins` list order;
2. agent-only enabled plugins in agent config declaration order.

Agent overrides of globally enabled plugins retain the global order position. Disabled plugins are removed from the resolved order.

This order controls:

- plugin prompt sections;
- local tool registration attempts;
- plugin interceptor execution;
- future ordered plugin hooks.

### Unknown and Invalid Config

Unknown plugin names produce warnings and are skipped. They do not fail startup.

Invalid settings for a known plugin are startup errors. A known plugin config table should be validated even when it appears only as defaults, so config mistakes are not silently ignored.

---

## Core Contracts

### Plugin Discovery

BEP 4 adds a plugin extension point in `src/bos/core/contract.py`:

```python
ep_plugin = ExtensionPoint(description="Harness plugin. A class or factory implementing HarnessPlugin.")
```

Plugin packages register plugin providers through `ep_plugin`:

```python
@ep_plugin(name="MemoryPlugin")
class MemoryHarnessPlugin: ...
```

`ep_plugin` discovers harness-scoped plugin providers. It is not a tool registry.

### Context Contracts

`TurnContext` is the broad mutable context for the current agent turn. It is used by the agent loop, prompt hooks, and interceptors.

```python
@dataclass
class TurnContext:
    agent_name: str
    chat_id: str
    turn_id: str

    system: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    current: list[Message] = field(default_factory=list)
    ephemeral: list[dict[str, Any]] = field(default_factory=list)

    tool_defs: list[dict[str, Any]] = field(default_factory=list)
    current_llm_response: LLMResponse | None = None
    final_content: str | None = None

    event_sink: EventSink | None = None

    def get_llm_messages(self) -> list[dict[str, Any]]:
        return self.system + self.history + [m.llm_message for m in self.current] + self.ephemeral
```

`ephemeral` contains request-only messages that are sent to the LLM but not persisted. Cache-control remains an implementation detail of the LLM call path. The current LiteLLM integration may continue to use `cache_control_injection_points`; implementation must ensure cache hints are not placed on dynamic ephemeral context unless intentionally desired. BEP 4 makes no cache optimization claim.

`ToolContext` is the narrow per-tool-call context. It is derived from `TurnContext` and passed to tool handlers that accept a `context` parameter.

```python
@dataclass(frozen=True)
class ToolContext:
    agent_name: str
    chat_id: str
    turn_id: str
    event_sink: EventSink | None = None
    extra_data: Mapping[str, Any] = field(default_factory=dict)
```

`extra_data` is an escape hatch for caller/runtime values not formally declared on the context. It is not a service locator and must not carry raw harness or plugin internals.

### Binding Context

Plugins may need binding-time facts that are not normal user config, such as named-actor scope. Those facts are passed through `AgentBindContext`.

```python
@dataclass(frozen=True)
class AgentBindContext:
    agent_name: str
    actor_scope: str | None = None
```

### Harness Platform Services

Plugins must not receive the raw `AgentHarness`. Harness-owned capabilities are exposed through narrow service protocols.

```python
class SubagentRuntime(Protocol):
    async def ask(
        self,
        role: str,
        message: str,
        *,
        parent: ToolContext,
    ) -> str:
        """Delegate to a configured subagent and return its response."""
        ...


@dataclass(frozen=True)
class PluginServices:
    bos_dir: Path
    workspace: Path
    llm: LLMClient
    message_store: MessageStore
    consolidator: Consolidator
    subagents: SubagentRuntime
```

The harness may add explicit typed services over time, but it should not expose raw private methods such as `_get_subagent_config` or `_make_subagent_chat_id` to plugins.

### HarnessPlugin and AgentPlugin

Plugins are split by scope:

- `HarnessPlugin`: one per harness, owns shared lifecycle/resources/service adapters.
- `AgentPlugin`: one per agent binding, owns resolved agent config, local state, prompt sections, local tool handlers, and interceptors.

```python
class HarnessPlugin(Protocol):
    @property
    def name(self) -> str: ...

    def default_config(self) -> Mapping[str, Any]: ...

    async def setup(self, services: PluginServices) -> None: ...

    def validate_config(
        self,
        config: Mapping[str, Any],
        context: AgentBindContext,
    ) -> None: ...

    def bind(
        self,
        config: Mapping[str, Any],
        context: AgentBindContext,
    ) -> "AgentPlugin": ...

    async def teardown(self) -> None: ...


class AgentPlugin(Protocol):
    @property
    def name(self) -> str: ...

    def register_tools(self, registry: ToolRegistry) -> None: ...

    async def get_system_prompt_section(self, context: TurnContext) -> str | None: ...

    def get_interceptors(self) -> Sequence[TurnInterceptor]: ...
```

`AgentPlugin.register_tools(registry)` replaces the earlier `get_tools() -> list[str]` sketch. Plugins contribute complete tool definitions and bound handlers to the owning agent's local `ToolRegistry`.

There is no `AgentContext.get_plugin(name)` API. Tool handlers are already bound to the correct `AgentPlugin` instance when registered.

---

## Agent Kernel Behavior

### Constructor Surface

`ReActAgent` receives core services and bound plugins. Capability-specific constructor arguments are removed.

```python
class ReActAgent:
    def __init__(
        self,
        *,
        message_store: MessageStore,
        consolidator: Consolidator,
        plugins: Sequence[AgentPlugin] = (),
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        tools_usage: dict[str, str] | None = None,
        exclude_tools: list[str] | None = None,
        name: str | None = None,
        model: str | None = None,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
        llm: LLMClient | None = None,
        local_tools: ToolRegistry | None = None,
        interceptor: TurnInterceptor | None = None,
        max_tokens: int = 128 * 1024,
        max_iterations: int = 25,
    ): ...
```

Removed from the kernel constructor:

```text
memory
skills_loader
skills
exclude_skills
maxims
subagents
exclude_subagents
iterations_per_task
```

These move into plugin config and plugin implementation.

### Local Tool Registration

Each agent owns an agent-local `ToolRegistry`. During construction, enabled `AgentPlugin` instances register tools into that registry:

```python
self._local_tools = local_tools or ToolRegistry("Agent-scoped local tools.")
for plugin in self._plugins:
    plugin.register_tools(self._local_tools)
```

Same-agent local tool name collisions are errors.

`ep_tool` remains public BOS API for standalone process-global tools. A plugin package may still register global tools with `ep_tool` when appropriate. However, agent-specific plugin behavior should use `AgentPlugin.register_tools()`.

Agent-local tools shadow process-global `ep_tool` tools of the same name for that agent.

### Tool Permissions

Plugin enablement and tool permission are separate concerns.

An enabled plugin always contributes its prompt sections, interceptors, state, and local tool registrations. The agent's `tools` and `exclude_tools` filters determine which registered tools are callable.

If the model calls a registered but disallowed tool, the normal tool permission boundary returns a tool error.

Provider tool schemas and the available-tools prompt section must be generated from the same filtered effective tool set:

```text
process-global ep_tool tools
+ agent-local tools
- tools/exclude_tools filter
```

### Tool Invocation Context

Plugin-owned tool settings are bound into the `AgentPlugin`. They should not rely on per-call `tool_config` injection.

The invocation boundary injects `ToolContext` into tools that declare a `context` parameter. Existing global tools may continue to accept legacy named injections such as `chat_id`, `turn_id`, `event_sink`, or `tool_config`, but plugin-owned tools should prefer `ToolContext`.

### Prompt Compilation

`ReActAgent` remains responsible for compiling the final system prompt.

Prompt order is:

```text
base system prompt
plugin system prompt sections
available tools
system info
```

Core prompt sections:

1. base system prompt;
2. available tools;
3. system info.

Plugin prompt sections:

- MemoryPlugin maxims;
- SkillsPlugin available skills;
- SubagentPlugin available subagents;
- any plugin-specific instructions.

Plugin sections are rendered in resolved plugin order.

### Interceptor Composition

Plugin interceptors run before configured harness/workspace interceptors.

```text
plugin interceptors, in resolved plugin order
configured ChainReactInterceptor
```

Interceptor exceptions are logged and the turn continues, except `AbortTurn`, which propagates.

---

## Harness Assembly

`AgentHarness` owns core services and harness-scoped plugins:

```text
MessageStore
Consolidator
LLMClient
MailRoute
configured interceptors
HarnessPlugin instances
```

Harness startup:

1. loads extension modules/paths;
2. resolves known plugin names from config and enabled plugin lists;
3. instantiates known `HarnessPlugin` providers needed for validation and for enabled bindings;
4. validates known plugin config tables and resolved per-agent plugin configs;
5. builds `PluginServices` with typed platform services;
6. calls `setup(services)` on each harness plugin that is enabled by at least one agent.

Agent creation:

1. resolves plugin config for that agent;
2. determines enabled plugin order;
3. creates `AgentBindContext`;
4. validates resolved config;
5. calls `HarnessPlugin.bind(config, context)`;
6. passes bound `AgentPlugin` instances into `ReActAgent`.

No plugin receives the raw harness object.

---

## Lifecycle and Error Semantics

| Situation | Behavior |
|---|---|
| Unknown plugin name | Warn and skip |
| Invalid config for known plugin | Startup error |
| `HarnessPlugin.setup()` raises | Startup error |
| `HarnessPlugin.bind()` raises | Agent creation/startup error |
| `AgentPlugin.register_tools()` raises | Agent creation/startup error |
| Same-agent local tool collision | Agent creation/startup error |
| Runtime tool exception | Caught and returned as tool result, as today |
| Plugin prompt section exception | Fail the turn |
| Plugin interceptor exception | Log and continue, except `AbortTurn` |
| `HarnessPlugin.teardown()` raises | Log and continue cleanup |

Teardown is best-effort and proceeds in reverse plugin setup order.

---

## Default Plugins

Default capabilities are implemented as normal plugins. They are not special-cased in `ReActAgent`.

### MemoryPlugin

Memory is plugin-owned. Core removes `ep_memory` and the core `MemoryExtension` dependency.

MemoryPlugin owns:

- memory backend construction and lifecycle;
- maxim prompt section;
- `Remember`;
- `Recall`;
- `ReviseMaxim`;
- `Forget`;
- actor scope handling via `AgentBindContext.actor_scope`;
- maxims allowlist/config.

MemoryPlugin may retain an internal backend protocol equivalent to the current memory contract:

```python
class MemoryBackend(Protocol):
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...
    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]: ...
    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str: ...
    async def get_memory(self, entry_id: str) -> MemoryEntry | None: ...
    async def forget_memory(self, entry_id: str) -> None: ...
```

That protocol belongs to MemoryPlugin implementation, not the agent kernel.

### TaskPlugin

TaskPlugin owns task state and task tools:

- `TaskCreate`;
- `TaskUpdate`;
- `TaskList`;
- `TaskGet`.

Task lists are stored on the agent-scoped TaskPlugin, keyed by `chat_id`.

BEP 4 removes dynamic iteration-budget scaling. The agent uses fixed `max_iterations`. A future BEP may define graceful max-iteration abortion by summarizing turn progress and clearly stating the stop reason so the work can continue in a new turn.

Task tools should return updated task state in tool results so the model sees the current state through the normal tool-message path.

TaskPlugin may also inject request-only active-task context through `TurnContext.ephemeral` during `before_llm`, but this must be idempotent and must not be described as a cache optimization. Cache-control for this context follows the general `TurnContext.ephemeral` rule: cache hints should not be placed on dynamic ephemeral context unless intentionally desired.

Task update events are emitted by plugin interceptors through first-class `TurnContext.event_sink`.

### SkillsPlugin

Skills are plugin-owned. Core removes `ep_skills_loader` and the core `SkillsLoader` dependency.

SkillsPlugin implements skill discovery and loading directly. It owns:

- available skills prompt section;
- `LoadSkill`;
- skill allow/exclude config;
- skill source/path config.

### SubagentPlugin

Subagent delegation is a normal plugin. `AskSubagent` is not a core tool.

SubagentPlugin owns:

- available subagents prompt section;
- `AskSubagent` tool;
- subagent allow/exclude config.

It delegates through `SubagentRuntime`, not raw harness internals:

```python
class SubagentAgentPlugin:
    async def ask_subagent(
        self,
        role: str,
        message: str,
        *,
        context: ToolContext,
    ) -> str:
        return await self._runtime.ask(role, message, parent=context)
```

The harness implementation of `SubagentRuntime` may internally resolve subagent config, apply task templates, derive child chat IDs, create child agents, and derive child event sinks. Those details remain private to the harness adapter.

---

## Component Taxonomy

| Component | Classification | Rationale |
|---|---|---|
| `MessageStore` | Core Service | Mandatory turn persistence. No LLM tool surface. |
| `Consolidator` | Core Service | Context compression utility. No LLM tool surface. |
| `LLMClient` | Core Service | Provider abstraction. No LLM tool surface. |
| `MailRoute` | Core Service | Message routing infrastructure. No LLM tool surface. |
| `ReActAgent` loop | Agent Kernel | Executes turns, invokes tools, compiles prompts, persists messages. |
| Agent-local `ToolRegistry` | Agent Kernel | Per-agent effective tool namespace. |
| `ep_tool` | Global Extension Point | Public process-global standalone tool API. |
| `ep_plugin` | Global Extension Point | Public plugin discovery API. |
| `MemoryPlugin` | Default Plugin | Memory tools, maxims, backend, scoped memory. |
| `TaskPlugin` | Default Plugin | Task tools, task state, task events, optional ephemeral task context. |
| `SkillsPlugin` | Default Plugin | Skill discovery, skill prompt section, `LoadSkill`. |
| `SubagentPlugin` | Default Plugin | Subagent prompt section and `AskSubagent`. |

Core extension points after BEP 4 include:

```text
ep_tool
ep_agent
ep_message_store
ep_consolidator
ep_provider
ep_turn_interceptor
ep_plugin
```

Removed from core:

```text
ep_memory
ep_skills_loader
```

A plugin may define its own internal extension points, but those are not part of the agent kernel contract.

---

## Named Actors Impact

Named actors use the same plugin binding path as normal agents.

`NamedAgent` should not receive `ScopedMemory` or a memory service directly. Instead, named actor construction passes actor scope through `AgentBindContext`:

```python
context = AgentBindContext(
    agent_name=agent_kind,
    actor_scope=scope,
)
plugins = harness.bind_plugins_for_agent(agent_spec, context)
```

MemoryPlugin uses `actor_scope` during binding to create a scoped memory backend or equivalent scoped behavior. The existing `ScopedMemory` wrapper may remain as an internal MemoryPlugin utility, but it is no longer injected into `ReActAgent`.

---

## Default Configuration Template

`bos init` should not enable plugins globally by default. It may include a commented example:

```toml
[platform]
# enabled_plugins = ["MemoryPlugin", "TaskPlugin", "SkillsPlugin", "SubagentPlugin"]
```

Default agent capabilities may be expressed in `src/bos/core/defaults/agent_spec.py` through agent-level plugin config, not through generated workspace-global enablement.

---

## Resolved Design Decisions

1. `AskSubagent` becomes `SubagentPlugin`; `ReActAgent` has zero hardcoded LLM tools.
2. Harness/platform capabilities are exposed through typed services such as `SubagentRuntime`, not raw `AgentHarness`.
3. Use `HarnessPlugin` / `AgentPlugin` naming.
4. Use `AgentPlugin.register_tools(registry)`, not `get_tools() -> list[str]`.
5. Remove `AgentContext.get_plugin(name)`.
6. Use `ToolContext` for per-tool-call metadata.
7. Use top-level `platform.enabled_plugins` for global enablement.
8. Use `platform.plugins.<PluginName>` for workspace plugin defaults.
9. Use shallow config merge.
10. Unknown plugin names warn; invalid known-plugin config fails startup.
11. Remove dynamic task iteration budget adjustment.
12. Use fixed `max_iterations`; graceful max-iteration continuation is future work.
13. Plugin prompt order is base → plugin sections → tools → system info.
14. Plugin interceptors run before configured harness/workspace interceptors.
15. `event_sink` is first-class on `TurnContext`.
16. Remove `ep_memory` and `ep_skills_loader` from core.

---

## Cache-Control Note

`TurnContext.ephemeral` is useful for dynamic request-only context such as active task status. Cache-control remains an implementation detail of the LLM call path. The current LiteLLM integration may continue to use `cache_control_injection_points`; implementation must ensure cache hints are not placed on dynamic ephemeral context unless intentionally desired. BEP 4 does not claim ephemeral task context improves prefix caching.

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-20 | Initial draft | Formulate plugin architecture for decoupling agent-scoped tools from `ReActAgent`. |
| 2026-05-20 | Added task case study and named actor impact | Explore plugin boundaries and scoped memory implications. |
| 2026-05-21 | Consolidated review decisions | Make `AskSubagent` a plugin, switch to `HarnessPlugin`/`AgentPlugin`, local plugin tool registration, explicit config semantics, lifecycle semantics, and remove core memory/skills extension points. |
