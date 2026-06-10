from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import platform
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Sequence
from xml.sax.saxutils import escape

from bos.protocol import MessageContent, TurnEvent

from ._utils import (
    _aclose,
    _allowed,
    _apply_async,
    _as_parts,
    _compact,
    _create_extension_instance,
    _pick_collection,
    _strip_think,
    _xml_attr,
)
from .contract import (
    AgentPlugin,
    ChatStore,
    ContextResult,
    EventSink,
    Message,
    ReasoningEffort,
    ToolContext,
    ToolNoiseFilter,
    TurnInterceptor,
    ep_tool,
    ep_turn_interceptor,
)
from .llm import LLMClient, ToolCallRequest
from .registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from .contract import Consolidator
    from .llm import LLMResponse

logger = logging.getLogger(__name__)


ABORTED_TURN_CONTENT = """(turn aborted before completion)

The previous user request was interrupted before a final answer was produced.
Intermediate assistant/tool state from the aborted turn was intentionally not
committed as conversation context. If the user asks to continue, start a fresh
attempt from the preceding user request and redo any necessary checks or tool
work."""


@dataclass
class TurnContext:
    agent_name: str
    chat_id: str
    turn_id: str
    system: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    current: list[Message] = field(default_factory=list)
    ephemeral: list[dict[str, Any]] = field(default_factory=list)
    tool_defs: list[dict[str, Any]] = field(default_factory=list)
    current_llm_response: LLMResponse | None = None
    final_content: str | None = None
    event_sink: EventSink | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    current_message_projector: Callable[[Message], dict[str, Any]] | None = None

    def set_system_prompt(self, content: MessageContent) -> None:
        self.system = [{"role": "system", "content": content}]

    def add_message(self, llm_message: dict[str, Any], *, merge: bool = False, **kwargs) -> None:
        if merge and self.current and self.current[-1].llm_message["role"] == llm_message["role"]:
            parts = _as_parts(self.current[-1].llm_message["content"]) + _as_parts(llm_message["content"])
            self.current[-1].llm_message["content"] = parts
        else:
            self.current.append(Message(llm_message=llm_message, turn_id=self.turn_id, metadata=kwargs))

    def get_llm_messages(self) -> list[dict[str, Any]]:
        project = self.current_message_projector or (lambda m: m.llm_message)
        return (
            self.system
            + self.history
            + [project(m) for m in self.current]
            + [{k: v for k, v in message.items() if not k.startswith("_")} for message in self.ephemeral]
        )

    def set_ephemeral_message(self, key: str, llm_message: dict[str, Any]) -> None:
        """Set or replace a keyed ephemeral message for this turn.

        Ephemeral messages are sent to the LLM after persisted/current messages, but are not
        persisted to chat history. A stable key lets interceptors refresh dynamic context without
        accumulating stale duplicates across LLM iterations in the same turn.
        """
        self.clear_ephemeral_message(key)
        self.ephemeral.append(llm_message | {"_ephemeral_key": key})

    def clear_ephemeral_message(self, key: str) -> None:
        self.ephemeral = [message for message in self.ephemeral if message.get("_ephemeral_key") != key]

    @property
    def final_response(self) -> str:
        return self.final_content or self.current[-1].llm_message["content"] if self.current else "(no response)"


def _attribute_history_message(
    projected: dict[str, Any],
    source: Message,
    *,
    current_agent: str,
    actor_attribution: bool = True,
    include_workdir: bool = True,
) -> dict[str, Any]:
    content = projected.get("content")
    if not isinstance(content, str):
        return projected
    role = projected.get("role")
    if role == "assistant" and actor_attribution:
        source_agent = _metadata_str(source.metadata, "agent_name", "actor")
        if source_agent and source_agent != current_agent:
            label = _history_agent_label(source.metadata) or source_agent
            return projected | {
                "role": "user",
                "content": f"[assistant {label} said]\n{content}",
            }
    label = _history_attribution_label(
        role, source.metadata, actor_attribution=actor_attribution, include_workdir=include_workdir
    )
    if not label:
        return projected
    return projected | {"content": f"{label}\n{content}"}


