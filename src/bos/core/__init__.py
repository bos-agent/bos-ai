"""
Lightweight single-file agent framework.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import platform
import re
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Literal, Protocol, runtime_checkable

from bos.core.actor import AgentActor as _AgentActor
from bos.core.harness import (
    CURRENT_HARNESS as _CURRENT_HARNESS,
)
from bos.core.harness import (
    AgentHarness as _AgentHarness,
)
from bos.core.harness import (
    bootstrap_platform as _bootstrap_platform,
)
from bos.core.llm import LLMClient, LLMResponse, ToolCallRequest
from bos.core.llm import ep_provider as ep_provider
from bos.core.registry import Extension as Extension
from bos.core.registry import ExtensionPoint as ExtensionPoint
from bos.core.registry import ToolRegistry as ToolRegistry
from bos.protocol import Envelope

AgentActor = _AgentActor
AgentHarness = _AgentHarness
CURRENT_HARNESS = _CURRENT_HARNESS
bootstrap_platform = _bootstrap_platform

__version__ = "0.1.0"


logger = logging.getLogger("bos")


# ═══════════════════════════════════════════════════════════════
#  CLOSEABLE PROTOCOL
# ═══════════════════════════════════════════════════════════════


@runtime_checkable
class Closeable(Protocol):
    """Opt-in cleanup contract for extensions that hold resources."""

    async def aclose(self) -> None: ...


# ═══════════════════════════════════════════════════════════════
#  TOOLS
# ═══════════════════════════════════════════════════════════════


ep_tool = ToolRegistry(description="Tool. An async function could be invoked by llm.")


# ═══════════════════════════════════════════════════════════════
#  MESSAGE STORE
# ═══════════════════════════════════════════════════════════════


ep_message_store = ExtensionPoint(
    description="""
        Message store. A factory that creates message stores implementing the MessageStore protocol.
    """
)


@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    is_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MessageStore(Protocol):
    async def save_messages(self, conversation_id: str, messages: Iterable[Message]) -> None: ...
    async def get_messages(self, conversation_id: str, original: bool = False) -> Iterable[Message]: ...
    async def save_summary(self, conversation_id: str, summary: str) -> None: ...
    async def list_conversations(self) -> dict[str, dict[str, Any]]: ...


@ep_message_store(name="_default")
class InMemMessageStore:
    """In-process memory store for conversation and long-term notes."""

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}

    async def save_messages(self, conversation_id: str, messages: list[Message]) -> None:
        self._messages.setdefault(conversation_id, []).extend(messages)

    async def get_messages(self, conversation_id: str, original: bool = False) -> list[Message]:
        if original:
            return [m for m in self._messages.get(conversation_id, []) if not m.is_summary]
        result = []
        for m in reversed(self._messages.get(conversation_id, [])):
            if m.is_summary:
                result.append(m)
                break
            result.append(m)
        result.reverse()
        return result

    async def save_summary(self, conversation_id: str, summary: str) -> None:
        self._messages.setdefault(conversation_id, []).append(
            Message(llm_message={"role": "system", "content": f"Conversation summary:\n{summary}"}, is_summary=True)
        )

    async def list_conversations(self) -> dict[str, Any]:
        contexts = {}
        for conversation_id, messages in self._messages.items():
            if not (m := next((m for m in messages if m.llm_message["role"] == "user"), None)):
                m = messages[0]
            contexts[conversation_id] = {
                "description": m.llm_message["content"],
                "created_at": m.created_at,
                "last_activity": messages[-1].created_at,
                "message_count": len(messages),
            }
        return contexts


# ═══════════════════════════════════════════════════════════════
#  MEMORY STORE
# ═══════════════════════════════════════════════════════════════


ep_memory_store = ExtensionPoint(
    description="""
        Memory store. A factory that creates memory stores implementing the MemoryStore protocol.
    """
)


@runtime_checkable
class MemoryStore(Protocol):
    async def load_memory(self, key: str) -> str: ...
    async def save_memory(self, key: str, content: str) -> None: ...
    async def list_memories(self) -> dict[str, str]: ...
    async def search_memory(self, query: str) -> dict[str, str]: ...


@ep_memory_store(name="_default")
class InMemMemoryStore:
    """In-memory store for long-term agent identity and rules."""

    def __init__(self, **memories: str) -> None:
        self._mem = {k.lower(): v for k, v in memories.items()}

    async def load_memory(self, key: str) -> str:
        return self._mem.get(key.lower(), "")

    async def save_memory(self, key: str, content: str) -> None:
        self._mem[key.lower()] = content

    async def list_memories(self) -> dict[str, str]:
        return self._mem.copy()

    async def search_memory(self, query: str) -> dict[str, str]:
        return {key: txt for key, txt in self._mem.items() if query.lower() in txt.lower()}


# ═══════════════════════════════════════════════════════════════
#  Content CONSOLIDATOR
# ═══════════════════════════════════════════════════════════════


ep_consolidator = ExtensionPoint(
    description="""
        Content consolidator. A factory that creates consolidators implementing the Consolidator protocol.
    """
)


@runtime_checkable
class Consolidator(Protocol):
    async def consolidate(self, messages: list[dict], instruction: str | None = None) -> str: ...


@ep_consolidator(name="_default")
class NaiveConsolidator:
    """Naive content consolidator that take the last 10 messages and concatenate them."""

    async def consolidate(self, messages: list[dict], instruction: str | None = None) -> str:
        summary = None
        for role, content in ((m.get("role"), m.get("content", "")) for m in messages if not m.get("tool_calls")):
            if summary is None and role not in ["user", "system"]:
                continue
            summary = (summary or "") + (content if role == "system" else f"{role}: {content.strip()}") + "\n"
        return summary.strip()


# ═══════════════════════════════════════════════════════════════
#  SKILLS
# ═══════════════════════════════════════════════════════════════


ep_skills_loader = ExtensionPoint(
    description="""
        Skills Loader. A factory that creates skills loaders implementing the SkillsLoader protocol.
    """
)


@runtime_checkable
class SkillsLoader(Protocol):
    async def load_skill(self, name: str) -> str: ...
    async def search_skills(self, query: str) -> list[str]: ...


@ep_skills_loader(name="_default")
class FileSystemSkillsLoader:
    def __init__(self, skill_dirs: Iterable[Path | str] | None = None) -> None:
        self._skill_dirs = [(Path(__file__).parent / "skills").resolve()]
        self._skill_dirs.extend(Path(dir).expanduser().resolve() for dir in skill_dirs or [])
        self._skills: dict[str, dict[str, Any]] | None = None

    async def list_skills(self) -> dict[str, dict[str, Any]]:
        if self._skills is None:
            self._skills = await self._load_skill_summary()
        return self._skills

    async def load_skill(self, name: str) -> tuple[Path, str]:
        skills = await self.list_skills()
        if (s := skills.get(name)) and (text := _read_text(s.get("path"))):
            return s["path"].parent, text

    async def search_skills(self, query: str | None = None) -> dict[str, str]:
        results = {name: data.get("summary", "") for name, data in (await self.list_skills()).items()}
        if query:
            query = query.lower()
            return {name: data for name, data in results.items() if query in name.lower() or query in data.lower()}
        return results

    async def _load_skill_summary(self) -> dict[str, dict[str, Any]]:
        def _iter_skill_files() -> Iterable[tuple[str, Path]]:
            for d in self._skill_dirs:
                if (d / "SKILL.md").exists():
                    yield d.name, d / "SKILL.md"
                if d.is_dir():
                    for c in d.iterdir():
                        if c.is_dir() and (c / "SKILL.md").exists():
                            yield c.name, c / "SKILL.md"

        skills = {}
        for skill_name, path in _iter_skill_files():
            content = path.read_text(encoding="utf-8")
            # get the frontmatter
            if frontmatter := re.match(r"^---\n(.*?)\n---", content, re.DOTALL):
                summary = frontmatter.group(1)
            else:
                # get th first a few un-empty lines. stop when total length exceeds 150 characters.
                summary = ""
                for line in (line.strip() for line in content.splitlines() if line.strip()):
                    if len(summary) > 150:
                        break
                    summary += line + "\n"
            skills[skill_name] = {"path": path, "summary": summary}
        return skills


# ============================================================================
#  REACT INTERCEPTOR
# ============================================================================

ep_react_interceptor = ExtensionPoint(
    description="React Interceptor. A factory that creates interceptors implementing the ReactInterceptor protocol."
)


@runtime_checkable
class ReactInterceptor(Protocol):
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
        context: ReactContext,
    ) -> None: ...


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
        self._instances: list[ReactInterceptor] = [None] * len(self._configs)

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
        context: ReactContext,
    ) -> None:
        for i, cfg in enumerate(self._configs):
            if self._instances[i] is None and ep_react_interceptor.has(cfg["name"]):
                try:
                    self._instances[i] = _create_extension_instance(ep_react_interceptor, ReactInterceptor, cfg)
                except Exception as e:
                    self._instances[i] = e
                    logger.error(f"Failed to create interceptor {cfg['name']}: {e}")
            if isinstance(self._instances[i], ReactInterceptor):
                await self._instances[i].intercept(stage, context)


# ============================================================================
#  AGENT
# ============================================================================

ep_agent = ExtensionPoint(description="Agent. A factory that creates agents implementing the Agent protocol.")


class Agent(Protocol):
    async def ask(
        self,
        conversation_id: str,
        message: str | list[dict[str, Any]],
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        llm_metadata: dict[str, Any] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
    ) -> str: ...


@dataclass
class ReactContext:
    conversation_id: str
    turn_id: str
    system: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    current: list[Message] = field(default_factory=list)
    tool_defs: list[dict[str, Any]] = field(default_factory=list)
    current_llm_response: LLMResponse | None = None
    final_content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def set_system_prompt(self, content: str | list[dict[str, Any]]) -> None:
        self.system = [{"role": "system", "content": content}]

    def add_message(self, llm_message: dict[str, Any], *, merge: bool = False, **kwargs) -> None:
        # When merging, only combine if there is a previous message with the same role.
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


class ReactAgent:
    def __init__(
        self,
        system_prompt: str | dict[str, str] | None = None,
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
        # Services — required pre-built instances, owned by harness
        llm: LLMClient | None = None,
        message_store: MessageStore | None = None,
        memory_store: MemoryStore | None = None,
        consolidator: Consolidator | None = None,
        skills_loader: SkillsLoader | None = None,
        interceptor: ReactInterceptor | None = None,
        local_tools: ToolRegistry | None = None,
    ):
        self._system_prompt = {"_default": system_prompt} if isinstance(system_prompt, str) else (system_prompt or {})
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

        # Borrowed services — agent does NOT own their lifecycle
        self._llm = llm or LLMClient()
        self._message_store = message_store or InMemMessageStore()
        self._memory_store = memory_store or InMemMemoryStore()
        self._consolidator = consolidator or NaiveConsolidator()
        self._skills_loader = skills_loader or FileSystemSkillsLoader()
        self._interceptor = interceptor or ChainReactInterceptor()

        # Tool registry — exists on agent, but populated by harness
        self._local_tools = local_tools or ToolRegistry("Harness-scoped tools for this agent.")

        # Skills loaded by tool - will be apart of system prompt
        self._loaded_skills: dict[str, str] = {}

        self._register_memory_tools()
        self._register_skills_tools()
        self._register_agent_tools()

    async def ask(
        self,
        conversation_id: str,
        content: str | list[dict[str, Any]],
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]] | None = None,
        llm_metadata: dict[str, Any] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
    ) -> str:
        ctx = ReactContext(
            conversation_id=conversation_id,
            turn_id=uuid.uuid4().hex,
            history=await self._get_conversation_history(conversation_id),
            tool_defs=self._get_tool_defs(),
            metadata=(ctx_metadata or {}).copy(),
        )
        ctx.set_system_prompt(await self._build_system_prompt())
        if not ctx.history:
            ctx.add_message({"role": "user", "content": f"--- Current Conversation ID is {conversation_id} ---\n\n"})
        ctx.add_message({"role": "user", "content": content or ""}, merge=True)

        llm_params = {
            "model": self._model,
            "reasoning_effort": self._reasoning_effort,
        } | (llm_metadata or {})

        cache_index = 0

        def _add_message(message: dict[str, Any]) -> None:
            nonlocal cache_index
            ctx.add_message(_compact(message))
            cache_index -= 1

        async def _run_interceptor(stage: str):
            """Run interceptor, letting AbortTurn propagate; only catch other errors."""
            try:
                await self._interceptor.intercept(stage, ctx)
            except AbortTurn:
                raise
            except Exception as e:
                logger.error(
                    "Error in interceptor: [conversation_id: %s, turn_id: %s, stage: %s] %s",
                    ctx.conversation_id,
                    ctx.turn_id,
                    stage,
                    e,
                    exc_info=True,
                )

        async def _interrupt():
            if interrupt and (llm_message := await _apply_async(interrupt, {})):
                ctx.add_message(llm_message, merge=True)

        try:
            await _run_interceptor("prepare")
            for _ in range(self._max_iterations):
                await _interrupt()
                ctx.set_system_prompt(await self._build_system_prompt())
                await _run_interceptor("before_llm")

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
                if not response.tool_calls:
                    final_content = _strip_think(response.content)
                    if response.finish_reason == "error":
                        logger.error("Error in LLM response: %s", response.finish_reason)
                        final_content = final_content or "(LLM responds error)"
                    _add_message(
                        {
                            "role": "assistant",
                            "content": final_content or "",
                            "reasoning_content": ctx.current_llm_response.reasoning_content,
                            "thinking_blocks": ctx.current_llm_response.thinking_blocks,
                        }
                    )
                    ctx.final_content = final_content
                    await _run_interceptor("final_content")
                    break

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
                    try:
                        if not _allowed(tc.name, self._tools, self._exclude_tools):
                            raise Exception(f"Tool {tc.name} is not allowed")

                        result = (
                            await ep_tool.invoke_async(tc.name, tc.arguments)
                            if ep_tool.has(tc.name)
                            else await self._local_tools.invoke_async(tc.name, tc.arguments)
                        )
                    except Exception as e:
                        logger.error("Error in tool call [%s]: %s", tc.name, e)
                        result = str(e)
                    _add_message(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": str(result),
                        }
                    )
                    await _run_interceptor("after_tool")
            else:
                ctx.add_message({"role": "assistant", "content": "(max iterations reached)"})
                await _run_interceptor("max_iteration")
        except AbortTurn:
            pass
        except Exception as e:
            logger.error("Error in agent: %s", e, exc_info=True)
            ctx.add_message({"role": "assistant", "content": f"(error: {e})"})

        await self._message_store.save_messages(conversation_id, ctx.current)
        return ctx.final_response

    def _get_tool_defs(self) -> list[dict[str, Any]]:
        tool_defs = ep_tool.to_openai_schema() | self._local_tools.to_openai_schema()
        return list(_pick_collection(tool_defs, self._tools, self._exclude_tools).values())

    async def _get_conversation_history(self, conversation_id: str) -> list[dict]:
        def _format_content(msg: dict) -> str:
            content = msg.get("content", "")
            if msg.get("role") == "tool" and isinstance(content, str) and len(content) > 150:
                return content[:147] + "..."
            return content

        def _format_call_id(call_id: str | None) -> str | None:
            return call_id[:64] if call_id is not None else None

        async def _get_messages() -> list[dict]:
            messages = await self._message_store.get_messages(conversation_id)
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

        if sum(len(m.get("content", "")) for m in history) > self._max_tokens:
            summary = await self._consolidator.consolidate(history)
            await self._message_store.save_summary(conversation_id, summary)
            history = await _get_messages()

        return history

    async def _build_system_prompt(self) -> str:
        prompt = "--- SYSTEM PROMPT ---\n\n"
        prompt += self._system_prompt.get("_default", "")

        for key, value in self._system_prompt.items():
            if key != "_default":
                prompt += f"\n\n### {key.upper()} ###\n{value}"

        if memories := _pick_collection(
            await self._memory_store.list_memories(),
            self._memories,
            self._exclude_memories,
        ):
            prompt = "\n\n--- ACTIVE MEMORY ---\n"
            for key, content in memories.items():
                prompt += f"\n### {key.upper()} ###\n{content}"

        if available_skills := _pick_collection(
            await self._skills_loader.list_skills(), self._skills, self._exclude_skills
        ):
            prompt += "\n\n--- AVAILABLE SKILLS ---\n"
            for name, info in available_skills.items():
                prompt += f"\n### {name.upper()} ###\n\n"
                prompt += f"- Location: {info.get('location')}\n"
                prompt += f"- Summary: {info.get('summary')}"

        if self._loaded_skills:
            prompt += "\n\n--- ACTIVE SKILLS ---\n"
            for name, (location, skill) in self._loaded_skills.items():
                prompt += f"\n### {name.upper()} ###\n\nLocation: {location}\n\n{skill}"

        prompt += (
            "\n\n--- SYSTEM INFORMATION ---\n"
            f"\n- Platform: {platform.system()}"
            f"\n- Date: {datetime.now().strftime('%A, %B %d, %Y')}\n"
            "\n\n"
        )

        return prompt

    def _register_memory_tools(self) -> None:
        @self._local_tools(
            name="UpdateMemory",
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
            """Overwrite a specific type of long-term agent memory."""
            if not _allowed(key.lower(), self._memories, self._exclude_memories):
                raise ValueError(f"Update memory '{key}' is not allowed.")
            await self._memory_store.save_memory(key, content)
            return f"(Successfully updated memory '{key}'.)"

    def _register_skills_tools(self) -> None:
        @self._local_tools(
            name="LoadSkill",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill name"}},
                "required": ["name"],
            },
        )
        async def tool_load_skill(name: str) -> str:
            """Load a specific skill into the agent's system prompt."""
            if not _allowed(name, self._skills, self._exclude_skills):
                raise ValueError(f"Skill '{name}' is not allowed.")
            if skill := await self._skills_loader.load_skill(name):
                self._loaded_skills[name] = skill
                return f"(Successfully loaded skill '{name}' to system prompt.)"
            return f"(Failed to load skill '{name}'.)"

        @self._local_tools(
            name="UnloadSkill",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill name."}},
                "required": ["name"],
            },
        )
        async def tool_unload_skill(name: str) -> str:
            """Unload a skill from the agent's system prompt."""
            self._loaded_skills.pop(name, None)
            return f"(Successfully unloaded skill '{name}' from system prompt.)"

        @self._local_tools(
            name="SearchSkills",
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
            """Search for skills by query."""
            results = await self._skills_loader.search_skills(query)
            results = _compact(_pick_collection(results, self._skills, self._exclude_skills))
            return json.dumps(results)

    def _register_agent_tools(self) -> None:
        @self._local_tools(
            name="ListAgents",
            description="List all available agents registered in the system.",
            parameters={"type": "object", "properties": {}},
        )
        async def tool_list_agents() -> str:
            """Search for agents by query."""
            results = ep_agent.describe()
            results.pop("_default", None)
            results = _pick_collection(results, self._subagents, self._exclude_subagents)
            return json.dumps(results)

    @classmethod
    def register(cls, name: str, discriptions: str | None = None, **kwargs):
        @ep_agent(name=name, discriptions=discriptions, defaults=kwargs)
        @wraps(ReactAgent)
        def create_react_agent(*args, **kwargs):
            return ReactAgent(*args, **kwargs)


# ============================================================================
#  MAILBOX
# ============================================================================

ep_mailbox = ExtensionPoint(
    description="Mailbox. Used for message passing between agents. It should implement the MailBox protocol."
)


@runtime_checkable
class Mailbox(Protocol):
    """Address-bound message endpoint.

    Each instance is bound to a single address at construction time.
    Instances must not be shared across actors.
    """

    async def receive(self, address: str) -> Envelope:
        """Block until a message arrives for this address."""
        ...

    async def send(self, env: Envelope) -> None:
        """Deliver an envelope to ``env.recipient``."""
        ...

    async def receive_nowait(self, address: str) -> Envelope | None:
        """Non-blocking receive. Returns ``None`` when inbox is empty."""
        ...


@ep_mailbox(name="_default")
class InMemMailbox:
    _queues: dict[str, asyncio.Queue[Envelope]] = {}  # agent_name -> queue

    @classmethod
    def _get_queue(cls, address: str) -> asyncio.Queue[Envelope]:
        if address not in cls._queues:
            cls._queues[address] = asyncio.Queue()
        return cls._queues[address]

    async def receive(self, address: str) -> Envelope:
        return await self._get_queue(address).get()

    async def send(self, env: Envelope) -> None:
        await self._get_queue(env.recipient).put(env)

    async def receive_nowait(self, address: str) -> Envelope | None:
        try:
            return self._get_queue(address).get_nowait()
        except asyncio.QueueEmpty:
            return None


# ═══════════════════════════════════════════════════════════════
#  CHANNEL
# ═══════════════════════════════════════════════════════════════


ep_channel = ExtensionPoint(description="Channel. Bridges external clients to/from a mailbox address.")


@runtime_checkable
class Channel(Protocol):
    """Bridges an external interface (TUI, bot, web) to/from the mailbox.

    A channel is an async service that:
    - Reads envelopes from ``mailbox.receive(address)`` and presents them externally.
    - Translates external input into ``Envelope`` objects and calls ``mailbox.send(env)``.
    """

    async def run(self, mailbox: Mailbox, address: str) -> None:
        """Bridge loop — runs until cancelled.

        Args:
            mailbox: The shared harness mailbox.
            address: This channel's own mailbox address (e.g. ``"http"``).
        """
        ...


# ═══════════════════════════════════════════════════════════════
#  AGENT ACTOR
# ═══════════════════════════════════════════════════════════════

ep_actor_command = ExtensionPoint(
    description="""Actor command handler. An async function with injectable arguments: input, env, actor, harness.
    For example:

    @ep_actor_command(name="echo")
    async def echo(input: str) -> str:
        return input

    @ep_actor_command(name="tools")
    async def tools(actor: AgentActor) -> dict:
        return actor._agent._get_tool_defs()
    """
)

# ═══════════════════════════════════════════════════════════════
#  INTERNALS
# ═══════════════════════════════════════════════════════════════


def _create_extension_instance(ext_point: ExtensionPoint, ext_protocol: type, config: Any) -> Any:
    if isinstance(config, ext_protocol):
        return config
    if config is None and not ext_point.has("_default"):
        return None
    cfg = (config or {}).copy()
    return ext_point.invoke(cfg.pop("name", "_default"), cfg)


def _compact(*dicts: dict, **kwargs: Any) -> dict[str, Any]:
    """Drop None-valued entries from a dict."""
    merged = {}
    [merged.update(d) for d in (*dicts, kwargs) if d is not None]
    return {k: v for k, v in merged.items() if v is not None}


def _build_params(fn: Callable, params: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    sig = inspect.signature(fn)
    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    valid_params = params if has_varkw else {k: v for k, v in params.items() if k in sig.parameters}
    bound = sig.bind_partial(**valid_params)
    bound.apply_defaults()
    return bound.args, bound.kwargs


def _apply(fn: Callable, params: dict[str, Any]) -> Any:
    args, kwargs = _build_params(fn, params)
    return fn(*args, **kwargs)


async def _apply_async(fn: Callable, params: dict[str, Any]) -> Any:
    args, kwargs = _build_params(fn, params)
    result = fn(*args, **kwargs)
    return await result if asyncio.iscoroutine(result) else result


def _strip_think(text: str | None) -> str | None:
    """Remove <think>…</think> blocks that some models embed in content."""
    if not text:
        return None
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None


def _safe_format(template: str, **kwargs: Any) -> str:
    class SafeMapping(dict):
        def __missing__(self, key: str) -> str:
            return f"{{{key}}}"

    return template.format_map(SafeMapping(kwargs))


def _load_json(source: Path | str, from_string: bool = False) -> dict[str, Any]:
    try:
        return json.loads(source if from_string else Path(source).read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load JSON from %s", source, exc_info=True)
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to read text from %s", path, exc_info=True)
        return ""


def _pick_collection(
    collection: dict[str, Any],
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    if include is not None:
        collection = {k: v for k, v in collection.items() if k in include}
    if exclude is not None:
        collection = {k: v for k, v in collection.items() if k not in exclude}
    return collection


def _allowed(name: str, include: list[str] | None = None, exclude: list[str] | None = None) -> bool:
    return (include is None or name in include) and (exclude is None or name not in exclude)


def _as_parts(content: str | list[dict[str, Any]], cache: bool = False) -> list[dict[str, Any]]:
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return parts if not cache else parts[:-1] + [parts[-1] | {"cache_control": {"type": "ephemeral"}}]


@contextmanager
def _flock(path: Path | str):
    """Acquire an exclusive filesystem lock on a sidecar ``.lock`` file."""
    from filelock import FileLock

    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _litellm_response_to_llm_response(raw: Any) -> LLMResponse:
    """Convert a LiteLLM chat-completion response to LLMResponse."""
    if isinstance(raw, LLMResponse):
        return raw

    choice = raw.choices[0]
    message = choice.message
    usage_obj = getattr(raw, "usage", None)
    usage = {
        "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
    }
    return LLMResponse(
        content=message.content and str(message.content),
        tool_calls=_litellm_tool_calls_to_requests(getattr(message, "tool_calls", None)),
        finish_reason=choice.finish_reason or "stop",
        usage=usage,
        reasoning_content=getattr(message, "reasoning_content", None),
        thinking_blocks=getattr(message, "thinking_blocks", None),
    )


def _litellm_tool_calls_to_requests(raw_tool_calls: Any) -> list[ToolCallRequest]:
    """Convert LiteLLM/OpenAI tool_calls to ToolCallRequest records."""
    if not raw_tool_calls:
        return []
    result: list[ToolCallRequest] = []
    for idx, tc in enumerate(raw_tool_calls):
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None)
        raw_arguments = getattr(fn, "arguments", None)
        arguments = (
            _load_json(raw_arguments, from_string=True)
            if isinstance(raw_arguments, str)
            else raw_arguments
            if isinstance(raw_arguments, dict)
            else {}
        )
        tc_id = getattr(tc, "id", None) or f"call_{idx}"
        metadata: dict[str, Any] = {
            "provider": "litellm",
            "index": idx,
            "tool_type": getattr(tc, "type", None),
            "function_name": name,
            "raw_arguments": raw_arguments,
        }
        result.append(
            ToolCallRequest(
                id=str(tc_id),
                name=str(name or ""),
                arguments=arguments,
                metadata=metadata,
            )
        )
    return result


def _load_ext_modules(modules: list[str]) -> None:
    """Load extension modules from a list of module names."""
    for modname in modules:
        try:
            importlib.import_module(modname)
        except Exception:
            logger.error("Failed to import extension module %s", modname, exc_info=True)


def _load_ext_paths(paths: list[str | Path]) -> None:
    """Load extensions from a list of paths. If the path is a file, load it.
    If the path is a directory, load all .py files in it. No recursive loading.
    """

    def _load_extension_module_file(path: Path) -> bool:
        """Import a single Python file as an extension module."""
        module_name = "agentloop_ext_" + str(abs(hash(str(path))))
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec and spec.loader:
            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                logger.error("Failed to load extension file %s", path, exc_info=True)
        else:
            logger.error("Could not create import spec for extension file %s", path)

    files = {
        f.expanduser().resolve()
        for p in map(Path, paths)
        for f in ([p] if p.is_file() else (x for x in p.rglob("*.py") if not x.name.startswith("_")))
    }
    for f in files:
        _load_extension_module_file(f)


async def _aclose(instance: Any) -> None:
    if isinstance(instance, Closeable):
        try:
            await instance.aclose()
        except Exception:
            logger.warning("aclose error in %s", type(instance).__name__, exc_info=True)
