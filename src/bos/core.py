"""
Lightweight single-file agent framework.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import importlib.util
import inspect
import json
import logging
import os
import platform
import re
import shutil
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Literal, Protocol, runtime_checkable

try:
    import tomllib
except ImportError:
    import tomli as tomllib


__version__ = "0.1.0"


logger = logging.getLogger("bos")


# ═══════════════════════════════════════════════════════════════
#  Extension Points
# ═══════════════════════════════════════════════════════════════


@dataclass
class Extension:
    name: str
    fn: Callable[..., Any]
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExtensionPoint:
    def __init__(
        self,
        description: str,
        validate: Callable[..., bool] | None = None,
    ) -> None:
        self.description = description
        self._validate = validate or getattr(self, "default_validate", None)
        self._extensions: dict[str, Extension] = {}
        self.get = self._extensions.get
        self.has = lambda name: name in self._extensions
        self.describe = lambda: {k: v.description for k, v in self._extensions.items()}

    def register(self, ext: Extension) -> None:
        if ext.name in self._extensions:
            logger.warning(
                f"Set default provider for extension point: {self.description}"
                if ext.name == "_default"
                else f"Extension `{ext.name}` got overwritten for extension point: {self.description}"
            )
        self._extensions[ext.name] = ext

    def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> Any:
        if name not in self._extensions:
            raise ValueError(f"Extension '{name}' not found for '{self.description[:30].strip()}...'")
        return _apply(self.get(name).fn, _compact(self.get(name).defaults, kwargs or {}))

    async def invoke_async(self, name: str, kwargs: dict[str, Any] | None = None) -> Any:
        return await _apply_async(self.get(name).fn, _compact(self.get(name).defaults, kwargs or {}))

    def __call__(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        defaults: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Any]:
        def decorator(fn: Any) -> Any:
            ext_name = name or getattr(fn, "__name__", None)
            if ext_name is None:
                raise ValueError("Extension name is required")
            ext = Extension(
                name=ext_name,
                description=description or getattr(fn, "__doc__", ""),
                defaults=defaults,
                metadata=metadata,
                fn=fn,
            )
            if self._validate and not _apply(self._validate, {"fn": fn, "ext": ext, "ext_point": self}):
                raise ValueError(f"Extension is not valid:\n{description}")
            self.register(ext)
            return fn

        return decorator


# ═══════════════════════════════════════════════════════════════
#  CLOSEABLE PROTOCOL
# ═══════════════════════════════════════════════════════════════


@runtime_checkable
class Closeable(Protocol):
    """Opt-in cleanup contract for extensions that hold resources."""

    async def aclose(self) -> None: ...


# ═══════════════════════════════════════════════════════════════
#  LLM CLIENT
# ═══════════════════════════════════════════════════════════════


ep_provider = ExtensionPoint(
    description="""
        LLM provider. An async function that takes messages: list[dict]
        and returns response:LLMResponse.
    """
)


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None

    @property
    def text(self) -> str:
        """Preferred user-visible text payload."""
        return self.content or self.reasoning_content or ""


@dataclass
class ToolCallRequest:
    """Tool-call request projected into a provider-agnostic shape."""

    id: str
    name: str
    arguments: dict[str, Any]
    metadata: dict[str, Any] | None = None

    def to_openai_call(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                # OpenAI-compatible tool call replay expects JSON text here.
                "arguments": json.dumps(self.arguments),
            },
        }


@ep_provider(name="_default")
async def litellm_complete(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    # avoid litellm call load_dotenv() which will load .env in current working directory

    os.environ["LITELLM_MODE"] = "extension"

    import litellm

    raw = await litellm.acompletion(model=model, messages=messages, **kwargs)
    return _litellm_response_to_llm_response(raw)


class LLMClient:
    """Extensible LLM client with provider routing and scoped config."""

    def __init__(self, providers_cfg: dict[str, dict[str, Any]] | None = None) -> None:
        self._providers_cfg: dict[str, dict[str, Any]] = (
            {k: _compact(v) for k, v in providers_cfg.items() if v is not None} if providers_cfg is not None else {}
        )

    async def complete(
        self,
        messages: list[dict],
        **kwargs: Any,
    ) -> LLMResponse:
        """Call the LLM, routing to the correct provider.

        Merges 4 tiers of config, resolves the provider by model
        prefix, and injects only the parameters the provider's
        ``complete`` method accepts.
        """
        if model := kwargs.get("model"):
            provider_name, model_name = model.split("/", 1)
            if not ep_provider.has(provider_name):
                provider_name, model_name = "_default", model
        else:
            provider_name, model_name = "_default", None
        params = self._providers_cfg.get(provider_name, {}) | kwargs | {"messages": messages, "model": model_name}
        return await ep_provider.invoke_async(provider_name, params)


# ═══════════════════════════════════════════════════════════════
#  TOOLS
# ═══════════════════════════════════════════════════════════════


class ToolRegistry(ExtensionPoint):
    def to_openai_schema(self) -> dict[str, dict[str, Any]]:
        return {t.name: self.build_openai_schema(t) for t in self._extensions.values()}

    @staticmethod
    def build_openai_schema(ext: Extension) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": ext.name,
                "description": ext.description,
                "parameters": ext.metadata["parameters"],
            },
        }

    def default_validate(self, ext: Extension) -> bool:
        # check the parameters is provided
        if "parameters" not in ext.metadata:
            raise ValueError(f"Tool {ext.name} is missing parameters")
        # check all the parameters of the fn are in the metadata
        fn_params = set(inspect.signature(ext.fn).parameters.keys())
        meta_params = set(ext.metadata["parameters"]["properties"].keys())
        if fn_params != meta_params:
            raise ValueError(f"Tool {ext.name} parameters do not match the function signature")
        if not ext.description:
            logger.warning(f"Tool {ext.name} is missing description")
        return True


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
            ctx.add_message({"role": "user", "content": f"Current Conversation ID is {conversation_id}"})
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
            name="update_memory",
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
            name="load_skill",
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
            name="unload_skill",
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
            name="search_skills",
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
            name="list_agents",
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


@dataclass
class Envelope:
    sender: str  # address like: agent://name
    recipient: str  # address like: channel+telegram://chat_id
    content: str
    context_type: str = "message"
    conversation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)


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
#  AGENT ACTOR
# ═══════════════════════════════════════════════════════════════


class AgentActor:
    """Actor that drives an Agent via a Mailbox.

    Supports **per-sender parallelism**: each distinct sender gets at most one
    concurrent ask task.  Messages that arrive while a sender's task is running
    are buffered and merged into the next ask for that sender once the current
    one finishes.  Interrupts (inject/stop) are routed to the matching sender's
    task.
    """

    def __init__(self, address: str, agent: Agent, mailbox: Mailbox):
        self._address = address
        self._agent = agent
        self._mailbox = mailbox

        # per-sender state
        self._tasks: dict[str, asyncio.Task] = {}  # sender -> running task
        self._conversations: dict[str, str] = {}  # sender -> conversation_id
        self._pending: dict[str, list[Envelope]] = {}  # sender -> queued messages
        self._interrupts: dict[str, list[Envelope]] = {}  # sender -> interrupt buffer

    async def aclose(self) -> None:
        """Cancel all running tasks. Does NOT close the agent (harness owns that)."""
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def run(self) -> None:
        """Main actor loop.

        Single receive loop.  Each incoming envelope is dispatched by sender:

        - **no task running for sender** → start an ask task (which includes
          the reply step) from all pending + current messages for that sender.
        - **task running, interrupt type** → buffer for the sender's pull-mode
          ``_interrupt`` callback.
        - **task running, regular message** → buffer for the sender's next ask.
        """
        try:
            while True:
                # --- Reap finished tasks across all senders ---
                for s in list(self._tasks.keys()):
                    if self._tasks[s].done():
                        if exc := self._tasks[s].exception():
                            import traceback

                            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                            logger.error("Ask task failed for sender=%s:\n%s", s, tb)
                            error_content = f"(error: {exc})\n\n```\n{tb}```"
                            await self._mailbox.send(Envelope(sender=self._address, recipient=s, content=error_content))
                        del self._tasks[s]

                        if s in self._pending and any(e.context_type == "message" for e in self._pending[s]):
                            self._fire_pending(s)

                env = await self._mailbox.receive_nowait(self._address)
                if env is None:
                    await asyncio.sleep(0.1)
                    continue

                sender = env.sender

                # --- No task running for sender: start one ---
                if sender not in self._tasks:
                    if env.context_type == "message":
                        self._pending.setdefault(sender, []).append(env)
                        self._fire_pending(sender)
                    continue

                # --- Task in-flight for sender: buffer by type ---
                if env.context_type in ("inject", "stop"):
                    self._interrupts.setdefault(sender, []).append(env)
                else:
                    self._pending.setdefault(sender, []).append(env)
        except asyncio.CancelledError:
            await self.aclose()
            raise

    # ── ask lifecycle ──────────────────────────────────────────

    def _fire_pending(self, sender: str) -> None:
        """Start an ask task for *sender* from all its pending messages."""
        messages = [e for e in self._pending.pop(sender, []) if e.context_type == "message"]
        if not messages:
            return
        content = "\n\n".join(
            f"[from {e.sender} {e.timestamp.isoformat()}]: {e.content}" if len(messages) > 1 else e.content
            for e in messages
        )
        conversation_id = messages[-1].conversation_id or self._conversations.setdefault(sender, uuid.uuid4().hex)
        self._conversations[sender] = conversation_id
        self._interrupts[sender] = []
        self._tasks[sender] = asyncio.create_task(self._run_ask(sender, conversation_id, content))

    async def _run_ask(self, sender: str, conversation_id: str, content: str) -> None:
        """Run agent.ask **and** send the reply — all inside the task."""
        while True:
            response = await self._agent.ask(conversation_id, content, interrupt=self._make_interrupt(sender))
            await self._mailbox.send(Envelope(sender=self._address, recipient=sender, content=response))

            messages = [e for e in self._pending.pop(sender, []) if e.context_type == "message"]
            if not messages:
                break

            conversation_id = messages[-1].conversation_id or conversation_id
            self._conversations[sender] = conversation_id

            content = "\n\n".join(
                f"[from {e.sender} {e.timestamp.isoformat()}]: {e.content}" if len(messages) > 1 else e.content
                for e in messages
            )

    # ── interrupt (pull-mode, per-sender) ──────────────────────

    def _make_interrupt(self, sender: str):
        """Return an interrupt callback bound to *sender*'s buffer."""

        def _interrupt() -> dict[str, Any] | None:
            buf = self._interrupts.get(sender, [])
            parts: list[str] = []
            remaining: list[Envelope] = []
            for env in buf:
                if env.context_type == "stop":
                    self._interrupts[sender] = remaining
                    raise AbortTurn()
                if env.context_type == "inject":
                    parts.append(f"[from {env.sender}]: {env.content}")
                else:
                    remaining.append(env)
            self._interrupts[sender] = remaining
            if parts:
                return {"role": "user", "content": "\n\n".join(parts)}
            return None

        return _interrupt


