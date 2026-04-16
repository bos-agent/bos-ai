"""
Lightweight single-file agent framework.
"""

from __future__ import annotations

from bos.protocol import Envelope as Envelope

from ._utils import (
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
from .actor import AgentActor
from .agent import AbortTurn as AbortTurn
from .agent import ChainReactInterceptor as ChainReactInterceptor
from .agent import ReactAgent as ReactAgent
from .agent import ReactContext as ReactContext
from .contract import Agent as Agent
from .contract import Channel as Channel
from .contract import Closeable as Closeable
from .contract import Consolidator as Consolidator
from .contract import MailBox as MailBox
from .contract import Mailbox as Mailbox
from .contract import MailRoute as MailRoute
from .contract import MemoryStore as MemoryStore
from .contract import Message as Message
from .contract import MessageStore as MessageStore
from .contract import ReactInterceptor as ReactInterceptor
from .contract import SkillsLoader as SkillsLoader
from .contract import ep_actor_command as ep_actor_command
from .contract import ep_agent as ep_agent
from .contract import ep_channel as ep_channel
from .contract import ep_consolidator as ep_consolidator
from .contract import ep_mail_route as ep_mail_route
from .contract import ep_mailbox as ep_mailbox
from .contract import ep_memory_store as ep_memory_store
from .contract import ep_message_store as ep_message_store
from .contract import ep_provider as ep_provider
from .contract import ep_react_interceptor as ep_react_interceptor
from .contract import ep_skills_loader as ep_skills_loader
from .contract import ep_tool as ep_tool
from .defaults import FileSystemSkillsLoader as FileSystemSkillsLoader
from .defaults import InMemMailbox as InMemMailbox
from .defaults import InMemMailRoute as InMemMailRoute
from .defaults import InMemMemoryStore as InMemMemoryStore
from .defaults import InMemMessageStore as InMemMessageStore
from .defaults import NaiveConsolidator as NaiveConsolidator
from .defaults import litellm_complete as litellm_complete
from .harness import (
    CURRENT_HARNESS,
    AgentHarness,
    bootstrap_platform,
)
from .llm import LLMClient as LLMClient
from .llm import LLMResponse as LLMResponse
from .llm import ToolCallRequest as ToolCallRequest
from .registry import Extension as Extension
from .registry import ExtensionPoint as ExtensionPoint
from .registry import ToolRegistry as ToolRegistry

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
    "InMemMailRoute",
    "InMemMailbox",
    "InMemMemoryStore",
    "InMemMessageStore",
    "LLMClient",
    "LLMResponse",
    "MailBox",
    "Mailbox",
    "MailRoute",
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
    "ep_mail_route",
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
