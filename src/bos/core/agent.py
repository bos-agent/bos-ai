from __future__ import annotations

import json
import logging
import os
import platform
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from bos.protocol import MessageContent, TurnEvent

from ._utils import (
    _aclose,
    _allowed,
    _apply_async,
    _as_parts,
    _compact,
    _create_extension_instance,
    _pick_collection,
    _safe_format,
    _strip_think,
)
from .contract import (
    EventSink,
    Message,
    TurnInterceptor,
    ep_agent,
    ep_tool,
    ep_turn_interceptor,
)
from .defaults import bos_maxims, bos_memory_usage
from .events import derive_event_sink
from .history import estimate_message_history_tokens
from .llm import LLMClient, ToolCallRequest
from .registry import ToolRegistry

if TYPE_CHECKING:
    from .contract import Consolidator, MemoryExtension, MessageStore, SkillsLoader
    from .llm import LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    chat_id: str
    turn_id: str
    system: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    current: list[Message] = field(default_factory=list)
    tool_defs: list[dict[str, Any]] = field(default_factory=list)
    current_llm_response: LLMResponse | None = None
    final_content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def set_system_prompt(self, content: MessageContent) -> None:
        self.system = [{"role": "system", "content": content}]

    def add_message(self, llm_message: dict[str, Any], *, merge: bool = False, **kwargs) -> None:
        if merge and self.current and self.current[-1].llm_message["role"] == llm_message["role"]:
            parts = _as_parts(self.current[-1].llm_message["content"]) + _as_parts(llm_message["content"])
            self.current[-1].llm_message["content"] = parts
        else:
            self.current.append(Message(llm_message=llm_message, turn_id=self.turn_id, metadata=kwargs))

    def get_messages(self) -> list[dict[str, Any]]:
        return self.system + self.history + [m.llm_message for m in self.current]

    @property
    def final_response(self) -> str:
        return self.final_content or self.current[-1].llm_message["content"] if self.current else "(no response)"


class AbortTurn(Exception):
    pass


class ChainReactInterceptor:
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