def _history_attribution_label(
    role: Any,
    metadata: dict[str, Any],
    *,
    actor_attribution: bool = True,
    include_workdir: bool = True,
) -> str | None:
    if role == "assistant":
        if actor_attribution:
            label = _history_agent_label(metadata)
            if label:
                return f"[assistant: {label}]"
        return None
    if role == "user":
        parts: list[str] = []
        if actor_attribution:
            target = _metadata_str(metadata, "target_display") or _metadata_str(
                metadata, "target_agent", "target_actor"
            )
            if target:
                parts.append(f"user -> {target}")
        if include_workdir:
            workdir = _metadata_str(metadata, "workdir")
            if workdir:
                parts.append(f"workdir: {workdir}")
        if parts:
            return f"[{' | '.join(parts)}]"
    return None


def _history_agent_label(metadata: dict[str, Any]) -> str | None:
    return _metadata_str(metadata, "agent_display", "actor_display") or _metadata_str(metadata, "agent_name", "actor")


def _metadata_str(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_thinking(response: Any) -> str | None:
    """Extract the best available thinking content from an LLM response."""
    if getattr(response, "reasoning_content", None):
        return response.reasoning_content
    blocks = getattr(response, "thinking_blocks", None)
    if blocks:
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("thinking"):
                parts.append(block["thinking"])
        if parts:
            return "\n".join(parts)
    if getattr(response, "content", None) and getattr(response, "tool_calls", None):
        return response.content
    return None


class AbortTurn(Exception):
    pass


class ChainInterceptor:
    """
    An interceptor that takes a list of interceptor names (or configurations)
    and runs them sequentially in the provided order.
    """

    def __init__(self, interceptors: list[str | dict[str, Any]] | None = None) -> None:
        self._configs = [
            cfg.copy() if isinstance(cfg, dict) else {"name": cfg}
            for cfg in (interceptors or [])
            if isinstance(cfg, str) or (isinstance(cfg, dict) and "name" in cfg)
        ]
        self._instances: list[TurnInterceptor] = [None] * len(self._configs)

    async def aclose(self) -> None:
        for interceptor in self._instances:
            await _aclose(interceptor)

    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
        ],
        context: TurnContext,
    ) -> None:
        for i, cfg in enumerate(self._configs):
            if self._instances[i] is None and ep_turn_interceptor.has(cfg["name"]):
                try:
                    self._instances[i] = _create_extension_instance(ep_turn_interceptor, TurnInterceptor, cfg)
                except Exception as e:
                    self._instances[i] = e
                    logger.error(f"Failed to create interceptor {cfg['name']}: {e}")
            if isinstance(self._instances[i], TurnInterceptor):
                await self._instances[i].intercept(stage, context)


class _CompositePluginInterceptor:
    """Runs plugin interceptors, then a fallback interceptor chain."""

    def __init__(self, plugin_interceptors: list[TurnInterceptor], fallback: TurnInterceptor) -> None:
        self._plugin = plugin_interceptors
        self._fallback = fallback

    async def aclose(self) -> None:
        for interceptor in self._plugin:
            await _aclose(interceptor)
        await _aclose(self._fallback)

    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
        ],
        context: TurnContext,
    ) -> None:
        for interceptor in self._plugin:
            try:
                await interceptor.intercept(stage, context)
            except AbortTurn:
                raise
            except Exception as e:
                logger.error("Error in plugin interceptor: %s", e, exc_info=True)
        await self._fallback.intercept(stage, context)


