# BEP 4: Decouple Agent-Scoped Tools via AgentContext

Status: **design** — contract and tool surface finalized, ready for implementation planning.

---

## Core Insight

Currently, [ReActAgent](file:///home/jzhang/bos-ai/src/bos/core/agent.py#L181) registers its core capabilities—such as task management, memory operations, subagent interaction, and skill loading—at the agent instance level as nested closures. These tools access agent-scoped state (like memory backends, task lists, and policies) directly from the `self` instance.

This design introduces tight coupling between the agent runtime and concrete tool logic, making it difficult for developers to:
1. **Extend or Override**: Swap out default behaviors (e.g., customize `Remember` or run custom subagent routing).
2. **Decouple**: Reuse the agent runner independently of specific built-in tool assumptions.
3. **Test**: Unit test core tools in isolation without constructing a full agent runtime.

By migrating to a **Micro-kernel & Plugin Architecture**, the agent core is simplified to an execution loop with **zero built-in tools**. Features like memory, tasks, and skills are encapsulated inside harness plugins. These plugins dynamically register their tools at startup and provide prompt injection sections and interceptors at turn runtime.

---

## Configuration Spec

Plugins are configured globally in the workspace configuration, with settings matching the plugin's registration name. Configuration supports both global defaults and agent-specific overrides.

```toml
# Pass-through configuration defaults for all agents
[platform.agent_defaults.plugins.MarkdownMemoryPlugin]
maxims = ["user", "soul"]

# Agent definition
[[platform.agents]]
name = "coordinator"
system_prompt = "Coordinate subtasks."

# Agent-specific plugin configuration overrides
[platform.agents.plugins.MarkdownMemoryPlugin]
maxims = ["soul", "rules"]
```

---

## The Harness & Plugin Lifecycle

The [AgentHarness](file:///home/jzhang/bos-ai/src/bos/core/harness.py) owns the lifecycle of plugins, instantiating them, configuring global resources on startup, and cleaning up resources on shutdown.

### 1. The `HarnessPlugin` & `BoundPlugin` Split
To support thread-safety and agent isolation, plugins implement a factory pattern via `.bind()`:
* **`HarnessPlugin` (Harness Scope / Singleton)**: Manages global, heavy resources (e.g. database connections) and executes startup registrations.
* **`BoundPlugin` (Agent Scope / Instance)**: Created per-agent, holding agent-specific configuration (like active maxim lists).

```python
from typing import Protocol, Any
from bos.core.contract import TurnInterceptor, TurnContext

class HarnessPlugin(Protocol):
    @property
    def name(self) -> str:
        """Unique plugin registration name (e.g. 'MarkdownMemoryPlugin')."""
        ...

    async def setup(self, harness: "AgentHarness") -> None:
        """Called on harness startup to register tools and init pools."""
        ...

    async def teardown(self) -> None:
        """Called on harness shutdown to clean up resources."""
        ...

    def bind(self, config: dict[str, Any]) -> "BoundPlugin":
        """Instantiate an agent-specific bound runner with custom config."""
        ...

class BoundPlugin(Protocol):
    @property
    def name(self) -> str: ...

    def get_tools(self) -> list[str]:
        """Return the names of ep_tool registrations activated by this plugin."""
        ...

    async def get_system_prompt(self, context: TurnContext) -> str | None:
        """Return prompt instructions to inject into the system prompt."""
        ...

    def get_interceptors(self) -> list[TurnInterceptor]:
        """Return turn interceptors to execute during loop execution."""
        ...
```

### 2. Harness Assembly
When compiling an agent, the harness binds registered plugins using the resolved agent configuration:

```python
class AgentHarness:
    def create_agent(self, name: str, agent_spec: dict[str, Any]) -> Agent:
        plugins_config = agent_spec.get("plugins", {})
        
        bound_plugins = [
            plugin.bind(plugins_config.get(plugin.name, {}))
            for plugin in self.registered_plugins
        ]
        
        # Inject bound plugins into the agent creation
        return ep_agent.invoke(name, agent_spec | {"plugins": bound_plugins})
```

---

## Dynamic Tool Registration & Delegation

To prevent polluting the global namespace, tools are registered dynamically to [ep_tool](file:///home/jzhang/bos-ai/src/bos/core/contract.py#L20) during the harness initialization phase.

```python
from bos.core.contract import Extension, ep_tool

class MarkdownMemoryPlugin:
    async def setup(self, harness: AgentHarness) -> None:
        ep_tool.register(Extension(
            name="Remember",
            fn=self.tool_remember,
            description="Store a fact in episodic memory.",
            metadata={"parameters": {...}}
        ))

    async def tool_remember(
        self,
        content: str,
        agent_context: AgentContext | None = None,
    ) -> str:
        if not agent_context:
            return "Error: Agent context is unavailable."
            
        # Delegate tool execution to the agent-bound plugin instance
        bound_plugin = agent_context.get_plugin(self.name)
        return await bound_plugin.remember(content)
```

---

## The `AgentContext` Protocol

The `AgentContext` is constructed by the agent loop every turn and injected into tool signatures. It provides the tool with metadata and access to the active agent-bound plugins:

```python
from typing import Protocol, Any

class AgentContext(Protocol):
    @property
    def agent_name(self) -> str: ...
    @property
    def chat_id(self) -> str: ...
    @property
    def turn_id(self) -> str: ...

    def get_plugin(self, name: str) -> Any | None:
        """Locate a bound plugin instance by name."""
        ...
```

---

## Agent Permission & Execution

The [ReActAgent](file:///home/jzhang/bos-ai/src/bos/core/agent.py#L181) has **zero built-in tools**. It relies on active plugins to supply capabilities, while remaining the ultimate filter for tool permissions:

### 1. Active Tools Resolution
```python
class ReactAgent:
    def __init__(self, plugins: list[BoundPlugin], allowed_tools: list[str] = None, exclude_tools: list[str] = None):
        self._plugins = plugins
        self._allowed_tools = set(allowed_tools or [])
        self._exclude_tools = set(exclude_tools or [])

    def _get_active_tools(self) -> set[str]:
        active_tools = set()
        
        # 1. Start with configured allowed tools (or default to all registered tools)
        if self._allowed_tools:
            active_tools.update(self._allowed_tools)
        else:
            active_tools.update(ep_tool.describe().keys())

        # 2. Add all tools exposed by active plugins
        for plugin in self._plugins:
            active_tools.update(plugin.get_tools())

        # 3. Filter out any excluded tools
        active_tools.difference_update(self._exclude_tools)
        return active_tools
```

### 2. Prompt Compilation
```python
async def _build_system_prompt(self, ctx: TurnContext) -> str:
    sections = [self._prompt_section_base()]
    
    # Collect custom sections from all active plugins in order
    for plugin in self._plugins:
        if prompt := await plugin.get_system_prompt(ctx):
            sections.append(prompt)
            
    sections.append(self._prompt_section_system_info())
    return "\n\n".join(s for s in sections if s)
```

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-20 | Initial draft (BEP 4) | Formulate the design for decoupling agent-scoped tools from ReActAgent to make tools modular, customizable, and testable. |
| 2026-05-20 | Plugin Micro-kernel Evolution | Revise the design to treat memory and tasks as cohesive plugins, separating harness/agent boundaries via factory binding and pass-through configuration. |
