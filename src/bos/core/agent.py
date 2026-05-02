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
from bos.protocol.content import content_length

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
from .events import derive_event_sink
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
        maxims: list[str] | None = None,
        exclude_maxims: list[str] | None = None,
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
        self._maxims = maxims and [m.lower() for m in maxims]
        self._exclude_maxims = exclude_maxims
        self._subagents = subagents
        self._exclude_subagents = exclude_subagents
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations

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

    async def _invoke_tool(self, tool_name: str, **params: Any) -> str:
        """Invoke a local tool by name — used by tests."""
        kwargs = {
            "chat_id": "",
            "turn_id": "",
            "event_sink": None,
            "tool_config": self._tool_configs.get(tool_name, {}),
        } | params
        if self._local_tools.has(tool_name):
            return await self._local_tools.invoke_async(tool_name, kwargs)
        return await ep_tool.invoke_async(tool_name, kwargs)

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
            await self._prompt_section_maxims(),
            await self._prompt_section_tools(),
            await self._prompt_section_skills(),
            await self._prompt_section_subagents(),
            self._prompt_section_memory_usage(),
            self._prompt_section_system_info(),
        ]
        return "\n\n".join(s for s in sections if s)

    def _prompt_section_base(self) -> str:
        return "--- SYSTEM PROMPT ---\n\n" + self._system_prompt

    _MAXIM_SCOPES = {
        "user": "your knowledge about the user — preferences, background, projects, style",
        "soul": "your character and operating philosophy — how you work, communicate, and make decisions",
        "identity": "who you are — your role, purpose, and context",
        "rules": "hard constraints — things you must always or never do",
    }

    def _format_maxim_header(self, key: str) -> str:
        scope = self._MAXIM_SCOPES.get(key, "")
        if scope:
            return f"* **{key}** ({scope})\n"
        return f"* **{key}**\n"

    async def _prompt_section_maxims(self) -> str:
        maxims = _pick_collection(
            await self._memory.list_maxims(),
            self._maxims,
            self._exclude_maxims,
        )
        if not maxims:
            return ""
        section = "--- MAXIMS ---\n\n"
        for key, content in maxims.items():
            section += self._format_maxim_header(key)
            section += f"```\n{content}\n```\n\n"
        return section

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

        section = f"--- {title} ---\n\n"
        for key, content in items.items():
            section += f"* **{key}**\n"
            section += f"```\n{content}\n```\n\n"
        return section

    def _prompt_section_system_info(self) -> str:
        return (
            "--- SYSTEM INFORMATION ---\n\n"
            f"- Platform: {platform.system()}\n"
            f"- Date: {datetime.now().strftime('%A, %B %d, %Y')}\n"
        )

    def _prompt_section_memory_usage(self) -> str:
        return """--- USING YOUR MEMORY ---

You have two kinds of memory, accessed through three tools: Remember, Recall, and Forget.

## Maxims (your principles)

Maxims are deeply held convictions that shape how you behave and make decisions.
They are always visible to you — they appear above in the MAXIMS section.
Think of them as your conscience, not your notepad.

Each maxim has a defined scope, described in its header. Respect the scope —
put user preferences in "user", not in "rules".

Use Remember(key, content) to update a maxim when:

- The user explicitly asks you to change how you operate.
  Example: "From now on, always use TypeScript instead of JavaScript."
  → Remember(key="user", content="User prefers TypeScript over JavaScript for all projects.")

- You discover a fundamental truth about the user that should change your default behavior.
  Example: The user always rejects async solutions in favor of sync alternatives.
  → Remember(key="user", content="User prefers sync patterns. Default to sync unless async is required.")

- You are given a new rule or constraint you must follow.
  Example: "Never deploy on Fridays."
  → Remember(key="rules", content="Never deploy on Fridays. Deployments only on Mon-Thu before 14:00 UTC.")

Do NOT use maxims for:
- Facts about projects, tools, or APIs — those are memories.
- Meeting notes, code snippets, URLs — those are memories.
- Anything you might need once or twice but doesn't change who you should be.
- New categories of information — maxim keys are fixed. Use memory tags instead.

When updating a maxim, you are overwriting the ENTIRE content. Include all existing
information alongside your changes. The system cannot merge for you — you must read
the current maxim content (visible above), incorporate the new information, and write
the complete updated text.

Each maxim has a hard limit of 2048 characters. If your update would exceed this,
you must summarize and prioritize rather than expanding. Focus on what matters most
for shaping your future behavior. The system will reject writes that exceed the limit.

## Memories (your knowledge)

Memories are facts, experiences, and details you accumulate over time.
They are NOT visible to you by default — you must Recall them when needed.
Think of them as a searchable notebook, not your working memory.

Use Remember(content, tags?) to record a memory when:

- You learn a factual detail that might matter later.
  Example: "The user's database is on AWS RDS, us-east-1, PostgreSQL 16."
  → Remember(content="User's prod DB: AWS RDS us-east-1, PostgreSQL 16, "
      "pgbouncer.", tags=["infra", "database"])

- The user shares context you should carry forward across sessions.
  Example: "We're building a CLI tool for managing Kubernetes secrets."

- You complete a task and want to record the outcome for future reference.
  Example: "Deployed v2.3.1 to staging. All tests passed. Rollback window: 24h."

Use Recall(query, top_k?) to search your memories:

- Before answering a question that might depend on past context.
  Example: The user asks "what's the status of the migration?" → Recall(query="migration status")

- When the user references something you don't fully remember.
  Example: "Remember that bug we fixed last month?"

- After Recall returns snippets: if a snippet looks relevant and you need full detail,
  fetch it with Recall(entry_id=...).

Use Forget(entry_id) or Forget(query) to remove memories:

- The user explicitly asks you to forget something.
  Example: "Stop bringing up project X — we're done with it."
  → Recall(query="project X") → identify entries → Forget(entry_id=...) on each
  → Then: Remember(key="user", content="...user asked to stop referencing project X...")

- Information is clearly stale or contradicted by newer information.
  Example: Two memories contradict each other about the same topic. Keep the newer one, Forget the stale one.

## Memory hygiene

- Write memories AFTER the conversation, not during it. If you're mid-task, "
      "focus on the task. Record learnings when the user pauses or the topic concludes.
- Be concise. A memory entry is a note to your future self, not a transcript.
- Use tags. They help you find things later with Recall.
- When in doubt, write it. A slightly noisy memory is better than a lost insight.
- If you update a maxim, be thorough. Read the current content, merge carefully, and write the complete updated text."""

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
            description=(
                "Store information in your memory. Use with a 'key' parameter to update a maxim "
                "(overwrites the entire maxim — include all existing content alongside your changes). "
                "Use without a 'key' to create a new episodic memory entry. "
                "Maxim keys are: user, soul, identity, rules."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Maxim key to update. One of: user, soul, identity, rules. "
                            "If provided, 'content' overwrites the entire maxim."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to store. For maxisms, this is the complete new content.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for a new memory entry. Only used when 'key' is not provided.",
                    },
                },
                "required": ["content"],
            },
        )
        async def tool_remember(
            content: str,
            key: str | None = None,
            tags: list[str] | None = None,
        ) -> str:
            if key:
                # Maxim write
                if not _allowed(key.lower(), self._maxims, self._exclude_maxims):
                    return f"Error: Maxim '{key}' is not allowed."
                if len(content) > self.MAXIM_LIMIT:
                    return (
                        f"Error: Maxim content is {len(content)} characters, "
                        f"which exceeds the limit of {self.MAXIM_LIMIT}. "
                        f"Please summarize and try again."
                    )
                await self._memory.set_maxim(key.lower(), content)
                return f"(Maxim '{key}' updated. Content length: {len(content)}/{self.MAXIM_LIMIT} characters.)"
            else:
                # Memory ingest
                entry_id = await self._memory.ingest_memory(content, tags=tags)
                tag_note = f" Tags: {tags}." if tags else ""
                return f"(Memory stored with entry_id: {entry_id}.{tag_note})"

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
                            "ID of a specific memory entry to retrieve in full "
                            "(from previous Recall results)."
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
                footer = "\n\nUse Recall(entry_id=\"...\") to fetch the full content of any entry."
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
                    f"Remember(key=\"user\", content=\"...\") to record why you forgot it.)"
                )
            return "Error: Provide either 'entry_id' or 'query' to forget."

        @self._local_tools(
            name="AskSubagent",
            description="Delegate a task to a named subagent and return its response.",
            parameters={
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "The role (kind) of the subagent to delegate to, case sensitive."},
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
        _CAPABILITY_KEYS = ("tools", "skills", "maxims", "subagents")

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
