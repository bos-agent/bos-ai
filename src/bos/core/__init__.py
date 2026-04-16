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
import re
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from bos.core.actor import AgentActor as _AgentActor
from bos.core.agent import AbortTurn as AbortTurn
from bos.core.agent import Agent as Agent
from bos.core.agent import ReactAgent as ReactAgent
from bos.core.agent import ReactContext as ReactContext
from bos.core.agent import ep_agent as ep_agent
from bos.core.harness import (
    CURRENT_HARNESS as _CURRENT_HARNESS,
)
from bos.core.harness import (
    AgentHarness as _AgentHarness,
)
from bos.core.harness import (
    bootstrap_platform as _bootstrap_platform,
)
from bos.core.llm import LLMClient as LLMClient
from bos.core.llm import LLMResponse, ToolCallRequest
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