class ReactAgent:
    def __init__(
        self,
        *,
        message_store: MessageStore,
        memory: MemoryExtension,
        consolidator: Consolidator,
        skills_loader: SkillsLoader,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        exclude_tools: list[str] | None = None,
        skills: list[str] | None = None,
        exclude_skills: list[str] | None = None,
        maxims: dict[str, str] | None = None,
        memory_usage: str | None = None,
        subagents: list[str] | None = None,
        exclude_subagents: list[str] | None = None,
        name: str | None = None,
        model: str | None = None,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
        llm: LLMClient | None = None,
        local_tools: ToolRegistry | None = None,
        tool_configs: dict[str, dict[str, Any]] | None = None,
        interceptor: TurnInterceptor | None = None,
        max_tokens: int = 128 * 1024,
        max_iterations: int = 25,
    ):
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string or None")
        self._system_prompt = system_prompt or ""
        self._tools = tools
        self._exclude_tools = exclude_tools
        self._skills = skills
        self._exclude_skills = exclude_skills
        self._subagents = subagents
        self._exclude_subagents = exclude_subagents
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._maxims = (
            bos_maxims
            if maxims is None
            else _compact({key: bos_maxims.get(key) for key in maxims})
            if isinstance(maxims, list)
            else (dict(maxims))
        )
        self._memory_usage = (
            "" if memory_usage is None else bos_memory_usage if memory_usage == "*" else str(memory_usage)
        )

        self._llm = llm or LLMClient()
        self._message_store = message_store
        self._memory = memory
        self._consolidator = consolidator
        self._skills_loader = skills_loader
        self._interceptor = interceptor or ChainReactInterceptor()
        self._local_tools = local_tools or ToolRegistry("Agent-scoped local tools.")
        self._tool_configs = tool_configs or {}
        self._name = name or "__unknown__"

        self._register_tools()

    async def ask(
        self,
        chat_id: str,
        content: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: EventSink | None = None,
    ) -> str:
        llm_params = {
            "model": self._model,
            "reasoning_effort": self._reasoning_effort,
        } | (llm_args or {})
        budget_model = llm_params.get("model")

        ctx = TurnContext(
            chat_id=chat_id,
            turn_id=uuid.uuid4().hex,
            history=await self._get_chat_history(chat_id, budget_model=budget_model),
            tool_defs=self._get_tool_defs(),
            metadata=(ctx_metadata or {}).copy(),
        )
        if event_sink is not None:
            ctx.metadata["event_sink"] = event_sink
        ctx.set_system_prompt(await self._build_system_prompt())
        user_message_metadata = ctx.metadata.get("user_message_metadata")
        ctx.add_message(
            {"role": "user", "content": content or ""},
            **(user_message_metadata if isinstance(user_message_metadata, dict) else {}),
        )

        cache_index = 0

        def _add_message(message: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
            nonlocal cache_index
            ctx.add_message(_compact(message), **(metadata or {}))
            cache_index -= 1

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
            for _ in range(self._max_iterations):
                await _interrupt()
                ctx.set_system_prompt(await self._build_system_prompt())
                await _run_interceptor("before_llm")
                await _emit_event("llm", "start", stage="before_llm", detail="thinking")

                litellm_cache_hint = [{"location": "message", "role": "system"}] + (
                    [] if cache_index == 0 else [{"location": "message", "index": cache_index}]
                )
                ctx.current_llm_response = response = await self._llm.complete(
                    ctx.get_messages(),
                    tools=ctx.tool_defs,
                    cache_control_injection_points=litellm_cache_hint,
                    **llm_params,
                )
                cache_index = -1
                await _run_interceptor("after_llm")
                await _emit_event(
                    "llm",
                    "finish",
                    stage="after_llm",
                    detail="tool_calls" if response.tool_calls else "response_ready",
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
                _add_message(
                    {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                        "reasoning_content": response.reasoning_content,
                        "thinking_blocks": response.thinking_blocks,
                    }
                )

                for tc in response.tool_calls:
                    await _emit_event(
                        "tool",
                        "start",
                        detail="tool_call",
                        tool_name=tc.name,
                        content=json.dumps(tc.arguments, default=str),
                    )
                    tool_result = await self._call_tool(tc, ctx, event_sink=event_sink)
                    _add_message(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": tool_result,
                        }
                    )
                    await _run_interceptor("after_tool")
                    await _emit_event(
                        "tool",
                        "finish",
                        stage="after_tool",
                        detail="tool_result",
                        tool_name=tc.name,
                        content=tool_result,
                    )
            else:
                # max iterations reached
                ctx.add_message({"role": "assistant", "content": "(max iterations reached)"})
                await _run_interceptor("max_iteration")
                await _emit_event("turn", "fail", stage="max_iteration", detail="max_iteration")
        except AbortTurn:
            pass
        except Exception as e:
            logger.error("Error in agent: %s", e, exc_info=True)
            ctx.add_message({"role": "assistant", "content": f"(error: {e})"})
            await _run_interceptor("error")
            await _emit_event("turn", "fail", detail="error", content=str(e))

        await self._message_store.save_messages(chat_id, ctx.current)
        return ctx.final_response

    def _get_tool_defs(self) -> list[dict[str, Any]]:
        tool_defs = ep_tool.to_openai_schema() | self._local_tools.to_openai_schema()
        return list(_pick_collection(tool_defs, self._tools, self._exclude_tools).values())

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
        kwargs = params | {
            "chat_id": params.pop("chat_id", ""),
            "turn_id": params.pop("turn_id", ""),
            "event_sink": params.pop("event_sink", None),
            "tool_config": self._tool_configs.get(tool_name, {}),
        }
        if self._local_tools.has(tool_name):
            return await self._local_tools.invoke_async(tool_name, kwargs)
        if ep_tool.has(tool_name):
            return await ep_tool.invoke_async(tool_name, kwargs)
        raise Exception(f"Tool {tool_name} not found")

    async def _get_chat_history(self, chat_id: str, *, budget_model: str | None) -> list[dict]:
        messages = list(await self._message_store.get_messages(chat_id))
        projection = estimate_message_history_tokens(messages, budget_model=budget_model)

        if projection.estimated_tokens > self._max_tokens:
            summary = await self._consolidator.consolidate(messages)
            await self._message_store.save_summary(chat_id, summary)
            messages = list(await self._message_store.get_messages(chat_id))
            projection = estimate_message_history_tokens(messages, budget_model=budget_model)

        return projection.messages

    async def _build_system_prompt(self) -> str:
        sections = [
            self._prompt_section_base(),
            await self._prompt_section_maxims(),
            await self._prompt_section_tools(),
            await self._prompt_section_skills(),
            await self._prompt_section_subagents(),
            self._memory_usage,
            self._prompt_section_system_info(),
        ]
        return "\n\n".join(s for s in sections if s)

    def _prompt_section_base(self) -> str:
        return f"<system_prompt>\n{self._system_prompt}\n</system_prompt>"

    async def _prompt_section_maxims(self) -> str:
        if not self._maxims:
            return ""
        items: dict[str, str] = {}
        for key, scope in self._maxims.items():
            content = await self._memory.get_maxim(key) or "(empty)"
            items[key] = f"<scope>{scope}</scope>\n{content}" if scope else content
        return self._format_prompt_section("ACTIVE MAXIMS", items)

    async def _prompt_section_tools(self) -> str:
        all_tools = ep_tool.describe() | self._local_tools.describe()
        available_tools = self._limit_prompt_collection(
            _pick_collection(all_tools, self._tools, self._exclude_tools),
            "tools",
        )
        return self._format_prompt_section("AVAILABLE TOOLS", available_tools)

    async def _prompt_section_skills(self) -> str:
        available_skills = self._limit_prompt_collection(
            _pick_collection(await self._skills_loader.search_skills(), self._skills, self._exclude_skills),
            "skills",
        )
        return self._format_prompt_section(
            "AVAILABLE SKILLS",
            {name: meta.description for name, meta in available_skills.items()},
        )

    async def _prompt_section_subagents(self) -> str:
        available_subagents = self._limit_prompt_collection(
            _pick_collection(ep_agent.describe(), self._subagents, self._exclude_subagents),
            "subagents",
        )
        available_subagents.pop("_default", None)
        return self._format_prompt_section("AVAILABLE SUBAGENTS", available_subagents)

    @staticmethod
    def _format_prompt_section(title: str, items: dict[str, Any]) -> str:
        if not items:
            return ""

        tag = title.lower().replace(" ", "_")
        section = f"<{tag}>\n"
        for key, content in items.items():
            section += f"<item key=\"{key}\">\n{content}\n</item>\n"
        section += f"</{tag}>"
        return section

    def _prompt_section_system_info(self) -> str:
        return (
            "<system_info>\n"
            f"<platform>{platform.system()}</platform>\n"
            f"<date>{datetime.now().strftime('%A, %B %d, %Y')}</date>\n"
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

    MAXIM_LIMIT = 2048

    def _register_tools(self) -> None:
        @self._local_tools(
            name="Remember",
            description="Store a fact or detail in your episodic memory for later Recall.",
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The information to store.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorisation.",
                    },
                },
                "required": ["content"],
            },
        )
        async def tool_remember(
            content: str,
            tags: list[str] | None = None,
        ) -> str:
            entry_id = await self._memory.ingest_memory(content, tags=tags)
            tag_note = f" Tags: {tags}." if tags else ""
            return f"(Memory stored with entry_id: {entry_id}.{tag_note})"

        @self._local_tools(
            name="ReviseMaxim",
            description=(
                "Append a revision note to a maxim. Existing content is preserved; "
                "your text is added as a timestamped entry. You can only update the "
                "active maxims in your context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Maxim key. One of: user, soul, identity, rules.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The revision note to append.",
                    },
                },
                "required": ["key", "content"],
            },
        )
        async def tool_revise_maxim(key: str, content: str) -> str:
            if not _allowed(key.lower(), self._maxims):
                return f"Error: Maxim '{key}' is not allowed."
            current = await self._memory.get_maxim(key.lower())
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            revised = f"{current}\n[{ts}] {content}" if current else f"[{ts}] {content}"
            if len(revised) > self.MAXIM_LIMIT:
                return (
                    f"Error: Revision would bring maxim '{key}' to "
                    f"{len(revised)} characters (limit {self.MAXIM_LIMIT}). "
                    f"Wait for a merge cycle or keep it shorter."
                )
            await self._memory.set_maxim(key.lower(), revised)
            return f"(Revision appended to maxim '{key}'. Total size: {len(revised)}/{self.MAXIM_LIMIT} characters.)"

        @self._local_tools(
            name="Recall",
            description=(
                "Retrieve information from your memories. Use with a 'query' to search "
                "(returns snippets of matching entries). Use with an 'entry_id' to fetch "
                "the full content of a specific entry after searching."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant memories.",
                    },
                    "entry_id": {
                        "type": "string",
                        "description": (
                            "ID of a specific memory entry to retrieve in full (from previous Recall results)."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return when searching (default: 5).",
                    },
                },
                "required": [],
            },
        )
        async def tool_recall(
            query: str | None = None,
            entry_id: str | None = None,
            top_k: int = 5,
        ) -> str:
            if entry_id:
                entry = await self._memory.get_memory(entry_id)
                if entry is None:
                    return f"(No memory found with entry_id: {entry_id}.)"
                return (
                    f"Memory entry {entry_id}:\n---\n{entry.content}\n---\n"
                    f"Tags: {entry.tags}\nCreated: {entry.created_at}"
                )
            if query:
                entries = await self._memory.search_memories(query, top_k=top_k)
                if not entries:
                    return f"(No memories found for '{query}'.)"
                results = []
                for e in entries:
                    snippet = e.content[:200] + "..." if len(e.content) > 200 else e.content
                    results.append(f"[{e.id}] {snippet}\n    Tags: {e.tags}")
                header = f"Found {len(entries)} memories for '{query}':\n\n"
                footer = '\n\nUse Recall(entry_id="...") to fetch the full content of any entry.'
                return header + "\n\n".join(results) + footer
            return "Error: Provide either 'query' to search or 'entry_id' to fetch a specific entry."

        @self._local_tools(
            name="Forget",
            description=(
                "Remove information from your memory. Use with an 'entry_id' to remove a specific "
                "memory. Use with a 'query' to search and remove all matching memories."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": "ID of a specific memory entry to remove.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query — all matching memories will be removed.",
                    },
                },
                "required": [],
            },
        )
        async def tool_forget(
            entry_id: str | None = None,
            query: str | None = None,
        ) -> str:
            if entry_id:
                await self._memory.forget_memory(entry_id)
                return f"(Memory entry {entry_id} forgotten.)"
            if query:
                entries = await self._memory.search_memories(query, top_k=20)
                if not entries:
                    return f"(No memories found for '{query}' — nothing to forget.)"
                count = len(entries)
                for e in entries:
                    await self._memory.forget_memory(e.id)
                return (
                    f"(Forgot {count} memory entries matching '{query}'. "
                    f"If the user asked you to stop referencing something, consider using "
                    f'Remember(key="user", content="...") to record why you forgot it.)'
                )
            return "Error: Provide either 'entry_id' or 'query' to forget."

        @self._local_tools(
            name="AskSubagent",
            description="Delegate a task to a named subagent and return its response.",
            parameters={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "The role (kind) of the subagent to delegate to, case sensitive.",
                    },
                    "message": {"type": "string", "description": "Task or message to send."},
                },
                "required": ["role", "message"],
            },
        )
        async def tool_ask_subagent(
            role: str,
            message: str | None = None,
            task: str | None = None,
            chat_id: str = "",
            turn_id: str = "",
            event_sink: EventSink | None = None,
        ) -> str:
            if not _allowed(role, self._subagents, self._exclude_subagents):
                return f"Error: Agent '{role}' is not an allowed subagent."
            if not ep_agent.has(role):
                return f"Error: Agent '{role}' not found."

            from .harness import CURRENT_HARNESS

            harness = CURRENT_HARNESS.get(None)
            if harness is None:
                return "Error: AskSubagent requires an active AgentHarness."

            child_message = message if message is not None else task
            if not child_message:
                return "Error: AskSubagent requires a non-empty message."

            subagent_cfg = harness._get_subagent_config(role)
            if task_template := subagent_cfg.get("task_template"):
                child_message = _safe_format(
                    task_template,
                    task=child_message,
                    message=child_message,
                    role=role,
                    workspace=harness.workspace,
                )

            child_chat_id = harness._make_subagent_chat_id(chat_id, role)
            child_agent_cfg = {k: v for k, v in subagent_cfg.items() if k not in {"name", "task_template"}}
            agent = harness.create_agent(role, child_agent_cfg)
            child_event_sink = derive_event_sink(
                event_sink,
                parent_turn_id=turn_id,
                parent_chat_id=chat_id,
                parent_agent_name=self._name,
            )
            return await agent.ask(
                child_chat_id,
                child_message,
                ctx_metadata={"subagent": role, "ref_chat_id": chat_id},
                event_sink=child_event_sink,
            )

        @self._local_tools(
            name="LoadSkill",
            description="Read an allowed skill's full instructions and return them as the tool result.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill name"}},
                "required": ["name"],
            },
        )
        async def tool_load_skill(name: str) -> str:
            if not _allowed(name, self._skills, self._exclude_skills):
                raise ValueError(f"Skill '{name}' is not allowed.")
            try:
                return await self._skills_loader.load_skill(name)
            except Exception as ex:
                return f"(Failed to load skill '{name}': {ex}.)"

    @classmethod
    def register(cls, name: str, description: str | None = None, **kwargs):
        _CAPABILITY_KEYS = ("tools", "skills", "subagents", "maxims")

        def map_value(key, value):
            if isinstance(value, (list, dict)):
                return value
            if value is None:
                return []  # mute the capability
            if value == "*":
                return None  # fully open the capability
            raise TypeError(f"{key} must be a list, '*', or None")

        for key in _CAPABILITY_KEYS:
            kwargs[key] = map_value(key, kwargs.get(key))

        @ep_agent(name=name, description=description, defaults=kwargs)
        @wraps(ReactAgent)
        def create_react_agent(*args, **kwargs):
            return ReactAgent(*args, **kwargs)
