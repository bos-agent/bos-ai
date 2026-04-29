from __future__ import annotations

import json
import logging
import platform
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from bos.protocol import MessageContent, TurnEvent
from bos.protocol.content import content_length

from ._utils import (
    _aclose,
    _allowed,
    _apply_async,
    _as_parts,
    _compact,
    _create_extension_instance,
    _pick_collection,
    _strip_think,
)
from .contract import EventSink, Message, TurnInterceptor, ep_agent, ep_tool, ep_turn_interceptor
from .defaults import FileSystemSkillsLoader, InMemMemoryStore, InMemMessageStore, NaiveConsolidator
from .llm import LLMClient, ToolCallRequest
from .registry import ToolRegistry

if TYPE_CHECKING:
    from .contract import Consolidator, MemoryStore, MessageStore, SkillsLoader
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
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        exclude_tools: list[str] | None = None,
        skills: list[str] | None = None,
        exclude_skills: list[str] | None = None,
        memories: list[str] | None = None,
        exclude_memories: list[str] | None = None,
        subagents: list[str] | None = None,
        exclude_subagents: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
        max_tokens: int = 128 * 1024,
        max_iterations: int = 25,
        llm: LLMClient | None = None,
        message_store: MessageStore | None = None,
        memory_store: MemoryStore | None = None,
        consolidator: Consolidator | None = None,
        skills_loader: SkillsLoader | None = None,
        interceptor: TurnInterceptor | None = None,
        local_tools: ToolRegistry | None = None,
        tool_configs: dict[str, dict[str, Any]] | None = None,
        agent_name: str | None = None,
    ):
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string or None")
        self._system_prompt = system_prompt or ""
        self._tools = tools
        self._exclude_tools = exclude_tools
        self._skills = skills
        self._exclude_skills = exclude_skills
        self._memories = memories and [m.lower() for m in memories]
        self._exclude_memories = exclude_memories
        self._subagents = subagents
        self._exclude_subagents = exclude_subagents
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations

        self._llm = llm or LLMClient()
        self._message_store = message_store or InMemMessageStore()
        self._memory_store = memory_store or InMemMemoryStore()
        self._consolidator = consolidator or NaiveConsolidator()
        self._skills_loader = skills_loader or FileSystemSkillsLoader()
        self._interceptor = interceptor or ChainReactInterceptor()
        self._local_tools = local_tools or ToolRegistry("Harness-scoped tools for this agent.")
        self._tool_configs = tool_configs or {}
        self._agent_name = agent_name or "__unknown__"

        self._register_memory_tools()
        self._register_skills_tools()
        self._register_agent_tools()

    async def ask(
        self,
        chat_id: str,
        content: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: EventSink | None = None,
    ) -> str:
        ctx = TurnContext(
            chat_id=chat_id,
            turn_id=uuid.uuid4().hex,
            history=await self._get_chat_history(chat_id),
            tool_defs=self._get_tool_defs(),
            metadata=(ctx_metadata or {}).copy(),
        )
        if event_sink is not None:
            ctx.metadata["event_sink"] = event_sink
        ctx.set_system_prompt(await self._build_system_prompt())
        ctx.add_message({"role": "user", "content": content or ""})

        llm_params = {
            "model": self._model,
            "reasoning_effort": self._reasoning_effort,
        } | (llm_args or {})

        cache_index = 0

        def _add_message(message: dict[str, Any]) -> None:
            nonlocal cache_index
            ctx.add_message(_compact(message))
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
                        agent_name=self._agent_name,
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
                        }
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

            params = tc.arguments | {
                "chat_id": ctx.chat_id,
                "turn_id": ctx.turn_id,
                "event_sink": event_sink,
                "tool_config": self._tool_configs.get(tc.name, {}),
            }

            if self._local_tools.has(tc.name):
                return await self._local_tools.invoke_async(tc.name, params)

            if ep_tool.has(tc.name):
                return await ep_tool.invoke_async(tc.name, params)

            raise Exception(f"Tool {tc.name} not found")
        except Exception as e:
            logger.error("Error in tool call [%s]: %s", tc.name, e)
            return str(e)

    async def _get_chat_history(self, chat_id: str) -> list[dict]:
        def _format_content(msg: dict) -> MessageContent:
            content = msg.get("content", "")
            if msg.get("role") == "tool" and isinstance(content, str) and len(content) > 150:
                return content[:147] + "..."
            return content

        async def _get_messages() -> list[dict]:
            messages = await self._message_store.get_messages(chat_id)
            return [
                _compact(
                    {
                        "role": m.llm_message["role"],
                        "content": _format_content(m.llm_message),
                        "tool_calls": m.llm_message.get("tool_calls", None),
                        "tool_call_id": m.llm_message.get("tool_call_id", None),
                        "name": m.llm_message.get("name", None),
                    }
                )
                for m in messages
            ]

        history = await _get_messages()

        if sum(content_length(m.get("content", "")) for m in history) > self._max_tokens:
            summary = await self._consolidator.consolidate(history)
            await self._message_store.save_summary(chat_id, summary)
            history = await _get_messages()

        return history

    async def _build_system_prompt(self) -> str:
        sections = [
            self._prompt_section_base(),
            await self._prompt_section_memories(),
            await self._prompt_section_tools(),
            await self._prompt_section_skills(),
            self._prompt_section_system_info(),
        ]
        return "\n\n".join(s for s in sections if s)

    def _prompt_section_base(self) -> str:
        return "--- SYSTEM PROMPT ---\n\n" + self._system_prompt

    async def _prompt_section_memories(self) -> str:
        memories = _pick_collection(
            await self._memory_store.list_memories(),
            self._memories,
            self._exclude_memories,
        )
        if not memories:
            return ""
        section = "--- ACTIVE MEMORY ---\n\n"
        for key, content in memories.items():
            section += f"\n### {key.upper()} ###\n{content}"
        return section

    async def _prompt_section_tools(self) -> str:
        all_tools = ep_tool.describe() | self._local_tools.describe()
        available_tools = _pick_collection(all_tools, self._tools, self._exclude_tools)
        if not available_tools:
            return ""
        section = "--- AVAILABLE TOOLS ---\n\n"
        for name, info in available_tools.items():
            section += f"### {name} ###\n"
            section += f"{info}\n\n"
        return section

    async def _prompt_section_skills(self) -> str:
        available_skills = _pick_collection(
            await self._skills_loader.search_skills(), self._skills, self._exclude_skills
        )
        if not available_skills:
            return ""
        section = "--- AVAILABLE SKILLS ---\n\n"
        for name, meta in available_skills.items():
            section += f"\n### {name.upper()} ###\n\n"
            section += f"- Location: {meta.location}\n"
            section += f"- Summary: {meta.summary}"
        return section

    def _prompt_section_system_info(self) -> str:
        return (
            "--- SYSTEM INFORMATION ---\n\n"
            f"- Platform: {platform.system()}\n"
            f"- Date: {datetime.now().strftime('%A, %B %d, %Y')}\n"
        )

    def _register_memory_tools(self) -> None:
        @self._local_tools(
            name="UpdateMemory",
            description="Overwrite an allowed memory partition with the complete updated content.",
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Memory partition key. Prefer predefined keys: soul, identity, rules, "
                            "tasks, history, memory, user. Create new keys only if fundamentally distinct."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The COMPLETE content for this partition. This overwrites the existing "
                            "file, so include all existing information alongside any new updates."
                        ),
                    },
                },
                "required": ["key", "content"],
            },
        )
        async def tool_update_memory(key: str, content: str) -> str:
            if not _allowed(key.lower(), self._memories, self._exclude_memories):
                raise ValueError(f"Update memory '{key}' is not allowed.")
            await self._memory_store.save_memory(key, content)
            return f"(Successfully updated memory '{key}'.)"

    def _register_skills_tools(self) -> None:
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

        @self._local_tools(
            name="SearchSkills",
            description="Search available allowed skills by name or summary text.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. If empty, return all skills.",
                    }
                },
                "required": [],
            },
        )
        async def tool_search_skills(query: str | None = None) -> str:
            skills = await self._skills_loader.search_skills(query)
            skills = _compact(_pick_collection(skills, self._skills, self._exclude_skills))
            results = [
                {"name": name, "location": str(sm.location), "summary": sm.summary} for name, sm in skills.items()
            ]
            return json.dumps(results)

    def _register_agent_tools(self) -> None:
        @self._local_tools(
            name="ListAgents",
            description="List all available agents registered in the system.",
            parameters={"type": "object", "properties": {}},
        )
        async def tool_list_agents() -> str:
            results = ep_agent.describe()
            results.pop("_default", None)
            results = _pick_collection(results, self._subagents, self._exclude_subagents)
            return json.dumps(results)

    @classmethod
    def register(cls, name: str, description: str | None = None, **kwargs):
        _CAPABILITY_KEYS = ("tools", "skills", "memories", "subagents")

        def map_value(key, value):
            if isinstance(value, list):
                return value
            if value is None:
                return []
            if value == "*":
                return None
            raise TypeError(f"{key} must be a list, '*', or None")

        for key in _CAPABILITY_KEYS:
            kwargs[key] = map_value(key, kwargs.get(key))

        @ep_agent(name=name, description=description, defaults=kwargs)
        @wraps(ReactAgent)
        def create_react_agent(*args, **kwargs):
            return ReactAgent(*args, **kwargs)
