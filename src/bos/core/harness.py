from __future__ import annotations

import asyncio
import copy
import logging
import os
from pathlib import Path
from typing import Any

from ._chat_store_utils import make_internal_chat_id
from ._utils import (
    _aclose,
    _allowed,
    _apply,
    _deep_merge,
    _pick_collection,
)
from .agent import AbortTurn, Agent, AgentPort, ExternalRuntime, StructuredValidator, TurnContext
from .contract import (
    AgentPlugin,
    AgentResult,
    ChatStore,
    Consolidator,
    EventBus,
    HarnessPlugin,
    InterceptorStage,
    MailRoute,
    MessageContent,
    ParentTurn,
    PluginServices,
    ToolAttributes,
    TurnInterceptor,
    ep_chat_store,
    ep_consolidator,
    ep_mail_route,
    ep_plugin,
    ep_tool,
    ep_turn_interceptor,
)
from .llm import LLMClient
from .registry import ExtensionPoint, ToolRegistry
from .sinks import derive_event_sink

logger = logging.getLogger(__name__)

_structured_validator_singleton: StructuredValidator | None = None


def _default_structured_validator() -> StructuredValidator:
    """The default (jsonschema-backed) structured-output validator injected into
    every agent (BEP 12) — ``Agent`` and, as of BEP 19 §3.9, every vendor
    runtime built by ``create_agent`` too, so ``schema=`` validates the same
    way regardless of which kind of agent ran the turn. Lazily imported so the
    agent ring stays stdlib-pure and the third-party dep is only pulled when an
    agent is actually built."""
    global _structured_validator_singleton
    if _structured_validator_singleton is None:
        from bos.core.defaults.structured_validator import JsonSchemaValidator

        _structured_validator_singleton = JsonSchemaValidator()
    return _structured_validator_singleton


# BEP 19 §3.2. Kind → "module:Class", not the class itself: this module is
# imported on a base install with no extras, so importing a vendor SDK here
# would break `import bos.sdk`. Resolved on first build, per kind.
EXTERNAL_AGENT_KINDS: dict[str, str] = {
    "claude-code": "bos.extensions.runtimes.claude_code:ClaudeCodeAgent",
    "codex": "bos.extensions.runtimes.codex:CodexAgent",
}

EXTERNAL_RUNTIME_EXTRAS: dict[str, str] = {"claude-code": "claude-code", "codex": "codex"}

# The vendor package each runtime module imports. Used to tell "the extra is not
# installed" from "the runtime module itself is broken" — both arrive as
# ImportError, and only the first should point at `pip install`. `claude-code`
# has no entry yet: until its runtime module exists, any ImportError under that
# kind reports its real cause rather than guessing.
EXTERNAL_RUNTIME_VENDOR_MODULES: dict[str, str] = {"codex": "openai_codex"}


def _load_external_runtime(runtime: str) -> type[ExternalRuntime]:
    """Import a runtime class by dotted path, reporting what failed on ImportError.

    The failure isn't necessarily a missing extra — it could be an import
    failing inside a runtime module that *is* installed (a typo'd import, a
    broken transitive dependency, a renamed symbol after a vendor version
    bump). Only point at `pip install` when the module or submodule that
    actually failed to import is the vendor's own; otherwise report the
    real cause.
    """
    import importlib

    module_path, _, class_name = EXTERNAL_AGENT_KINDS[runtime].partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        vendor = EXTERNAL_RUNTIME_VENDOR_MODULES.get(runtime)
        missing = getattr(exc, "name", "") or ""
        # ModuleNotFoundError means the module/submodule itself could not be
        # found — that's "the extra isn't installed". A plain ImportError
        # (e.g. `from openai_codex import Renamed` after a vendor rename)
        # can carry the same .name — the *package* — even though the package
        # was found and imported fine; only something *inside* it is wrong,
        # which is a real bug, not a missing extra.
        vendor_missing = (
            isinstance(exc, ModuleNotFoundError)
            and vendor is not None
            and (missing == vendor or missing.startswith(f"{vendor}."))
        )
        if vendor_missing:
            extra = EXTERNAL_RUNTIME_EXTRAS.get(runtime, runtime)
            raise RuntimeError(
                f"The {runtime!r} agent runtime needs its optional dependency. "
                f"Install it with: pip install 'bos-ai[{extra}]'"
            ) from exc
        raise RuntimeError(f"Could not load the {runtime!r} agent runtime: {exc}") from exc
    return getattr(module, class_name)


