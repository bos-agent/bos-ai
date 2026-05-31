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
from .events import derive_event_sink
from .llm import LLMClient

logger = logging.getLogger(__name__)


class AgentRegistry:
    _registry: dict[str, dict[str, Any]] = {}

    @classmethod
    def register(cls, name: str, description: str | None = None, **kwargs):
        def _resolve_tools(value):
            if isinstance(value, dict):
                # BEP6 structured format: {enabled, disabled, usages}
                enabled = value.get("enabled", [])
                return None if "*" in enabled else enabled
            if isinstance(value, list):
                return value
            if value is None:
                return []
            if value == "*":
                return None  # fully open the capability
            raise TypeError(f"tools must be a dict, list, '*', or None, got {type(value).__name__}")

        def _resolve_plugins(value):
            if isinstance(value, dict):
                if "enabled" in value or "disabled" in value:
                    # BEP6 structured format: {enabled, disabled, prompts}
                    return value
                # Legacy dict format: {Name: {enabled: true}}
                enabled = [k for k, v in value.items() if isinstance(v, dict) and v.get("enabled") is not False]
                return {"enabled": enabled, "disabled": [], "prompts": {}}
            if isinstance(value, list):
                return {"enabled": value, "disabled": [], "prompts": {}}
            if value is None:
                return {"enabled": [], "disabled": [], "prompts": {}}
            raise TypeError(f"plugins must be a dict or list, got {type(value).__name__}")

        kwargs["tools"] = _resolve_tools(kwargs.get("tools"))
        kwargs["plugins"] = _resolve_plugins(kwargs.get("plugins"))
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


CURRENT_HARNESS: contextvars.ContextVar[AgentHarness] = contextvars.ContextVar("current_harness")
CURRENT_MAILBOX: contextvars.ContextVar[MailBox] = contextvars.ContextVar("current_mailbox")


class _HarnessSubagentRuntime:
    """Adapter that implements SubagentRuntime using AgentHarness internals."""

    def __init__(self, harness: AgentHarness) -> None:
        self._harness = harness

    async def ask(self, role: str, message: str, *, parent: ToolContext) -> str:
        # Subagent config (task_template, per-role overrides) removed from harness —
        # deferred to SubagentPlugin future BEP. Subagents are now plain create_agent(role).
        child_chat_id = self._harness._make_subagent_chat_id(parent.chat_id, role)
        agent = await self._harness.create_agent(role)
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
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
        harness_config: dict[str, Any] | None = None,
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._harness_cfg = harness_config or {}

        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self.mail_route: MailRoute | None = None
        self.chat_store: ChatStore | None = None
        self.consolidator: Consolidator | None = None
        self.interceptor: ChainInterceptor | None = None
        self.llm: LLMClient | None = None

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

        cfg = self._harness_cfg

        mail_route_impl = cfg.get("mail_route", "_default")
        self.mail_route = self._create_and_own("ep_mail_route", MailRoute, None, impl=mail_route_impl)

        chat_store_impl = cfg.get("chat_store", "_default")
        self.chat_store = self._create_and_own("ep_chat_store", ChatStore, None, impl=chat_store_impl)

        self.llm = LLMClient()
        self.consolidator = self._create_consolidator()
        self.interceptor = ChainInterceptor(cfg.get("interceptors", []))

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
            "plugins": await self._bind_plugins_for_agent(merged_cfg),
            "chat_compaction_lock": self._get_compaction_lock,
        }

        return _apply(Agent, kwargs)

    async def _bind_plugins_for_agent(
        self,
        agent_cfg: dict[str, Any],
    ) -> list[AgentPlugin]:
        """Resolve, validate, and bind enabled plugins for an agent.

        Uses the BEP6 flat-list plugin model: plugins.enabled / plugins.disabled
        from the agent config, with plugin-bindings.<Name> for per-plugin settings.
        """
        plugins_cfg = agent_cfg.get("plugins", {})
        if isinstance(plugins_cfg, (list, tuple)):
            # Legacy list — treat as enabled
            enabled_names = list(plugins_cfg)
            disabled: list[str] = []
        elif isinstance(plugins_cfg, dict):
            enabled = plugins_cfg.get("enabled", [])
            disabled = plugins_cfg.get("disabled", [])
            if isinstance(enabled, (list, tuple)):
                enabled_names = list(enabled)
            else:
                enabled_names = list(ep_plugin._extensions.keys())
            if isinstance(disabled, (list, tuple)):
                disabled = list(disabled)
        else:
            return []

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

    def _create_and_own(self, ep_name: str, protocol: type, cfg: Any, *, impl: str | None = None) -> Any:
        from . import __dict__ as core_exports

        ep = core_exports[ep_name]
        if impl is not None and cfg is None:
            # Use EP invoke with defaults (already merged during bootstrap)
            instance = ep.invoke(impl)
        elif impl is not None:
            instance = ep.invoke(impl, cfg)
        else:
            config = (cfg or {}) | {"bos_dir": str(self._bos_root), "workspace_dir": str(self._workspace)}
            instance = _create_extension_instance(ep, protocol, config)
        if instance is not None:
            self._owned.append(instance)
        return instance

    def _create_consolidator(self) -> Consolidator:
        consolidator_cfg = self._harness_cfg.get("consolidator", "_default")
        # Pass-through: if the config value is already a Consolidator instance
        # (test convenience), use it directly.
        if isinstance(consolidator_cfg, Consolidator):
            self._owned.append(consolidator_cfg)
            return consolidator_cfg
        cfg = {"model": os.getenv("BOS_CONSOLIDATOR_MODEL"), "llm": self.llm}
        from . import __dict__ as core_exports
        instance = core_exports["ep_consolidator"].invoke(consolidator_cfg, cfg)
        if instance is not None:
            self._owned.append(instance)
        return instance

    def _get_compaction_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._compaction_locks:
            self._compaction_locks[chat_id] = asyncio.Lock()
        return self._compaction_locks[chat_id]

    @staticmethod
    def _make_subagent_chat_id(parent_chat_id: str, role: str) -> str:
        agent_tag = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "agent"
        agent_tag = agent_tag[:10]
        return f"{parent_chat_id}~{agent_tag}{uuid.uuid4().hex[:8]}"