# ═══════════════════════════════════════════════════════════════
#  AGENT HARNESS
# ═══════════════════════════════════════════════════════════════


def bootstrap_platform(
    bos_dir: str | Path = ".bos",
    envs: dict[str, str] | None = None,
    envfile: str | None = None,
    extensions: list[str] | None = None,
    agents: list[dict[str, Any]] | None = None,
    agent_defaults: dict[str, Any] | None = None,
) -> None:
    """Bootstrap the global environment: load env vars and register extension modules."""

    bos_root = Path(bos_dir).expanduser().resolve()
    bos_root.mkdir(parents=True, exist_ok=True)

    if envs:
        os.environ.update(envs)
    if envfile:
        from dotenv import load_dotenv

        load_dotenv((bos_root / Path(envfile).expanduser()).resolve())

    if extensions:
        modules, paths = [], []
        for ext in extensions:
            p = bos_root / Path(ext).expanduser()
            if p.exists():
                paths.append(p)
            else:
                modules.append(ext)
        if modules:
            _load_ext_modules(modules=modules)
        if paths:
            _load_ext_paths(paths=paths)

    if agents:
        defaults = agent_defaults or {}
        for agent_spec in agents:
            ReactAgent.register(**(defaults | agent_spec))


CURRENT_HARNESS: contextvars.ContextVar[AgentHarness] = contextvars.ContextVar("current_harness")


