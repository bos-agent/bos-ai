"""
Lightweight single-file agent framework.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path

from bos.protocol import Envelope, TurnEvent

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
from .agent import AbortTurn, ChainReactInterceptor, ReactAgent, TurnContext
from .contract import (
    Agent,
    Channel,
    Closeable,
    Consolidator,
    EventSink,
    MailBox,
    MailRoute,
    MemoryStore,
    Message,
    MessageStore,
    SkillsLoader,
    TurnInterceptor,
    ep_actor_command,
    ep_agent,
    ep_channel,
    ep_consolidator,
    ep_mail_route,
    ep_memory_store,
    ep_message_store,
    ep_provider,
    ep_skills_loader,
    ep_tool,
    ep_turn_interceptor,
)
from .defaults import (
    FileSystemSkillsLoader,
    InMemMailRoute,
    InMemMemoryStore,
    InMemMessageStore,
    NaiveConsolidator,
    default_agent_spec,
    litellm_complete,
)
from .events import DerivedEventSink, MailboxEventSink, derive_event_sink
from .harness import (
    CURRENT_HARNESS,
    CURRENT_MAILBOX,
    AgentHarness,
    bootstrap_platform,
)
from .llm import LLMClient, LLMResponse, ToolCallRequest
from .registry import Extension, ExtensionPoint, ToolRegistry


def _resolve_version() -> str:
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    if pyproject.exists():
        return str(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"])
    try:
        return _package_version("bos-ai")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()

__all__ = [
    "__version__",
    "AbortTurn",
    "Agent",
    "AgentActor",
    "AgentHarness",
    "CURRENT_HARNESS",
    "CURRENT_MAILBOX",
    "Channel",
    "ChainReactInterceptor",
    "Closeable",
    "Consolidator",
    "default_agent_spec",
    "DerivedEventSink",
    "Envelope",
    "EventSink",
    "Extension",
    "ExtensionPoint",
    "FileSystemSkillsLoader",
    "InMemMailRoute",
    "InMemMemoryStore",
    "InMemMessageStore",
    "LLMClient",
    "LLMResponse",
    "MailBox",
    "MailboxEventSink",
    "MailRoute",
    "MemoryStore",
    "Message",
    "MessageStore",
    "NaiveConsolidator",
    "ReactAgent",
    "SkillsLoader",
    "ToolCallRequest",
    "ToolRegistry",
    "TurnContext",
    "TurnInterceptor",
    "TurnEvent",
    "bootstrap_platform",
    "derive_event_sink",
    "ep_actor_command",
    "ep_agent",
    "ep_channel",
    "ep_consolidator",
    "ep_mail_route",
    "ep_memory_store",
    "ep_message_store",
    "ep_provider",
    "ep_skills_loader",
    "ep_tool",
    "ep_turn_interceptor",
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
