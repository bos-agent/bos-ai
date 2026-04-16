from __future__ import annotations

import contextvars
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ..protocol import Envelope
from ._utils import _aclose, _create_extension_instance, _load_ext_modules, _load_ext_paths, _safe_format
from .agent import ChainReactInterceptor, ReactAgent
from .contract import Consolidator, Mailbox, MemoryStore, MessageStore, SkillsLoader, ep_agent
from .llm import LLMClient
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def bootstrap_platform(
    bos_dir: str | Path = ".bos",
    envs: dict[str, str] | None = None,
    envfile: str | None = None,
    extensions: list[str] | None = None,
    agents: list[dict[str, Any]] | None = None,
    agent_defaults: dict[str, Any] | None = None,
) -> None:
    bos_root = Path(bos_dir).expanduser().resolve()
    bos_root.mkdir(parents=True, exist_ok=True)

    if envs:
        os.environ.update(envs)
    if envfile:
        from dotenv import load_dotenv

        load_dotenv((bos_root / Path(envfile).expanduser()).resolve())

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

    if agents:
        defaults = agent_defaults or {}
        for agent_spec in agents:
            ReactAgent.register(**(defaults | agent_spec))


CURRENT_HARNESS: contextvars.ContextVar["AgentHarness"] = contextvars.ContextVar("current_harness")


class AgentHarness:
    """Lifecycle-owning container for shared agent services."""

    def __init__(
        self,
        *,
        mailbox: dict[str, Any] | None = None,
        message_store: dict[str, Any] | None = None,
        memory_store: dict[str, Any] | None = None,
        consolidator: dict[str, Any] | None = None,
        skills_loader: dict[str, Any] | None = None,
        providers: dict[str, dict[str, Any]] | None = None,
        interceptors: list[str | dict[str, Any]] | None = None,
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
        subagents: list[dict[str, Any]] | None = None,
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self.workspace = self._workspace
        self._subagents_cfg = {cfg.get("name", "_default"): cfg for cfg in subagents} if subagents else {}

        self._mailbox_cfg = mailbox
        self._message_store_cfg = message_store
        self._memory_store_cfg = memory_store
        self._consolidator_cfg = consolidator
        self._skills_loader_cfg = skills_loader
        self._providers_cfg = providers
        self._interceptors_cfg = interceptors

        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self._original_cwd: Path | None = None
        self.mailbox = None
        self.message_store = None
        self.memory_store = None
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

        self._original_cwd = Path.cwd()
        os.chdir(self._bos_root)

        self.mailbox = self._create_and_own("ep_mailbox", Mailbox, self._mailbox_cfg)
        self.message_store = self._create_and_own("ep_message_store", MessageStore, self._message_store_cfg)
        self.memory_store = self._create_and_own("ep_memory_store", MemoryStore, self._memory_store_cfg)
        self.consolidator = self._create_and_own("ep_consolidator", Consolidator, self._consolidator_cfg)
        self.skills_loader = self._create_and_own("ep_skills_loader", SkillsLoader, self._skills_loader_cfg)
        self.interceptor = ChainReactInterceptor(self._interceptors_cfg)
        self.llm = LLMClient(self._providers_cfg)

        os.chdir(self._workspace)
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

        if self._original_cwd is not None:
            os.chdir(self._original_cwd)
            self._original_cwd = None

    def create_agent(self, agent_name: str | None = None, agent_cfg: dict[str, Any] = None):
        if CURRENT_HARNESS.get(None) is None:
            raise RuntimeError("create_agent must be called within an active AgentHarness context.")

        if not any([agent_name, agent_cfg]):
            agent_cfg = {
                "system_prompt": "You are a helpful assistant.",
                "model": os.getenv("BOS_MODEL"),
                "tools": [],
                "skills": [],
                "memories": [],
                "subagents": [],
            }

        local_tools = self._create_local_tools()
        kwargs = (agent_cfg or {}) | {
            "llm": self.llm,
            "message_store": self.message_store,
            "memory_store": self.memory_store,
            "consolidator": self.consolidator,
            "skills_loader": self.skills_loader,
            "interceptor": self.interceptor,
            "local_tools": local_tools,
        }

        return ep_agent.invoke(agent_name, kwargs) if agent_name else ReactAgent(**kwargs)

    def _create_and_own(self, ep_name: str, protocol: type, cfg: Any) -> Any:
        from . import __dict__ as core_exports

        instance = _create_extension_instance(core_exports[ep_name], protocol, cfg)
        if instance is not None:
            self._owned.append(instance)
        return instance

    def _create_local_tools(self):
        tools = ToolRegistry("Harness-scoped tools for this agent.")
        self._register_harness_tools(tools)
        return tools

    def _register_harness_tools(self, tools) -> None:
        harness = self

        @tools(
            name="SendMail",
            description=("Send a message to the recipient's address."),
            parameters={
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "Sender address"},
                    "recipient": {"type": "string", "description": "Recipient address"},
                    "content": {"type": "string", "description": "Message content"},
                },
                "required": ["sender", "recipient", "content"],
            },
        )
        async def tool_send_mail(sender: str, recipient: str, content: str) -> str:
            await harness.mailbox.send(Envelope(sender=sender, recipient=recipient, content=content))
            return f"(Sent to {recipient})"

        @tools(
            name="AskSubagent",
            description="Delegate a task to a named subagent and return its response.",
            parameters={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Name of the agent."},
                    "message": {"type": "string", "description": "Message to send."},
                    "conversation_id": {"type": "string", "description": "Parent conversation id."},
                },
                "required": ["agent_name", "conversation_id", "message"],
            },
        )
        async def ask_subagent(agent_name: str, message: str, conversation_id: str) -> str:
            if not ep_agent.has(agent_name):
                return f"Error: Agent '{agent_name}' not found."
            subagent_cfg = harness._get_subagent_config(agent_name)
            if task_template := subagent_cfg.get("task_template"):
                message = _safe_format(task_template, task=message, agent_name=agent_name, workspace=harness.workspace)

            agent = harness.create_agent(agent_name, subagent_cfg)
            return await agent.ask(
                f"{conversation_id}:{agent_name}:{uuid.uuid4().hex}",
                message,
                ctx_metadata={
                    "subagent": agent_name,
                    "parent_conversation_id": conversation_id,
                },
            )

    def _get_subagent_config(self, agent_name: str) -> dict[str, Any]:
        default = self._subagents_cfg.get("_default", {})
        config = self._subagents_cfg.get(agent_name, {})
        return default | config