class AgentHarness:
    """Lifecycle-owning container for shared agent services.

    Creates, configures, and tears down all services.
    Provides ``create_agent()`` to stamp out fully instrumented agents
    that share the harness-owned services.
    """

    def __init__(
        self,
        *,
        # Service configs — always dicts, resolved to instances on enter
        mailbox: dict[str, Any] | None = None,
        message_store: dict[str, Any] | None = None,
        memory_store: dict[str, Any] | None = None,
        consolidator: dict[str, Any] | None = None,
        skills_loader: dict[str, Any] | None = None,
        providers: dict[str, dict[str, Any]] | None = None,
        interceptors: list[str | dict[str, Any]] | None = None,
        # Agent configs
        bos_dir: str | Path = ".bos",
        workspace: str | Path = ".",
        subagents: list[dict[str, Any]] | None = None,
    ) -> None:
        self._bos_root = Path(bos_dir).expanduser().resolve()
        self._workspace = Path(workspace).expanduser().resolve()
        self._subagents_cfg = {cfg.get("name", "_default"): cfg for cfg in subagents} if subagents else {}

        self._mailbox_cfg = mailbox
        self._message_store_cfg = message_store
        self._memory_store_cfg = memory_store
        self._consolidator_cfg = consolidator
        self._skills_loader_cfg = skills_loader
        self._providers_cfg = providers
        self._interceptors_cfg = interceptors

        # Populated on __aenter__
        self._owned: list[Any] = []
        self._token: contextvars.Token | None = None
        self._original_cwd: Path | None = None
        self.mailbox: Mailbox | None = None
        self.message_store: MessageStore | None = None
        self.memory_store: MemoryStore | None = None
        self.consolidator: Consolidator | None = None
        self.skills_loader: SkillsLoader | None = None
        self.interceptor: ReactInterceptor | None = None
        self.llm: LLMClient | None = None

    async def __aenter__(self) -> AgentHarness:
        if self._token is not None:
            raise RuntimeError(
                "AgentHarness is already active. Use CURRENT_HARNESS.get() to access "
                "the current harness instead of re-entering."
            )

        self._original_cwd = Path.cwd()

        # Change to bos root directory to initialze the components
        os.chdir(self._bos_root)

        # Create services — harness owns all of them
        self.mailbox = self._create_and_own(ep_mailbox, Mailbox, self._mailbox_cfg)
        self.message_store = self._create_and_own(ep_message_store, MessageStore, self._message_store_cfg)
        self.memory_store = self._create_and_own(ep_memory_store, MemoryStore, self._memory_store_cfg)
        self.consolidator = self._create_and_own(ep_consolidator, Consolidator, self._consolidator_cfg)
        self.skills_loader = self._create_and_own(ep_skills_loader, SkillsLoader, self._skills_loader_cfg)
        self.interceptor = ChainReactInterceptor(self._interceptors_cfg)
        self.llm = LLMClient(self._providers_cfg)

        # Change to workspace as the agent working directory
        os.chdir(self._workspace)

        # Set self into contextvar for infrastructure access
        self._token = CURRENT_HARNESS.set(self)
        return self

    async def __aexit__(self, *exc) -> None:
        """Orderly shutdown in reverse creation order."""
        await _aclose(self.interceptor)
        for resource in reversed(self._owned):
            await _aclose(resource)
        self._owned.clear()

        if self._token is not None:
            # We are guaranteed to be in the same context where __aenter__ was called
            CURRENT_HARNESS.reset(self._token)
            self._token = None

        if self._original_cwd is not None:
            os.chdir(self._original_cwd)
            self._original_cwd = None

    def create_agent(
        self,
        agent_name: str | None = None,
        agent_cfg: dict[str, Any] = None,
    ) -> ReactAgent:
        """Create a fully instrumented agent that shares harness-owned services.

        Accepts the same kwargs as ``ReactAgent.__init__`` (system_prompt,
        model, tools, skills, memories, …).  The returned agent has all
        harness-scoped tools (memory, skills, mailbox, orchestration)
        pre-registered.
        """
        if CURRENT_HARNESS.get(None) is None:
            raise RuntimeError("create_agent must be called within an active AgentHarness context.")

        if not any([agent_name, agent_cfg]):
            agent_cfg = {
                "system_prompt": "You are a helpful assistant.",
                "model": os.getenv("BOS_MODEL"),
                "tools": [],
                "skills": [],
                "memories": [],
                "subagents": [],
            }

        local_tools = self._create_local_tools()

        kwargs = (agent_cfg or {}) | {
            "llm": self.llm,
            "message_store": self.message_store,
            "memory_store": self.memory_store,
            "consolidator": self.consolidator,
            "skills_loader": self.skills_loader,
            "interceptor": self.interceptor,
            "local_tools": local_tools,
        }

        agent = ep_agent.invoke(agent_name, kwargs) if agent_name else ReactAgent(**kwargs)
        return agent

    def _create_and_own(self, ep: ExtensionPoint, protocol: type, cfg: Any) -> Any:
        """Create a service from config and register it for cleanup."""
        instance = _create_extension_instance(ep, protocol, cfg)
        if instance is not None:
            self._owned.append(instance)
        return instance

    # ── harness-scoped tool registration ──────────────────────
    #
    #  ALL service-bound tools are registered here.
    #  ReactAgent never registers tools itself.

    def _create_local_tools(self) -> ToolRegistry:
        """Register all service-bound tools on the agent."""
        tools = ToolRegistry("Harness-scoped tools for this agent.")
        self._register_harness_tools(tools)
        return tools

    def _register_harness_tools(self, tools: ToolRegistry) -> None:
        harness = self

        @tools(
            name="send_mail",
            description=(
                "Send a message to the recipient's address. e.g.: "
                "send_mail(sender='John', recipient='tui', content='Task 1.3 is done')"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "Sender address"},
                    "recipient": {"type": "string", "description": "Recipient address"},
                    "content": {"type": "string", "description": "Message content"},
                },
                "required": ["sender", "recipient", "content"],
            },
        )
        async def tool_send_mail(sender: str, recipient: str, content: str) -> str:
            """Send a message to the specified address, which might be owned by another agent or an application."""
            await harness.mailbox.send(Envelope(sender=sender, recipient=recipient, content=content))
            return f"(Sent to {recipient})"

        @tools(
            name="ask_subagent",
            parameters={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Name of the agent."},
                    "message": {"type": "string", "description": "Message to send."},
                    "conversation_id": {"type": "string", "description": "Parent conversation id."},
                },
                "required": ["agent_name", "conversation_id", "message"],
            },
        )
        async def ask_subagent(
            agent_name: str,
            message: str,
            conversation_id: str,
        ) -> str:
            """Invoke a sub-agent to perform a one-shot task, sharing the parent's services."""
            if not ep_agent.has(agent_name):
                return f"Error: Agent '{agent_name}' not found."
            subagent_cfg = harness._get_subagent_config(agent_name)
            if task_template := subagent_cfg.get("task_template"):
                message = _safe_format(task_template, task=message, agent_name=agent_name, workspace=harness.workspace)

            agent = harness.create_agent(agent_name, subagent_cfg)
            return await agent.ask(
                f"{conversation_id}:{agent_name}:{uuid.uuid4().hex}",
                message,
                ctx_metadata={
                    "subagent": agent_name,
                    "parent_conversation_id": conversation_id,
                },
            )

    def _get_subagent_config(self, agent_name: str) -> dict[str, Any]:
        default = self._subagents_cfg.get("_default", {})
        config = self._subagents_cfg.get(agent_name, {})
        return default | config


