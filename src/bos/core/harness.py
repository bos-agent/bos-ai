from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from ._utils import (
    _aclose,
    _apply,
    _create_extension_instance,
    _deep_merge,
    _load_ext_modules,
    _load_ext_paths,
    _safe_format,
)
from .agent import Agent, ChainInterceptor
from .contract import (
    AgentPlugin,
    ChatStore,
    Consolidator,
    HarnessPlugin,
    MailBox,
    MailRoute,
    PluginServices,
    ToolContext,
    ep_plugin,
)
from .defaults import default_agent_spec
from .events import derive_event_sink
from .llm import LLMClient

logger = logging.getLogger(__name__)


class AgentRegistry:
    _registry: dict[str, dict[str, Any]] = {}

    @classmethod
    def register(cls, name: str, description: str | None = None, **kwargs):
        _CAPABILITY_KEYS = ("tools",)

        def map_value(key, value):
            if isinstance(value, (list, dict)):
                return value
            if value is None:
                return []  # mute the capability
            if value == "*":
                return None  # fully open the capability
            raise TypeError(f"{key} must be a list, '*', or None")

        for key in _CAPABILITY_KEYS:
            kwargs[key] = map_value(key, kwargs.get(key))

        kwargs["kind"] = name
        cls._registry[name] = {
            "defaults": kwargs,
            "description": description or "",
        }

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


def bootstrap_platform(
    bos_dir: str | Path,
    envs: dict[str, str] | None = None,
    envfile: str | None = None,
    extensions: list[str] | None = None,
    agents: list[dict[str, Any]] | None = None,
    agent_defaults: dict[str, Any] | None = None,
) -> None:
    bos_root = Path(bos_dir).expanduser().resolve()
    bos_root.mkdir(parents=True, exist_ok=True)

    if envs:
        os.environ.update({k: str(v) for k, v in envs.items()})
    if envfile:
        from dotenv import load_dotenv

        load_dotenv((bos_root / Path(envfile).expanduser()).resolve(), override=True)

    if extensions:
        modules, paths = [], []
        for ext in extensions:
            p = bos_root / Path(ext).expanduser()
            if p.exists():
                paths.append(p)
            else:
                modules.append(ext)
        if modules:
            _load_ext_modules(modules=modules)
        if paths:
            _load_ext_paths(paths=paths)

    defaults = agent_defaults or {}

    # the agent defaults in configuration takes precedence over the spec in the fallback _default agent
    _default_spec = default_agent_spec | defaults
    # the specifice agent definition takes precedence over the agent_defaults
    agent_specs = [defaults | spec for spec in (agents or [])]
    # register all the agents
    for agent_spec in [_default_spec] + agent_specs:
        AgentRegistry.register(**(agent_spec))

    # prevent the litellm to call load_dotenv automatically, and supress logs.
    os.environ["LITELLM_MODE"] = "extension"
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)


CURRENT_HARNESS: contextvars.ContextVar[AgentHarness] = contextvars.ContextVar("current_harness")
CURRENT_MAILBOX: contextvars.ContextVar[MailBox] = contextvars.ContextVar("current_mailbox")