class AgentRegistry:
    _registry: dict[str, dict[str, Any]] = {}

    @classmethod
    def register(cls, name: str, description: str | None = None, **kwargs):
        if "tools" not in kwargs:
            kwargs["tools"] = []
        elif (tools := kwargs["tools"]) is not None and not isinstance(tools, list):
            raise TypeError(f"tools must be a list or None, got {type(tools).__name__}")

        plugins = kwargs.get("plugins")
        if plugins is None:
            kwargs["plugins"] = {"enabled": [], "disabled": []}
        elif not isinstance(plugins, dict):
            raise TypeError(f"plugins must be a dict or None, got {type(plugins).__name__}")

        kwargs.setdefault("tools", [])
        kwargs.setdefault("plugins", {"enabled": [], "disabled": []})
        kwargs["kind"] = name
        cls._registry[name] = {
            "defaults": kwargs,
            "description": description or "",
        }

    @classmethod
    def clear(cls) -> None:
        """Drop every registration.

        The registry is rebuilt in full by ``Workspace.bootstrap_platform``
        from ``ep_agent`` factories plus ``config.agents``, which is its only
        writer — so clearing first is what makes a re-bootstrap *replace* the
        set rather than accumulate into it. Without this a hot restart keeps
        agents whose definitions are gone (BEP 17 §3.5.4).
        """
        cls._registry.clear()

    @classmethod
    def has_registered(cls, name: str) -> bool:
        return name in cls._registry

    @classmethod
    def get_defaults(cls, name: str) -> dict[str, Any]:
        entry = cls._registry.get(name)
        return entry["defaults"] if entry else {}

    @classmethod
    def describe(cls) -> dict[str, str]:
        return {name: entry["description"] for name, entry in cls._registry.items()}


