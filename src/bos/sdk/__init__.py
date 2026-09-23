"""The BOS embedding SDK (BEP 18).

What this module exports is the embedding contract. What it does not export is
not promised — including every ``_``-prefixed helper ``bos.core`` re-exports for
extensions, which remain available and explicitly unstable.
"""

from __future__ import annotations

from bos.config import RootConfig, Workspace, validate_config
from bos.core import (
    LLM,
    Agent,
    AgentHarness,
    AgentResult,
    ChatStore,
    Consolidator,
    Message,
    TurnContext,
    TurnEventSink,
    TurnInterceptor,
    ep_agent,
    ep_channel,
    ep_chat_store,
    ep_consolidator,
    ep_mail_route,
    ep_plugin,
    ep_provider,
    ep_tool,
    ep_turn_interceptor,
)

# Not re-exported by bos.core/__init__.py — importing straight from bos.core.contract
# is the idiom bos.plugins already uses (subagent.py, task.py). Widening bos.core's
# public surface for these two is an API change BEP 18 does not make.
from bos.core.contract import PromptProvider, ToolSet

from ._app import BosApp
from ._bootstrap import bootstrap as bootstrap
from ._bootstrap import open_harness

# `bootstrap` is deliberately absent from __all__: BEP 18 §3.8 does not promise it.
# It stays importable — bos.cli uses it — but __all__ is the embedder's contract.
__all__ = [
    "BosApp",
    "open_harness",
    "Agent",
    "AgentHarness",
    "AgentResult",
    "Message",
    "TurnContext",
    "LLM",
    "ChatStore",
    "Consolidator",
    "ToolSet",
    "TurnInterceptor",
    "PromptProvider",
    "TurnEventSink",
    "ep_tool",
    "ep_provider",
    "ep_agent",
    "ep_chat_store",
    "ep_mail_route",
    "ep_consolidator",
    "ep_turn_interceptor",
    "ep_channel",
    "ep_plugin",
    "Workspace",
    "RootConfig",
    "validate_config",
]