def _resolve_plugin_configs(
    platform_plugins: dict[str, dict[str, Any]],
    agent_plugins: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    all_names = set(platform_plugins.keys()) | set(agent_plugins.keys())
    for name in all_names:
        merged = dict(platform_plugins.get(name, {}))
        agent_cfg = agent_plugins.get(name, {})
        if isinstance(agent_cfg, dict):
            _deep_merge(merged, agent_cfg)
        result[name] = merged
    return result


def _resolve_enabled_plugin_names(
    global_enabled: list[str],
    agent_plugins: dict[str, Any],
) -> list[str]:
    """Determine ordered list of enabled plugin names for an agent."""
    resolved: list[str] = []
    for name in global_enabled:
        agent_cfg = agent_plugins.get(name, {})
        disabled = isinstance(agent_cfg, dict) and agent_cfg.get("enabled") is False
        if not disabled:
            resolved.append(name)

    for name in agent_plugins:
        if name not in resolved:
            cfg = agent_plugins[name]
            if isinstance(cfg, dict) and cfg.get("enabled") is True:
                resolved.append(name)

    return resolved


class _HarnessSubagentRuntime:
    """Adapter that implements SubagentRuntime using AgentHarness internals."""

    def __init__(self, harness: AgentHarness) -> None:
        self._harness = harness

    async def ask(self, role: str, message: str, *, parent: ToolContext) -> str:
        subagent_cfg = self._harness._get_subagent_config(role)
        if task_template := subagent_cfg.get("task_template"):
            message = _safe_format(
                task_template,
                task=message,
                message=message,
                role=role,
                workspace=self._harness.workspace,
            )

        child_chat_id = self._harness._make_subagent_chat_id(parent.chat_id, role)
        child_agent_cfg = {k: v for k, v in subagent_cfg.items() if k not in {"name", "task_template"}}
        agent = await self._harness.create_agent(role, child_agent_cfg)
        child_event_sink = derive_event_sink(
            parent.event_sink,
            parent_turn_id=parent.turn_id,
            parent_chat_id=parent.chat_id,
            parent_agent_name=parent.agent_name,
        )
        return await agent.ask(
            child_chat_id,
            message,
            ctx_metadata={"subagent": role, "ref_chat_id": parent.chat_id},
            event_sink=child_event_sink,
        )


class AgentHarness:
    """Lifecycle-owning container for shared agent services."""

    def __init__(
        self,
        *,
        mail_route: dict[str, Any] | None = None,
        chat_store: dict[str, Any] | None = None,
        consolidator: dict[str, Any] | None = None,
        providers: dict[str, dict[str, Any]] | None = None,
        interceptors: list[str | dict[str, Any]] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
        subagent_defaults: dict[str, Any] | None = None,
        subagents: list[dict[str, Any]] | None = None,
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
        enabled_plugins: list[str] | None = None,
        platform_plugins: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._subagent_defaults = subagent_defaults or {}
        self._subagents_cfg = {cfg["name"]: cfg for cfg in subagents} if subagents else {}

        self._mail_route_cfg = mail_route
        self._chat_store_cfg = chat_store
        self._consolidator_cfg = consolidator
        self._providers_cfg = providers
        self._interceptors_cfg = interceptors
        self._tools_cfg = tools or {}
        self._enabled_plugins = enabled_plugins or []
        self._platform_plugins = platform_plugins or {}

        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self.mail_route = None
        self.chat_store = None
        self.consolidator = None
        self.interceptor = None
        self.llm = None

        # Plugin state
        self._harness_plugins: dict[str, HarnessPlugin] = {}
        self._plugin_services: PluginServices | None = None

        # Per-chat compaction locks (BEP 5)
        self._compaction_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self):
        if self._token is not None:
            raise RuntimeError(
                "AgentHarness is already active. Use CURRENT_HARNESS.get() to access "
                "the current harness instead of re-entering."
            )

        self.mail_route = self._create_and_own("ep_mail_route", MailRoute, self._mail_route_cfg)
        self.chat_store = self._create_and_own("ep_chat_store", ChatStore, self._chat_store_cfg)
        self.llm = LLMClient(self._providers_cfg)
        self.consolidator = self._create_consolidator()
        self.interceptor = ChainInterceptor(self._interceptors_cfg)

        # Build plugin services
        self._plugin_services = PluginServices(
            bos_dir=self._bos_root,
            workspace=self._workspace,
            llm=self.llm,
            chat_store=self.chat_store,
            consolidator=self.consolidator,
            subagents=_HarnessSubagentRuntime(self),
        )

        self._token = CURRENT_HARNESS.set(self)
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

        if self._token is not None:
            CURRENT_HARNESS.reset(self._token)
            self._token = None

    async def create_agent(
        self,
        kind: str | None = None,
        agent_cfg: dict[str, Any] = None,
    ) -> Agent:
        if CURRENT_HARNESS.get(None) is None:
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

        merged_cfg = agent_defaults | (agent_cfg or {})

        kwargs = merged_cfg | {
            "kind": kind or merged_cfg.get("kind") or "undef",
            "llm": self.llm,
            "chat_store": self.chat_store,
            "consolidator": self.consolidator,
            "interceptor": self.interceptor,
            "tools_config": self._tools_cfg,
            "plugins": await self._bind_plugins_for_agent(merged_cfg),
            "chat_compaction_lock": self._get_compaction_lock,
        }

        return _apply(Agent, kwargs)

    async def _bind_plugins_for_agent(
        self,
        agent_cfg: dict[str, Any],
    ) -> list[AgentPlugin]:
        """Resolve, validate, and bind enabled plugins for an agent."""

        # Resolve merged configs for all known plugins
        merged_configs = _resolve_plugin_configs(
            self._platform_plugins,
            agent_cfg.get("plugins", {}),
        )

        # Determine enabled plugin names in order
        enabled_names = _resolve_enabled_plugin_names(
            self._enabled_plugins,
            agent_cfg.get("plugins", {}),
        )

        bound: list[AgentPlugin] = []
        for pname in enabled_names:
            hp = self._harness_plugins.get(pname)
            if hp is None and ep_plugin.has(pname):
                hp = await self._instantiate_and_setup_plugin(pname)
                self._harness_plugins[pname] = hp

            if hp is None:
                logger.warning("Unknown plugin %r; skipping.", pname)
                continue

            cfg = dict(hp.default_config()) | merged_configs.get(pname, {})
            hp.validate_config(cfg)
            try:
                agent_plugin = hp.bind(cfg)
            except Exception:
                logger.error(
                    "Failed to bind plugin %r for agent %r",
                    pname,
                    agent_cfg.get("agent_name") or "unknown",
                    exc_info=True,
                )
                raise
            bound.append(agent_plugin)

        return bound

    async def _instantiate_and_setup_plugin(self, plugin_name: str) -> HarnessPlugin:
        """Instantiate a harness plugin provider from ep_plugin and run setup."""
        instance = ep_plugin.invoke(plugin_name, {})
        if not isinstance(instance, HarnessPlugin):
            raise TypeError(f"Plugin {plugin_name} does not implement HarnessPlugin")
        if self._plugin_services is not None:
            await instance.setup(self._plugin_services)
        return instance

    def _create_and_own(self, ep_name: str, protocol: type, cfg: Any) -> Any:
        from . import __dict__ as core_exports

        config = (cfg or {}) | {"bos_dir": str(self._bos_root), "workspace_dir": str(self._workspace)}
        instance = _create_extension_instance(core_exports[ep_name], protocol, config)
        if instance is not None:
            self._owned.append(instance)
        return instance

    def _create_consolidator(self) -> Consolidator:
        if isinstance(self._consolidator_cfg, Consolidator):
            self._owned.append(self._consolidator_cfg)
            return self._consolidator_cfg

        cfg = (self._consolidator_cfg or {}).copy()
        cfg["model"] = cfg.get("model") or os.getenv("BOS_CONSOLIDATOR_MODEL")
        cfg["llm"] = self.llm
        return self._create_and_own("ep_consolidator", Consolidator, cfg)

    def _get_subagent_config(self, role: str) -> dict[str, Any]:
        return self._subagent_defaults | (self._subagents_cfg.get(role) or {})

    def _get_compaction_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._compaction_locks:
            self._compaction_locks[chat_id] = asyncio.Lock()
        return self._compaction_locks[chat_id]

    @staticmethod
    def _make_subagent_chat_id(parent_chat_id: str, role: str) -> str:
        agent_tag = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "agent"
        agent_tag = agent_tag[:10]
        return f"{parent_chat_id}~{agent_tag}{uuid.uuid4().hex[:8]}"