class Agent:
    def __init__(
        self,
        *,
        kind: str,
        chat_store: ChatStore,
        consolidator: Consolidator,
        system_prompt: str | None = None,
        plugins: Sequence[AgentPlugin] = (),
        plugins_prompt: dict[str, str] | None = None,
        tools: list[str] | None = None,
        tools_usage: dict[str, str] | None = None,
        exclude_tools: list[str] | None = None,
        model: str | None = None,
        agent_name: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        llm: LLMClient | None = None,
        local_tools: ToolRegistry | None = None,
        interceptor: TurnInterceptor | None = None,
        max_tokens: int = 128 * 1024,
        max_iterations: int = 25,
        tool_noise_filter: ToolNoiseFilter | None = None,
        chat_compaction_lock: Callable[[str], AbstractAsyncContextManager] | None = None,
        history_attribution: bool = False,
        workspace: str | os.PathLike[str] | None = None,
    ):
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string or None")
        self._system_prompt = system_prompt or ""
        self._tools = tools
        self._tools_usage = tools_usage or {}
        self._exclude_tools = exclude_tools
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._llm = llm or LLMClient()
        self._chat_store = chat_store
        self._consolidator = consolidator
        self._local_tools = local_tools or ToolRegistry("Agent-scoped local tools.")
        self._kind = kind
        self._name = agent_name or kind
        self._plugins = plugins
        self._plugins_prompt = plugins_prompt or {}
        self._current_context: TurnContext | None = None
        self._tool_noise_filter = tool_noise_filter
        self._compaction_lock = chat_compaction_lock
        self._history_attribution = history_attribution
        self._workspace = str(workspace) if workspace else None

        # Compose interceptors: plugin interceptors first, then configured harness/workspace interceptors
        plugin_interceptors = [i for plugin in self._plugins for i in plugin.get_interceptors()]
        self._interceptor = _CompositePluginInterceptor(
            plugin_interceptors=plugin_interceptors,
            fallback=interceptor or ChainInterceptor(),
        )

        # Register plugin tools into the agent-local tool registry
        for plugin in self._plugins:
            plugin.register_tools(self._local_tools)

    @property
    def name(self) -> str:
        return self._name

    async def ask(
        self,
        chat_id: str,
        content: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: EventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
    ) -> str:
        llm_params = {
            "model": self._model,
            "reasoning_effort": self._reasoning_effort,
        } | (llm_args or {})
        budget_model = llm_params.get("model")

        ctx = TurnContext(
            agent_name=self._name,
            chat_id=chat_id,
            turn_id=turn_id or uuid.uuid4().hex,
            history=await self._load_and_compact_history(chat_id, budget_model=budget_model),
            tool_defs=self._get_tool_defs(),
            event_sink=event_sink,
            metadata=(ctx_metadata or {}).copy(),
            current_message_projector=self._project_current_message,
        )
        self._current_context = ctx
        ctx.set_system_prompt(await self._build_system_prompt())
        user_message_metadata = ctx.metadata.get("user_message_metadata")
        ctx.add_message(
            {"role": "user", "content": content or ""},
            **(user_message_metadata if isinstance(user_message_metadata, dict) else {}),
        )
        turn_status: Literal["running", "completed", "aborted", "error"] = "running"
        abort_reason: str | None = None

        cache_index = 0

        def _message_metadata(message: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
            if metadata is not None:
                return metadata
            if message.get("role") == "assistant" and isinstance(ctx.metadata.get("assistant_message_metadata"), dict):
                return ctx.metadata["assistant_message_metadata"]
            return None

        def _add_message(message: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
            nonlocal cache_index
            ctx.add_message(_compact(message), **(_message_metadata(message, metadata) or {}))
            cache_index -= 1

        def _cache_control_injection_points() -> list[dict[str, Any]]:
            hints = [{"location": "message", "role": "system"}]
            if cache_index == 0:
                return hints
            # ctx.ephemeral is appended after persisted/current messages in the
            # LLM input, so negative indexes that target current/history messages
            # must be shifted left by the ephemeral tail length.
            hints.append({"location": "message", "index": cache_index - len(ctx.ephemeral)})
            return hints

        def _aborted_turn_messages(reason: str | None) -> list[Message]:
            user_messages = [message for message in ctx.current if message.llm_message.get("role") == "user"]
            if not user_messages:
                return []
            return [
                *user_messages,
                Message(
                    llm_message={"role": "assistant", "content": ABORTED_TURN_CONTENT},
                    turn_id=ctx.turn_id,
                    metadata={
                        "turn_status": "aborted",
                        "aborted": True,
                        "abort_reason": reason or "unknown",
                    },
                ),
            ]

        def _has_final_assistant_response() -> bool:
            if ctx.final_content is None or not ctx.current:
                return False
            last_message = ctx.current[-1].llm_message
            return (
                last_message.get("role") == "assistant"
                and not last_message.get("tool_calls")
                and last_message.get("content") == ctx.final_content
            )

        async def _persist_turn() -> None:
            if turn_status == "aborted" and not _has_final_assistant_response():
                messages = _aborted_turn_messages(abort_reason)
            else:
                messages = ctx.current
            if messages:
                commit = await self._chat_store.commit_turn(chat_id, messages, turn_id=ctx.turn_id)
                if commit_observer is not None:
                    result = commit_observer(commit)
                    if inspect.isawaitable(result):
                        await result

        async def _run_interceptor(stage: str):
            try:
                await self._interceptor.intercept(stage, ctx)
            except AbortTurn:
                raise
            except Exception as e:
                logger.error(
                    "Error in interceptor: [chat_id: %s, turn_id: %s, stage: %s] %s",
                    ctx.chat_id,
                    ctx.turn_id,
                    stage,
                    e,
                    exc_info=True,
                )

        async def _emit_event(
            event_type: str,
            phase: str,
            *,
            stage: str | None = None,
            detail: str | None = None,
            tool_name: str | None = None,
            content: str | None = None,
            summary: str | None = None,
            tool_calls: list[dict[str, Any]] | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            if event_sink is None:
                return
            try:
                await event_sink.emit(
                    TurnEvent(
                        event_type=event_type,
                        phase=phase,
                        chat_id=ctx.chat_id,
                        turn_id=ctx.turn_id,
                        agent_name=self._name,
                        stage=stage,
                        detail=detail,
                        tool_name=tool_name,
                        content=content,
                        summary=summary,
                        tool_calls=tool_calls,
                        metadata=metadata or {},
                    )
                )
            except Exception:
                logger.debug("Event sink emit error", exc_info=True)

        async def _interrupt():
            if interrupt and (llm_message := await _apply_async(interrupt, {})):
                ctx.add_message(llm_message, merge=True)

        try:
            await _run_interceptor("prepare")
            await _emit_event("turn", "start", stage="prepare", detail="start")
            iteration = 0
            while True:
                if iteration >= self._max_iterations:
                    # max iterations reached
                    ctx.add_message({"role": "assistant", "content": "(max iterations reached)"})
                    await _run_interceptor("max_iteration")
                    await _emit_event("turn", "fail", stage="max_iteration", detail="max_iteration")
                    break
                iteration += 1
                await _interrupt()
                ctx.set_system_prompt(await self._build_system_prompt())
                await _run_interceptor("before_llm")
                await _emit_event(
                    "llm", "start", stage="before_llm", detail="thinking",
                    metadata={"iteration": iteration, "max_iterations": self._max_iterations},
                )

                ctx.current_llm_response = response = await self._llm.complete(
                    ctx.get_llm_messages(),
                    tools=ctx.tool_defs,
                    cache_control_injection_points=_cache_control_injection_points(),
                    **llm_params,
                )
                cache_index = -1
                await _run_interceptor("after_llm")

                # Emit one unified LLM response event carrying both the
                # model's thinking content and any tool calls.
                thinking = _resolve_thinking(response)
                await _emit_event(
                    "llm",
                    "finish",
                    stage="after_llm",
                    detail="tool_calls" if response.tool_calls else "response_ready",
                    content=thinking,
                    tool_calls=(
                        [{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls]
                        if response.tool_calls
                        else None
                    ),
                    metadata={"finish_reason": response.finish_reason},
                )
                if not response.tool_calls:
                    # finalize the response if there is no tool call required
                    final_content = _strip_think(response.content)
                    if response.finish_reason == "error":
                        logger.error("Error in LLM response: %s", response.finish_reason)
                        final_content = final_content or "(LLM responds error)"
                    else:
                        final_content = final_content or "(empty model response)"
                    _add_message(
                        {
                            "role": "assistant",
                            "content": final_content,
                            "reasoning_content": response.reasoning_content,
                            "thinking_blocks": response.thinking_blocks,
                        },
                        metadata=(
                            ctx.metadata.get("assistant_message_metadata")
                            if isinstance(ctx.metadata.get("assistant_message_metadata"), dict)
                            else None
                        ),
                    )
                    ctx.final_content = final_content
                    await _run_interceptor("final_response")
                    await _emit_event(
                        "response",
                        "finish",
                        stage="final_response",
                        detail="final",
                        content=final_content,
                    )
                    break

                # call tools and send bake to the llm
                tool_call_dicts = [tc.to_openai_call() for tc in response.tool_calls]
                _add_message({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": tool_call_dicts,
                    "reasoning_content": response.reasoning_content,
                    "thinking_blocks": response.thinking_blocks,
                })

                for batch in self._tool_call_batches(response.tool_calls):
                    for tc in batch:
                        await _emit_event(
                            "tool",
                            "start",
                            detail="tool_call",
                            tool_name=tc.name,
                            content=json.dumps(tc.arguments, default=str),
                        )
                    if len(batch) > 1:
                        tool_results = await asyncio.gather(
                            *(self._call_tool(tc, ctx, event_sink=event_sink) for tc in batch)
                        )
                    else:
                        tool_results = [await self._call_tool(batch[0], ctx, event_sink=event_sink)]
                    for tc, tool_result in zip(batch, tool_results):
                        _add_message({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": tool_result,
                        })
                        await _run_interceptor("after_tool")
                        await _emit_event(
                            "tool",
                            "finish",
                            stage="after_tool",
                            detail="tool_result",
                            tool_name=tc.name,
                            content=tool_result,
                        )
            turn_status = "completed"
        except AbortTurn:
            turn_status = "aborted"
            abort_reason = "abort_turn"
            ctx.final_content = ABORTED_TURN_CONTENT
        except asyncio.CancelledError:
            turn_status = "aborted"
            abort_reason = "cancelled"
            raise
        except Exception as e:
            turn_status = "error"
            logger.error("Error in agent: %s", e, exc_info=True)
            ctx.add_message({"role": "assistant", "content": f"(error: {e})"})
            await _run_interceptor("error")
            await _emit_event("turn", "fail", detail="error", content=str(e))
        finally:
            await _persist_turn()
        return ctx.final_response

    def _get_tool_defs(self) -> list[dict[str, Any]]:
        tool_defs = ep_tool.to_openai_schema() | self._local_tools.to_openai_schema()
        return list(_pick_collection(tool_defs, self._tools, self._exclude_tools).values())

    def _tool_metadata(self, tool_name: str) -> dict[str, Any]:
        if self._local_tools.has(tool_name):
            return self._local_tools.metadata_for(tool_name)
        if ep_tool.has(tool_name):
            return ep_tool.metadata_for(tool_name)
        return {}

    def _tool_parallel_safe(self, tool_name: str) -> bool:
        return bool(self._tool_metadata(tool_name).get("parallel_safe", False))

    def _tool_call_batches(self, tool_calls: Sequence[ToolCallRequest]) -> list[list[ToolCallRequest]]:
        batches: list[list[ToolCallRequest]] = []
        pending_safe: list[ToolCallRequest] = []
        for tc in tool_calls:
            if self._tool_parallel_safe(tc.name):
                pending_safe.append(tc)
                continue
            if pending_safe:
                batches.append(pending_safe)
                pending_safe = []
            batches.append([tc])
        if pending_safe:
            batches.append(pending_safe)
        return batches

    async def _call_tool(self, tc: ToolCallRequest, ctx: TurnContext, event_sink: EventSink | None = None) -> str:
        try:
            if not _allowed(tc.name, self._tools, self._exclude_tools):
                raise Exception(f"Tool {tc.name} is not allowed")
            return await self._invoke_tool(
                tc.name,
                **tc.arguments,
                chat_id=ctx.chat_id,
                turn_id=ctx.turn_id,
                event_sink=event_sink,
            )
        except Exception as e:
            logger.error("Error in tool call [%s]: %s", tc.name, e)
            return str(e)

    async def _invoke_tool(self, tool_name: str, **params: Any) -> str:
        """Invoke a tool by name, merging runtime context into its parameters."""
        chat_id = params.get("chat_id", "")
        turn_id = params.get("turn_id", "")
        event_sink = params.get("event_sink")
        kwargs = params | {
            "chat_id": params.pop("chat_id", ""),
            "turn_id": params.pop("turn_id", ""),
            "event_sink": params.pop("event_sink", None),
            "context": ToolContext(
                agent_name=self._name,
                chat_id=chat_id,
                turn_id=turn_id,
                event_sink=event_sink,
            ),
        }
        if self._local_tools.has(tool_name):
            return await self._local_tools.invoke_async(tool_name, kwargs)
        if ep_tool.has(tool_name):
            return await ep_tool.invoke_async(tool_name, kwargs)
        raise Exception(f"Tool {tool_name} not found")

    async def _load_and_compact_history(self, chat_id: str, *, budget_model: str | None) -> list[dict[str, Any]]:
        result = await self._chat_store.get_context(
            chat_id, tokenizer_model=budget_model, filter_mode=self._tool_noise_filter
        )
        if result.estimated_tokens > self._max_tokens and self._compaction_lock is not None:
            async with self._compaction_lock(chat_id):
                # Re-check after acquiring lock (race guard)
                result = await self._chat_store.get_context(
                    chat_id, tokenizer_model=budget_model, filter_mode=self._tool_noise_filter
                )
                if result.estimated_tokens > self._max_tokens:
                    compaction_msgs = await self._chat_store.get_compaction_messages(
                        chat_id, filter_mode=self._tool_noise_filter
                    )
                    summary = await self._consolidator.consolidate(compaction_msgs)
                    await self._chat_store.save_summary(chat_id, summary)
                    result = await self._chat_store.get_context(
                        chat_id, tokenizer_model=budget_model, filter_mode=self._tool_noise_filter
                    )
        return self._format_history(result)

    def _project_current_message(self, message: Message) -> dict[str, Any]:
        """Render attribution labels on current-turn user messages sent to the LLM.

        Mirrors the history projection, except the workdir label always
        renders here so the latest message carries the active workdir even
        when history suppresses repeats (:meth:`_format_history`). Stored
        content stays raw; the label is re-derived from metadata on every
        projection.
        """
        llm_message = message.llm_message
        if llm_message.get("role") != "user" or not isinstance(llm_message.get("content"), str):
            return llm_message
        label = _history_attribution_label("user", message.metadata, actor_attribution=self._history_attribution)
        if not label:
            return llm_message
        return llm_message | {"content": f"{label}\n{llm_message['content']}"}

    def _format_history(self, result: ContextResult) -> list[dict[str, Any]]:
        """Hook for subclasses to customise history projection (e.g. attribution labels).

        Actor labels (``[user -> X]``, ``[assistant: X]``) render only when
        ``history_attribution`` is enabled. Workdir labels render only when the
        workdir changes between user messages — repeating an unchanged workdir
        on every message is noise; the current-turn projection always carries
        the active workdir (see :meth:`_project_current_message`).
        """
        if len(result.messages) != len(result.source_messages):
            return result.messages
        formatted: list[dict[str, Any]] = []
        last_workdir: str | None = None
        for projected, source in zip(result.messages, result.source_messages, strict=True):
            include_workdir = False
            if projected.get("role") == "user":
                workdir = _metadata_str(source.metadata, "workdir")
                include_workdir = workdir is not None and workdir != last_workdir
                last_workdir = workdir
            formatted.append(
                _attribute_history_message(
                    projected,
                    source,
                    current_agent=self._name,
                    actor_attribution=self._history_attribution,
                    include_workdir=include_workdir,
                )
            )
        return formatted

    async def _build_system_prompt(self) -> str:
        system_sections = [self._system_prompt]
        # Plugin prompt sections, in resolved plugin order
        for plugin in self._plugins:
            if plugin.name in self._plugins_prompt:
                section = self._plugins_prompt[plugin.name]
            else:
                section = await plugin.get_system_prompt_section(self._current_context)
            if section:
                system_sections.append(section)
        sections = [
            self._prompt_section_base(system_sections),
            await self._prompt_section_tools(),
            self._prompt_section_system_info(),
        ]
        return "\n\n".join(s for s in sections if s)

    def _prompt_section_base(self, sections: Sequence[str] | None = None) -> str:
        content = "\n\n".join(s for s in (sections or [self._system_prompt]) if s)
        return f"<system_prompt>\n{content}\n</system_prompt>"

    async def _prompt_section_tools(self) -> str:
        all_tools = ep_tool.describe_usage() | self._local_tools.describe_usage()
        available_tools = self._limit_prompt_collection(
            _pick_collection(all_tools, self._tools, self._exclude_tools),
            "tools",
        )
        available_tools = {k: self._tools_usage.get(k, v).strip() for k, v in available_tools.items()}

        if not available_tools:
            return "<available_tools>\n\n</available_tools>"
        section = "<available_tools>\n"
        section += "\n\n".join(
            f'<tool name="{_xml_attr(name)}">\n{escape(str(content)).strip()}\n</tool>'
            for name, content in available_tools.items()
        )
        section += "\n</available_tools>"
        return section

    def _prompt_section_system_info(self) -> str:
        workspace_line = f"<workspace>{escape(self._workspace)}</workspace>\n" if self._workspace else ""
        return (
            "<system_info>\n"
            f"<platform>{platform.system()}</platform>\n"
            f"<date>{datetime.now().strftime('%A, %B %d, %Y')}</date>\n"
            f"{workspace_line}"
            "</system_info>"
        )

    @staticmethod
    def _limit_prompt_collection(collection: dict[str, Any], kind: str) -> dict[str, Any]:
        try:
            limit = int(os.environ.get("BOS_CAPABILITY_LIMIT", 50))
        except Exception:
            limit = 50
            logger.warning("Env variable BOS_CAPABILITY_LIMIT should be a valid integer number.")

        if len(collection) <= limit:
            return collection
        logger.warning(
            "Rendering only the first %d %s in the system prompt; %d are available.",
            limit,
            kind,
            len(collection),
        )
        return dict(list(collection.items())[:limit])
