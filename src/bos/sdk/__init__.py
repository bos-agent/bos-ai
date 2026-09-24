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
    AgentPort,
    AgentResult,
    ChatCommit,
    ChatMeta,
    ChatStore,
    Consolidator,
    ContextResult,
    LLMResponse,
    Message,
    TokenEstimate,
    ToolCallRequest,
    TurnContext,
    TurnEvent,
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

# AgentPort.ask/run annotate `content` as MessageContent, a bare TypeAlias
# (str | list[MessageContentPart]) that typing.get_type_hints() inlines away —
# so the promise-completeness test below never sees the name "MessageContent"
# itself, only the TypedDicts it expands to. Not re-exported by bos.core or
# bos.core.contract, so — same idiom as the block below — they come straight
# from bos.core.agent. Promised by hand alongside MessageContent: an
# implementer needs them to build a non-string `content` value from the
# contract alone.
from bos.core.agent import FilePart, ImagePart, TextPart

# Not re-exported by bos.core/__init__.py — importing straight from bos.core.contract
# is the idiom bos.plugins already uses (subagent.py, task.py). Widening bos.core's
# public surface for these is an API change BEP 18 does not make.
from bos.core.contract import MessageContent, PromptProvider, ToolAttributes, ToolSet

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
    "AgentPort",
    "AgentResult",
    "Message",
    "TurnContext",
    "LLM",
    "LLMResponse",
    "ChatStore",
    "ChatCommit",
    "ChatMeta",
    "ContextResult",
    "TokenEstimate",
    "Consolidator",
    "ToolSet",
    "ToolAttributes",
    # ToolCallRequest is a *field* type of LLMResponse (LLMResponse.tool_calls), not a
    # signature type of any promised port's own methods, so
    # test_promised_ports_are_implementable_from_the_contract_alone does not see it —
    # drop it here and that test stays green. It is promised anyway, by hand: no
    # LLMResponse can be constructed without it.
    "ToolCallRequest",
    "TurnInterceptor",
    "PromptProvider",
    "TurnEventSink",
    "TurnEvent",
    "MessageContent",
    "TextPart",
    "ImagePart",
    "FilePart",
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
