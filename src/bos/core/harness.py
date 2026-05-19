from __future__ import annotations

import contextvars
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from ._utils import _aclose, _create_extension_instance, _load_ext_modules, _load_ext_paths
from .agent import ChainReactInterceptor, ReActAgent
from .contract import Consolidator, MailBox, MailRoute, MemoryExtension, MessageStore, SkillsLoader, ep_agent
from .defaults import default_agent_spec
from .llm import LLMClient

logger = logging.getLogger(__name__)


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
        ReActAgent.register(**(agent_spec))

    # prevent the litellm to call load_dotenv automatically, and supress logs.
    os.environ["LITELLM_MODE"] = "extension"
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)


CURRENT_HARNESS: contextvars.ContextVar[AgentHarness] = contextvars.ContextVar("current_harness")
CURRENT_MAILBOX: contextvars.ContextVar[MailBox] = contextvars.ContextVar("current_mailbox")


class AgentHarness:
    """Lifecycle-owning container for shared agent services."""

    def __init__(
        self,
        *,
        mail_route: dict[str, Any] | None = None,
        message_store: dict[str, Any] | None = None,
        memory: dict[str, Any] | None = None,
        consolidator: dict[str, Any] | None = None,
        skills_loader: dict[str, Any] | None = None,
        providers: dict[str, dict[str, Any]] | None = None,
        interceptors: list[str | dict[str, Any]] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
        subagent_defaults: dict[str, Any] | None = None,
        subagents: list[dict[str, Any]] | None = None,
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._subagent_defaults = subagent_defaults or {}
        self._subagents_cfg = {cfg["name"]: cfg for cfg in subagents} if subagents else {}

        self._mail_route_cfg = mail_route
        self._message_store_cfg = message_store
        self._memory_cfg = memory
        self._consolidator_cfg = consolidator
        self._skills_loader_cfg = skills_loader
        self._providers_cfg = providers
        self._interceptors_cfg = interceptors
        self._tools_cfg = tools or {}

        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self.mail_route = None
        self.message_store = None
        self.memory = None
        self.consolidator = None
        self.skills_loader = None
        self.interceptor = None
        self.llm = None

    async def __aenter__(self):
        if self._token is not None:
            raise RuntimeError(
                "AgentHarness is already active. Use CURRENT_HARNESS.get() to access "
                "the current harness instead of re-entering."
            )

        self.mail_route = self._create_and_own("ep_mail_route", MailRoute, self._mail_route_cfg)
        self.message_store = self._create_and_own("ep_message_store", MessageStore, self._message_store_cfg)
        self.memory = self._create_and_own("ep_memory", MemoryExtension, self._memory_cfg)
        self.llm = LLMClient(self._providers_cfg)
        self.consolidator = self._create_consolidator()
        self.skills_loader = self._create_and_own("ep_skills_loader", SkillsLoader, self._skills_loader_cfg)
        self.interceptor = ChainReactInterceptor(self._interceptors_cfg)

        self._token = CURRENT_HARNESS.set(self)
        return self

    async def __aexit__(self, *exc) -> None:
        await _aclose(self.interceptor)
        for resource in reversed(self._owned):
            await _aclose(resource)
        self._owned.clear()

        if self._token is not None:
            CURRENT_HARNESS.reset(self._token)
            self._token = None

    def create_agent(self, role: str | None = None, agent_cfg: dict[str, Any] = None) -> ReActAgent:
        if CURRENT_HARNESS.get(None) is None:
            raise RuntimeError("create_agent must be called within an active AgentHarness context.")

        if not any([role, agent_cfg]):
            agent_cfg = {
                "system_prompt": "You are a helpful assistant.",
                "tools": [],
                "skills": [],
                "maxims": {},
                "subagents": [],
            }

        kwargs = (agent_cfg or {}) | {
            "name": role or (agent_cfg or {}).get("name"),
            "llm": self.llm,
            "message_store": self.message_store,
            "memory": self.memory,
            "consolidator": self.consolidator,
            "skills_loader": self.skills_loader,
            "interceptor": self.interceptor,
            "tool_configs": self._tools_cfg,
        }

        return ep_agent.invoke(role, kwargs) if role else ReActAgent(**kwargs)

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

    @staticmethod
    def _make_subagent_chat_id(parent_chat_id: str, role: str) -> str:
        agent_tag = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "agent"
        agent_tag = agent_tag[:10]
        return f"{parent_chat_id}~{agent_tag}{uuid.uuid4().hex[:8]}"