def _resolve_agent_cfg_parent(kind: str | None, agent_cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve a ``_parent`` that arrives in ``agent_cfg`` (BEP 19 §3.4.1.1).

    ``agent_cfg`` bypasses the workspace resolver, so a ``_parent`` here used to
    be dropped without a word — ``_apply`` filters it out of ``Agent``'s kwargs
    — and a caller asking for a Codex agent got a BOS one, its ``permission``
    ignored. The parent is looked up where the resolver left its work: a
    registered agent's ``AgentRegistry`` defaults already hold its whole
    resolved chain, so deep-merging them under ``agent_cfg`` gives what the same
    settings under ``[agents.<name>]`` with that ``_parent`` would — given in
    ``agent_cfg``'s own shape, which is the harness's argument shape rather than
    TOML's (``tools`` is a list, not a ``[tools]`` table). A reserved runtime
    is a parent even with no ``[agents.<runtime>]`` table, seeded with the
    marker ``_EXTERNAL_RUNTIME_SPECS`` writes. A ``None`` parent is no parent, as
    in config.

    Refused: a parent that is neither; a ``kind`` that is already registered,
    whose defaults already fold in a lineage of their own, so a second parent has
    no defined place in the merge; and a parent whose runtime contradicts the one
    ``kind`` or an ``external_runtime`` key (``None`` included) names.
    """
    cfg = dict(agent_cfg)
    parent = cfg.pop("_parent")
    if parent is None:
        return cfg
    if kind is not None and AgentRegistry.has_registered(kind):
        raise ValueError(
            f"agent_cfg sets `_parent = {parent!r}` for {kind!r}, which is already registered with a lineage of "
            f"its own; agent_cfg cannot give it another. Build the variant under a new name."
        )
    reserved = isinstance(parent, str) and parent in EXTERNAL_AGENT_KINDS
    registered = isinstance(parent, str) and AgentRegistry.has_registered(parent)
    if not (reserved or registered):
        known = ", ".join(sorted({*EXTERNAL_AGENT_KINDS, *AgentRegistry.describe()}))
        raise ValueError(
            f"agent_cfg sets `_parent = {parent!r}`, which is neither a reserved runtime nor a registered "
            f"agent. Known: {known}."
        )
    base: dict[str, Any] = {"external_runtime": parent} if reserved else {}
    if registered:
        inherited = copy.deepcopy(AgentRegistry.get_defaults(parent))
        # `kind` in registered defaults is the parent's own name. Per-agent plugin
        # state is keyed by `agent_name or kind` (`_bind_plugins_for_agent`), so
        # replace it with the child's, as `register()` does for a config child;
        # dropping it would key every child without an `agent_name` as "default".
        # An `agent_name` the parent sets is inherited, on this route as in config.
        inherited.pop("kind", None)
        if kind is not None:
            inherited["kind"] = kind
        base = _deep_merge(base, inherited)
    runtime = base.get("external_runtime")
    resolves_to = f"the {runtime!r} runtime" if runtime else "a BOS agent"
    if kind in EXTERNAL_AGENT_KINDS and kind != runtime:
        raise ValueError(
            f"{kind!r} is the {kind!r} runtime, but `_parent = {parent!r}` resolves to {resolves_to}."
        )
    if "external_runtime" in cfg and cfg["external_runtime"] != runtime:
        raise ValueError(
            f"agent_cfg sets `external_runtime = {cfg['external_runtime']!r}`, but `_parent = {parent!r}` "
            f"resolves to {resolves_to}."
        )
    return _deep_merge(base, cfg)


class ResolvedToolSet:
    """A live, filtered view over one or more source tool collections.

    Satisfies the core ``ToolSet`` protocol. This is where tool *resolution*
    lives (outer layer): merge of sources with earlier sources taking
    precedence, plus include/exclude policy. The Agent receives the resolved
    view and stays ignorant of registries, globals, and filtering.
    """

    def __init__(
        self,
        sources: list[ToolRegistry],
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> None:
        # Precedence: earlier sources win on name conflicts (local before global).
        self._sources = sources
        self._include = include
        self._exclude = exclude

    def has(self, name: str) -> bool:
        return _allowed(name, self._include, self._exclude) and any(s.has(name) for s in self._sources)

    def _merged(self, get: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for source in reversed(self._sources):  # later sources are lower precedence
            merged |= getattr(source, get)()
        return _pick_collection(merged, self._include, self._exclude)

    def to_openai_schema(self) -> dict[str, dict[str, Any]]:
        return self._merged("to_openai_schema")

    def describe_usage(self) -> dict[str, str]:
        return self._merged("describe_usage")

    def attributes(self, name: str) -> ToolAttributes:
        # Adapt the registry's raw (untyped) metadata into core's typed,
        # core-owned ToolAttributes.
        for source in self._sources:
            if source.has(name):
                metadata = source.metadata_for(name)
                return ToolAttributes(parallel_safe=bool(metadata.get("parallel_safe", False)))
        return ToolAttributes()

    async def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> str:
        if not _allowed(name, self._include, self._exclude):
            raise Exception(f"Tool {name} is not allowed")
        for source in self._sources:
            if source.has(name):
                return await source.invoke(name, kwargs)
        raise Exception(f"Tool {name} not found")


class ChainInterceptor:
    """Runs a sequence of resolved interceptor instances in the given order.

    Resolution of interceptor names/configs into instances is the outer layer's
    job; this only runs what it is handed.
    """

    def __init__(self, interceptors: list[TurnInterceptor] | None = None) -> None:
        self._interceptors = list(interceptors or [])

    async def aclose(self) -> None:
        for interceptor in self._interceptors:
            await _aclose(interceptor)

    async def intercept(self, stage: InterceptorStage, context: TurnContext) -> None:
        for interceptor in self._interceptors:
            await interceptor.intercept(stage, context)


class _CompositePluginInterceptor:
    """Runs plugin interceptors (best-effort), then a fallback interceptor chain.

    Plugin interceptors are best-effort: a failing one is logged and skipped so
    a buggy plugin cannot crash the turn. AbortTurn still propagates, and the
    configured fallback chain runs with normal error propagation.
    """

    def __init__(self, plugin_interceptors: list[TurnInterceptor], fallback: TurnInterceptor) -> None:
        self._plugin = plugin_interceptors
        self._fallback = fallback

    async def aclose(self) -> None:
        for interceptor in self._plugin:
            await _aclose(interceptor)
        await _aclose(self._fallback)

    async def intercept(self, stage: InterceptorStage, context: TurnContext) -> None:
        for interceptor in self._plugin:
            try:
                await interceptor.intercept(stage, context)
            except AbortTurn:
                raise
            except Exception as e:
                logger.error("Error in plugin interceptor: %s", e, exc_info=True)
        await self._fallback.intercept(stage, context)


class _PluginPromptProvider:
    """Builds per-turn system-prompt sections from the agent's plugins, in order.

    Satisfies the core ``PromptProvider`` protocol; the Agent asks it each turn
    and stays unaware of plugins.
    """

    def __init__(self, plugins: list[AgentPlugin]) -> None:
        self._plugins = plugins

    async def sections(self, context: TurnContext) -> list[str]:
        out: list[str] = []
        for plugin in self._plugins:
            try:
                section = await plugin.get_system_prompt_section(context)
            except Exception as e:
                logger.error("Error in plugin prompt section %s: %s", plugin.name, e, exc_info=True)
                continue
            if section:
                out.append(section)
        return out


class _HarnessAgentRunner:
    """Adapter implementing :class:`AgentRunner` over AgentHarness internals (BEP 12).

    The single way a plugin/tool spins up a disposable agent. ``parent`` is
    optional: on-turn callers (the AskSubagent tool) pass it so the child chat
    nests under the parent and the event sink is parented; off-turn callers
    (the memory consolidator) omit it and get a standalone
    internal chat-id with no parent sink. Either way the disposable agent has a
    fresh chat-id, so there is no chat history and no compaction recursion.
    """

    def __init__(self, harness: AgentHarness) -> None:
        self._harness = harness

    async def run(
        self,
        message: MessageContent,
        *,
        kind: str | None = None,
        agent_cfg: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
        parent: ParentTurn | None = None,
        model: str | None = None,
    ) -> AgentResult:
        agent = await self._harness.create_agent(kind, agent_cfg)
        tag = kind or "agent"
        if parent is not None:
            child_chat_id = make_internal_chat_id(tag, parent.chat_id)
            child_event_sink = derive_event_sink(
                parent.event_sink,
                parent_turn_id=parent.turn_id,
                parent_chat_id=parent.chat_id,
                parent_agent_name=parent.agent_name,
            )
            ctx_metadata: dict[str, Any] = {"subagent": tag, "ref_chat_id": parent.chat_id}
        else:
            child_chat_id = make_internal_chat_id(tag)
            child_event_sink = None
            ctx_metadata = {"subagent": tag}
        return await agent.run(
            child_chat_id,
            message,
            schema=schema,
            ctx_metadata=ctx_metadata,
            event_sink=child_event_sink,
            llm_args={"model": model} if model else None,
        )


class AgentHarness:
    """Lifecycle-owning container for shared agent services."""

    def __init__(
        self,
        *,
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
        consolidator: str = "LLMConsolidator",
        chat_store: str = "JsonlChatStore",
        mail_route: str = "JsonlMailRoute",
        interceptors: list[str | dict[str, Any]] | None = None,
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._consolidator_impl = consolidator
        self._chat_store_impl = chat_store
        self._mail_route_impl = mail_route
        self._interceptors_impl = interceptors or []

        self._owned: list[Any] = []
        self._active: bool = False
        self.mail_route: MailRoute | None = None
        self.chat_store: ChatStore | None = None
        self.consolidator: Consolidator | None = None
        self.interceptor: ChainInterceptor | None = None
        self.llm: LLMClient | None = None
        self.events: EventBus | None = None

        # Plugin state
        self._harness_plugins: dict[str, HarnessPlugin] = {}
        self._plugin_services: PluginServices | None = None

        # Per-chat compaction locks (BEP 5)
        self._compaction_locks: dict[str, asyncio.Lock] = {}

        # External-runtime loopback MCP server (BEP 19 §3.8), started lazily
        # by _ensure_tool_mcp_server on first use.
        self._tool_mcp_server: Any = None

    async def __aenter__(self):
        if self._active:
            raise RuntimeError("AgentHarness is already active; do not re-enter the same instance.")

        # The assembly ring registers its own built-in adapters (LLMConsolidator,
        # litellm provider, JsonlChatStore/JsonlMailRoute) — the
        # harness depends on them being resolvable by name below, so it does not rely
        # on an outer ring (``bos.exts``) having imported them. Idempotent; deferred to
        # open-time to avoid import-order coupling during ``bos.core`` package init.
        import bos.core.defaults  # noqa: F401

        self.mail_route = await self._create_and_own(ep_mail_route, self._mail_route_impl)
        self.chat_store = await self._create_and_own(ep_chat_store, self._chat_store_impl)
        assert self.chat_store is not None  # ep_chat_store has a built-in, so creation never returns None
        self.llm = LLMClient()
        self.consolidator = await self._create_consolidator()
        self.interceptor = ChainInterceptor(await self._resolve_interceptors(self._interceptors_impl))

        # In-process EventBus: the session-lifecycle fan-out plugins subscribe to.
        from bos.core.defaults.eventbus import DefaultEventBus

        self.events = DefaultEventBus()

        # Build plugin services
        self._plugin_services = PluginServices(
            bos_dir=self._bos_root,
            workspace=self._workspace,
            llm=self.llm,
            chat_store=self.chat_store,
            consolidator=self.consolidator,
            events=self.events,
            agent_runner=_HarnessAgentRunner(self),
        )

        self._active = True
        return self

    async def __aexit__(self, *exc) -> None:
        await _aclose(self.interceptor)
        # Teardown harness plugins in reverse setup order
        for hp in reversed(list(self._harness_plugins.values())):
            try:
                await hp.teardown()
            except Exception:
                logger.error("Error tearing down plugin %r", hp.name, exc_info=True)
        self._harness_plugins.clear()
        for resource in reversed(self._owned):
            await _aclose(resource)
        self._owned.clear()
        self._tool_mcp_server = None

        self._active = False

    async def create_agent(
        self,
        kind: str | None = None,
        agent_cfg: dict[str, Any] | None = None,
    ) -> AgentPort:
        if not self._active:
            raise RuntimeError("create_agent must be called within an active AgentHarness context.")

        # Resolve agent defaults from AgentRegistry so plugin config is visible
        agent_defaults: dict[str, Any] = {}
        if kind and AgentRegistry.has_registered(kind):
            agent_defaults = AgentRegistry.get_defaults(kind)

        if not any([kind, agent_cfg]) and not agent_defaults:
            agent_cfg = {
                "system_prompt": "You are a helpful assistant.",
                "tools": [],
            }

        if agent_cfg and "_parent" in agent_cfg:
            agent_cfg = _resolve_agent_cfg_parent(kind, agent_cfg)

        # Deep-copy the defaults: _deep_merge mutates its base in place, and a
        # shallow copy would let per-agent overrides write through the shared
        # nested dicts into the registry's stored defaults.
        merged_cfg = _deep_merge(copy.deepcopy(agent_defaults), agent_cfg or {})

        # BEP 19 §3.2. Dispatch on the reserved name, or on the external_runtime
        # an agent inherited from one via _parent. Before plugin binding: none of
        # plugins, the local ToolRegistry, ResolvedToolSet, the composite
        # interceptor or the prompt provider applies to a runtime that owns its
        # own tool loop.
        runtime = kind if kind in EXTERNAL_AGENT_KINDS else merged_cfg.get("external_runtime")
        if runtime is not None:
            if runtime not in EXTERNAL_AGENT_KINDS:
                known = ", ".join(sorted(EXTERNAL_AGENT_KINDS))
                raise RuntimeError(f"Unknown external runtime {runtime!r}. Known: {known}.")
            external = _load_external_runtime(runtime)(
                kind=kind or runtime,
                cfg=merged_cfg,
                chat_store=self.chat_store,
                workspace=self._workspace,
                mcp=self._ensure_tool_mcp_server,
                structured_validator=_default_structured_validator(),
            )
            self._owned.append(external)
            return external

        agent_name = kind or merged_cfg.get("kind") or "undef"
        plugins = await self._bind_plugins_for_agent(merged_cfg)

        # Resolve this agent's tools (outer layer's job): register plugin tools
        # into an agent-local registry, then expose a filtered view over
        # [local, global] honoring the agent's include/exclude config.
        local_tools = ToolRegistry(f"_local_tools:{agent_name}", "Agent-scoped local tools.")
        for plugin in plugins:
            plugin.register_tools(local_tools)
        tools = ResolvedToolSet(
            [local_tools, ep_tool],
            include=merged_cfg.get("tools"),
            exclude=merged_cfg.get("exclude_tools"),
        )

        # Assemble this agent's single interceptor (outer layer's job): plugin
        # interceptors (best-effort) ahead of the configured/workspace chain.
        plugin_interceptors = [i for plugin in plugins for i in plugin.get_interceptors()]
        interceptor = _CompositePluginInterceptor(plugin_interceptors, self.interceptor or ChainInterceptor())

        kwargs = merged_cfg | {
            "kind": agent_name,
            "llm": self.llm,
            "chat_store": self.chat_store,
            "consolidator": self.consolidator,
            "tools": tools,
            "interceptor": interceptor,
            "prompt_provider": _PluginPromptProvider(plugins),
            "chat_compaction_lock": self._get_compaction_lock,
            "workspace": str(self._workspace),
            "structured_validator": _default_structured_validator(),
        }

        agent: Agent = _apply(Agent, kwargs)
        return agent

    async def _bind_plugins_for_agent(
        self,
        agent_cfg: dict[str, Any],
    ) -> list[AgentPlugin]:
        """Resolve, validate, and bind enabled plugins for an agent.

        Uses the BEP6 flat-list plugin model: plugins.enabled / plugins.disabled
        from the agent config, with plugin-bindings.<Name> for per-plugin settings.
        """
        plugins_cfg = agent_cfg.get("plugins", {})
        if not isinstance(plugins_cfg, dict):
            return []

        enabled = plugins_cfg.get("enabled", [])
        disabled = plugins_cfg.get("disabled", [])
        if not isinstance(enabled, (list, tuple)):
            return []
        if not isinstance(disabled, (list, tuple)):
            disabled = []

        enabled_names = list(enabled)
        if "*" in enabled_names:
            enabled_names = [n for n in ep_plugin._extensions.keys() if n not in disabled]

        bindings = agent_cfg.get("plugin-bindings", {})
        if hasattr(bindings, "model_dump"):
            bindings = bindings.model_dump()

        bound: list[AgentPlugin] = []
        for pname in enabled_names:
            if pname in disabled:
                continue
            hp = self._harness_plugins.get(pname)
            if hp is None and ep_plugin.has(pname):
                hp = await self._instantiate_and_setup_plugin(pname)
                self._harness_plugins[pname] = hp

            if hp is None:
                logger.warning("Unknown plugin %r; skipping.", pname)
                continue

            plugin_binding = bindings.get(pname, {})
            cfg = dict(hp.default_config()) | (plugin_binding if isinstance(plugin_binding, dict) else {})
            # Inject the resolved agent identity so per-actor plugins
            # (e.g. MemoryPlugin) can key state by it. Mirrors Agent's
            # `self._name = agent_name or kind` resolution (agent.py:345).
            cfg["agent_name"] = agent_cfg.get("agent_name") or agent_cfg.get("kind") or "default"
            hp.validate_config(cfg)
            try:
                agent_plugin = hp.bind(cfg)
            except Exception:
                logger.error(
                    "Failed to bind plugin %r for agent %r",
                    pname,
                    cfg["agent_name"],
                    exc_info=True,
                )
                raise
            bound.append(agent_plugin)

        return bound

    async def _instantiate_and_setup_plugin(self, plugin_name: str) -> HarnessPlugin:
        """Instantiate a harness plugin provider from ep_plugin and run setup."""
        instance = await ep_plugin.invoke(plugin_name, {})
        if not isinstance(instance, HarnessPlugin):
            raise TypeError(f"Plugin {plugin_name} does not implement HarnessPlugin")
        if self._plugin_services is not None:
            await instance.setup(self._plugin_services)
        return instance

    async def _create_and_own(self, ep: ExtensionPoint, impl: str) -> Any:
        """Instantiate the *impl* implementation of extension point *ep* and track
        it for teardown. *impl* is always an explicit, registered name (the harness
        defaults to the built-in adapter's name), so the invoke resolves a real
        instance rather than relying on an implicit fallback."""
        context = {"bos_dir": str(self._bos_root), "workspace_dir": str(self._workspace)}
        instance = await ep.invoke(impl, context)
        if instance is not None:
            self._owned.append(instance)
        return instance

    async def _create_consolidator(self) -> Consolidator:
        cfg = {"model": os.getenv("BOS_CONSOLIDATOR_MODEL"), "llm": self.llm}
        instance = await ep_consolidator.invoke(self._consolidator_impl, cfg)
        if instance is None:
            raise RuntimeError(f"Consolidator extension {self._consolidator_impl!r} could not be created")
        self._owned.append(instance)
        return instance

    async def _resolve_interceptors(self, configs: list[str | dict[str, Any]]) -> list[TurnInterceptor]:
        """Resolve interceptor names/configs into instances via ep_turn_interceptor.

        A name that is not registered is skipped; one that fails to instantiate
        is logged and skipped — a bad interceptor never breaks the chain.
        """
        resolved: list[TurnInterceptor] = []
        for entry in configs:
            cfg = {"name": entry} if isinstance(entry, str) else dict(entry)
            name = cfg.pop("name", None)
            if not name or not ep_turn_interceptor.has(name):
                continue
            try:
                interceptor = await ep_turn_interceptor.invoke(name, cfg)
                if interceptor is not None:
                    resolved.append(interceptor)
            except Exception as e:
                logger.error("Failed to create interceptor %s: %s", name, e)
        return resolved

    def _get_compaction_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._compaction_locks:
            self._compaction_locks[chat_id] = asyncio.Lock()
        return self._compaction_locks[chat_id]

    def _ensure_tool_mcp_server(self) -> Any:
        """The loopback MCP tool server, started on first use (BEP 19 §3.8).

        Lazy: a harness whose agents expose no tools never builds one, and never
        binds a port. Registered in ``_owned`` so it closes with the harness.
        The accessor is sync and ``start()`` is async, so the runtime awaits
        ``start()`` itself on first use; ``start()`` is idempotent.
        """
        if not self._active:
            # Same guard as create_agent: a runtime object a host still holds past
            # harness teardown must not be able to build a fresh server into a
            # cleared _owned, whose listener nothing would ever close.
            raise RuntimeError("_ensure_tool_mcp_server must be called within an active AgentHarness context.")
        if self._tool_mcp_server is None:
            # Resolved by dotted path, like EXTERNAL_AGENT_KINDS above and for the
            # same two reasons: the assembly ring never names an outer ring at
            # import time (BEP 13 §3.1), and this module is imported on a base
            # install that has neither mcp nor uvicorn.
            import importlib

            module = importlib.import_module("bos.extensions.runtimes.mcp_egress")
            self._tool_mcp_server = module.BosToolMcpServer()
            # insert(0), not append: __aexit__ closes reversed(_owned), so index 0
            # closes last — after every external runtime, which may still be
            # calling a tool through this server as it shuts down. A runtime that
            # asks for the server lazily lands *after* itself in _owned, so an
            # append would tear the server down first. Do not "tidy" this.
            self._owned.insert(0, self._tool_mcp_server)
        return self._tool_mcp_server
