"""
Lightweight single-file agent framework.
"""

from __future__ import annotations

import logging

from bos.core._utils import _aclose as _aclose
from bos.core._utils import _allowed as _allowed
from bos.core._utils import _apply as _apply
from bos.core._utils import _apply_async as _apply_async
from bos.core._utils import _as_parts as _as_parts
from bos.core._utils import _build_params as _build_params
from bos.core._utils import _compact as _compact
from bos.core._utils import _create_extension_instance as _create_extension_instance
from bos.core._utils import _flock as _flock
from bos.core._utils import _litellm_response_to_llm_response as _litellm_response_to_llm_response
from bos.core._utils import _litellm_tool_calls_to_requests as _litellm_tool_calls_to_requests
from bos.core._utils import _load_ext_modules as _load_ext_modules
from bos.core._utils import _load_ext_paths as _load_ext_paths
from bos.core._utils import _load_json as _load_json
from bos.core._utils import _pick_collection as _pick_collection
from bos.core._utils import _read_text as _read_text
from bos.core._utils import _safe_format as _safe_format
from bos.core._utils import _strip_think as _strip_think
from bos.core.actor import AgentActor as _AgentActor
from bos.core.agent import AbortTurn as AbortTurn
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
    CURRENT_HARNESS as _CURRENT_HARNESS,
)
from bos.core.harness import (
    AgentHarness as _AgentHarness,
)
from bos.core.harness import (
    bootstrap_platform as _bootstrap_platform,
)
from bos.core.interceptors import ChainReactInterceptor as ChainReactInterceptor
from bos.core.llm import LLMClient as LLMClient
from bos.core.llm import LLMResponse as LLMResponse
from bos.core.llm import ToolCallRequest as ToolCallRequest
from bos.core.registry import Extension as Extension
from bos.core.registry import ExtensionPoint as ExtensionPoint
from bos.core.registry import ToolRegistry as ToolRegistry
from bos.protocol import Envelope as Envelope

AgentActor = _AgentActor
AgentHarness = _AgentHarness
CURRENT_HARNESS = _CURRENT_HARNESS
bootstrap_platform = _bootstrap_platform

__version__ = "0.1.0"


logger = logging.getLogger("bos")
