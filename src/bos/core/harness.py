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
)
from .agent import Agent, ChainInterceptor
from .contract import (
    AgentPlugin,
    BackgroundLLM,
    ChatStore,
    Consolidator,
    HarnessPlugin,
    JobRunner,
    LifecycleBus,
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
        if "tools" not in kwargs:
            kwargs["tools"] = []
        elif (tools := kwargs["tools"]) is not None and not isinstance(tools, list):
            raise TypeError(f"tools must be a list or None, got {type(tools).__name__}")

        plugins = kwargs.get("plugins")
        if plugins is None:
            kwargs["plugins"] = {"enabled": [], "disabled": [], "prompts": {}}
        elif not isinstance(plugins, dict):
            raise TypeError(f"plugins must be a dict or None, got {type(plugins).__name__}")

        kwargs.setdefault("tools", [])
        kwargs.setdefault("plugins", {"enabled": [], "disabled": [], "prompts": {}})
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

    async def ask(
        self,
        role: str,
        message: str,
        *,
        parent: ToolContext,
        agent_cfg: dict[str, Any] | None = None,
    ) -> str:
        child_chat_id = self._harness._make_subagent_chat_id(parent.chat_id, role)
        agent = await self._harness.create_agent(role, agent_cfg)
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
        consolidator: str = "_default",
        chat_store: str = "_default",
        mail_route: str = "_default",
        job_runner: str = "_default",
        interceptors: list[str | dict[str, Any]] | None = None,
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._consolidator_impl = consolidator
        self._chat_store_impl = chat_store
        self._mail_route_impl = mail_route
        self._job_runner_impl = job_runner
        self._interceptors_impl = interceptors or []

        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self.mail_route: MailRoute | None = None
        self.chat_store: ChatStore | None = None
        self.consolidator: Consolidator | None = None
        self.interceptor: ChainInterceptor | None = None
        self.llm: LLMClient | None = None
        self.events: LifecycleBus | None = None
        self.jobs: JobRunner | None = None
        self.background_llm: BackgroundLLM | None = None

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

        self.mail_route = await self._create_and_own("ep_mail_route", MailRoute, None, impl=self._mail_route_impl)
        self.chat_store = await self._create_and_own("ep_chat_store", ChatStore, None, impl=self._chat_store_impl)
        assert self.chat_store is not None  # ep_chat_store has a _default, so creation never returns None
        self.llm = LLMClient()
        self.consolidator = await self._create_consolidator()
        self.interceptor = ChainInterceptor(self._interceptors_impl)

        # BEP 11 services: in-process LifecycleBus, JobRunner, BackgroundLLM.
        from bos.core.contract import ep_job_runner
        from bos.core.defaults.background_llm import DefaultBackgroundLLM
        from bos.core.defaults.lifecycle import DefaultLifecycleBus

        self.events = DefaultLifecycleBus()
        self.jobs = await ep_job_runner.invoke(self._job_runner_impl, {"bus": self.events})
        assert self.jobs is not None  # ep_job_runner has a _default, so creation never returns None
        await self.jobs.start()
        self._owned.append(self.jobs)
        self.background_llm = DefaultBackgroundLLM(self.llm)

        # Build plugin services
        self._plugin_services = PluginServices(
            bos_dir=self._bos_root,
            workspace=self._workspace,
            llm=self.llm,
            chat_store=self.chat_store,
            consolidator=self.consolidator,
            subagents=_HarnessSubagentRuntime(self),
            events=self.events,
            jobs=self.jobs,
            background_llm=self.background_llm,
        )

        self._token = CURRENT_HARNESS.set(self)
        return self

    async def __aexit__(self, *exc) -> None:
        # Drain BEP 11 JobRunner first — gives in-flight jobs a bounded window
        # while BackgroundLLM/ChatStore are still alive (BEP 11 §4).
        if self.jobs is not None:
            try:
                await self.jobs.drain(timeout=5.0)
            except Exception:
                logger.exception("Error draining JobRunner")
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
        agent_cfg: dict[str, Any] | None = None,
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

        merged_cfg = _deep_merge(dict(agent_defaults), agent_cfg or {})

        kwargs = merged_cfg | {
            "kind": kind or merged_cfg.get("kind") or "undef",
            "llm": self.llm,
            "chat_store": self.chat_store,
            "consolidator": self.consolidator,
            "interceptor": self.interceptor,
            "plugins": await self._bind_plugins_for_agent(merged_cfg),
            "chat_compaction_lock": self._get_compaction_lock,
            "workspace": str(self._workspace),
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

    async def _create_and_own(self, ep_name: str, protocol: type, cfg: Any, *, impl: str | None = None) -> Any:
        from . import __dict__ as core_exports

        ep = core_exports[ep_name]
        context = {"bos_dir": str(self._bos_root), "workspace_dir": str(self._workspace)}
        if impl is not None and cfg is None:
            instance = await ep.invoke(impl, context)
        elif impl is not None:
            instance = await ep.invoke(impl, cfg | context)
        else:
            config = (cfg or {}) | context
            instance = await _create_extension_instance(ep, protocol, config)
        if instance is not None:
            self._owned.append(instance)
        return instance

    async def _create_consolidator(self) -> Consolidator:
        cfg = {"model": os.getenv("BOS_CONSOLIDATOR_MODEL"), "llm": self.llm}
        from . import __dict__ as core_exports

        instance = await core_exports["ep_consolidator"].invoke(self._consolidator_impl, cfg)
        if instance is None:
            raise RuntimeError(f"Consolidator extension {self._consolidator_impl!r} could not be created")
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