# ═══════════════════════════════════════════════════════════════
#  CONFIGURE
# ═══════════════════════════════════════════════════════════════


def _load_config(workspace: str | Path = ".") -> tuple[Path, dict[str, Any]]:
    workspace = Path(workspace).expanduser().resolve()
    # search .bos folder from workspace to root
    bos_dir = None
    for parent in [workspace] + list(workspace.parents):
        if (parent / ".bos").exists():
            bos_dir = parent / ".bos"
            break
    else:
        bos_dir = Path(os.environ.get("BOS_DIR", "~/.bos")).expanduser()

    cfg_file = bos_dir / "config.toml"
    if not cfg_file.exists():
        bos_dir.mkdir(parents=True, exist_ok=True)
        return bos_dir, {}
    return bos_dir, tomllib.loads(cfg_file.read_text(encoding="utf-8"))


class Workspace:
    def __init__(self, workspace: str | Path = "."):
        self.workspace = Path(workspace).expanduser().resolve()
        self.bos_dir, self.config = _load_config(self.workspace)

    def init(self):
        # create bos_dir if not exists
        self.bos_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = self.bos_dir / "config.toml"
        if cfg_file.exists():
            raise FileExistsError(f"Config file {cfg_file} already exists.")
        # create config.toml if not exists, by copying from config_template.toml
        config_template_path = Path(__file__).parent / "config_template.toml"
        shutil.copy2(config_template_path, cfg_file)
        self.config = tomllib.loads(cfg_file.read_text(encoding="utf-8"))

    def bootstrap_platform(self):
        platform_cfg = self.config.get("platform", {}) | {"bos_dir": self.bos_dir}
        _apply(bootstrap_platform, platform_cfg)

    def harness(self) -> AgentHarness:
        harness_cfg = self.config.get("harness", {}) | {"bos_dir": self.bos_dir, "workspace": self.workspace}
        return _apply(AgentHarness, harness_cfg)

    def enable_interceptors(self, interceptors: list[str | dict[str, Any]]):
        interceptors_cfg = self.config.setdefault("harness", {}).setdefault("interceptors", [])
        interceptors_cfg.extend(i for i in interceptors if i not in interceptors_cfg)

    def get_setting(self, key: str):
        settings, segments = self.config, key.split(".")
        for seg in segments[:-1]:
            settings = settings.get(seg, {})
        return settings.get(segments[-1])


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
