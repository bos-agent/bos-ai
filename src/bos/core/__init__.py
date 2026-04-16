"""
Lightweight single-file agent framework.
"""

from __future__ import annotations

from bos.core._utils import (
    _aclose,
    _allowed,
    _apply,
    _apply_async,
    _as_parts,
    _build_params,
    _compact,
    _create_extension_instance,
    _flock,
    _litellm_response_to_llm_response,
    _litellm_tool_calls_to_requests,
    _load_ext_modules,
    _load_ext_paths,
    _load_json,
    _pick_collection,
    _read_text,
    _safe_format,
    _strip_think,
)
from bos.core.actor import AgentActor
from bos.core.agent import AbortTurn as AbortTurn
from bos.core.agent import ChainReactInterceptor as ChainReactInterceptor
from bos.core.agent import ReactAgent as ReactAgent
from bos.core.agent import ReactContext as ReactContext
from bos.core.contract import Agent as Agent
from bos.core.contract import Channel as Channel
from bos.core.contract import Closeable as Closeable
from bos.core.contract import Consolidator as Consolidator
from bos.core.contract import Mailbox as Mailbox
from bos.core.contract import MemoryStore as MemoryStore
from bos.core.contract import Message as Message
from bos.core.contract import MessageStore as MessageStore
from bos.core.contract import ReactInterceptor as ReactInterceptor
from bos.core.contract import SkillsLoader as SkillsLoader
from bos.core.contract import ep_actor_command as ep_actor_command
from bos.core.contract import ep_agent as ep_agent
from bos.core.contract import ep_channel as ep_channel
from bos.core.contract import ep_consolidator as ep_consolidator
from bos.core.contract import ep_mailbox as ep_mailbox
from bos.core.contract import ep_memory_store as ep_memory_store
from bos.core.contract import ep_message_store as ep_message_store
from bos.core.contract import ep_provider as ep_provider
from bos.core.contract import ep_react_interceptor as ep_react_interceptor
from bos.core.contract import ep_skills_loader as ep_skills_loader
from bos.core.contract import ep_tool as ep_tool
from bos.core.defaults import FileSystemSkillsLoader as FileSystemSkillsLoader
from bos.core.defaults import InMemMailbox as InMemMailbox
from bos.core.defaults import InMemMemoryStore as InMemMemoryStore
from bos.core.defaults import InMemMessageStore as InMemMessageStore
from bos.core.defaults import NaiveConsolidator as NaiveConsolidator
from bos.core.defaults import litellm_complete as litellm_complete
from bos.core.harness import (
    CURRENT_HARNESS,
    AgentHarness,
    bootstrap_platform,
)
from bos.core.llm import LLMClient as LLMClient
from bos.core.llm import LLMResponse as LLMResponse
from bos.core.llm import ToolCallRequest as ToolCallRequest
from bos.core.registry import Extension as Extension
from bos.core.registry import ExtensionPoint as ExtensionPoint
from bos.core.registry import ToolRegistry as ToolRegistry
from bos.protocol import Envelope as Envelope

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AbortTurn",
    "Agent",
    "AgentActor",
    "AgentHarness",
    "CURRENT_HARNESS",
    "Channel",
    "ChainReactInterceptor",
    "Closeable",
    "Consolidator",
    "Envelope",
    "Extension",
    "ExtensionPoint",
    "FileSystemSkillsLoader",
    "InMemMailbox",
    "InMemMemoryStore",
    "InMemMessageStore",
    "LLMClient",
    "LLMResponse",
    "Mailbox",
    "MemoryStore",
    "Message",
    "MessageStore",
    "NaiveConsolidator",
    "ReactAgent",
    "ReactContext",
    "ReactInterceptor",
    "SkillsLoader",
    "ToolCallRequest",
    "ToolRegistry",
    "bootstrap_platform",
    "ep_actor_command",
    "ep_agent",
    "ep_channel",
    "ep_consolidator",
    "ep_mailbox",
    "ep_memory_store",
    "ep_message_store",
    "ep_provider",
    "ep_react_interceptor",
    "ep_skills_loader",
    "ep_tool",
    "litellm_complete",
    "_aclose",
    "_allowed",
    "_apply",
    "_apply_async",
    "_as_parts",
    "_build_params",
    "_compact",
    "_create_extension_instance",
    "_flock",
    "_litellm_response_to_llm_response",
    "_litellm_tool_calls_to_requests",
    "_load_ext_modules",
    "_load_ext_paths",
    "_load_json",
    "_pick_collection",
    "_read_text",
    "_safe_format",
    "_strip_think",
]
