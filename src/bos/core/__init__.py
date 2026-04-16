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
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bos.core.actor import AgentActor as _AgentActor
from bos.core.agent import AbortTurn as AbortTurn
from bos.core.agent import ReactAgent as ReactAgent
from bos.core.agent import ReactContext as ReactContext
from bos.core.contract import Agent as Agent
from bos.core.contract import Channel as Channel
from bos.core.contract import Closeable as Closeable
from bos.core.contract import Consolidator as Consolidator
from bos.core.contract import Mailbox as Mailbox
from bos.core.contract import MemoryStore as MemoryStore
from bos.core.contract import Message as Message
from bos.core.contract import MessageStore as MessageStore
from bos.core.contract import ReactInterceptor as ReactInterceptor
from bos.core.contract import SkillsLoader as SkillsLoader
from bos.core.contract import ep_actor_command as ep_actor_command
from bos.core.contract import ep_agent as ep_agent
from bos.core.contract import ep_channel as ep_channel
from bos.core.contract import ep_consolidator as ep_consolidator
from bos.core.contract import ep_mailbox as ep_mailbox
from bos.core.contract import ep_memory_store as ep_memory_store
from bos.core.contract import ep_message_store as ep_message_store
from bos.core.contract import ep_provider as ep_provider
from bos.core.contract import ep_react_interceptor as ep_react_interceptor
from bos.core.contract import ep_skills_loader as ep_skills_loader
from bos.core.contract import ep_tool as ep_tool
from bos.core.defaults import FileSystemSkillsLoader as FileSystemSkillsLoader
from bos.core.defaults import InMemMailbox as InMemMailbox
from bos.core.defaults import InMemMemoryStore as InMemMemoryStore
from bos.core.defaults import InMemMessageStore as InMemMessageStore
from bos.core.defaults import NaiveConsolidator as NaiveConsolidator
from bos.core.defaults import litellm_complete as litellm_complete
from bos.core.harness import (
    CURRENT_HARNESS as _CURRENT_HARNESS,
)
from bos.core.harness import (
    AgentHarness as _AgentHarness,
)
from bos.core.harness import (
    bootstrap_platform as _bootstrap_platform,
)
from bos.core.interceptors import ChainReactInterceptor as ChainReactInterceptor
from bos.core.llm import LLMClient as LLMClient
from bos.core.llm import LLMResponse, ToolCallRequest
from bos.core.registry import Extension as Extension
from bos.core.registry import ExtensionPoint as ExtensionPoint
from bos.core.registry import ToolRegistry as ToolRegistry
from bos.protocol import Envelope as Envelope

AgentActor = _AgentActor
AgentHarness = _AgentHarness
CURRENT_HARNESS = _CURRENT_HARNESS
bootstrap_platform = _bootstrap_platform

__version__ = "0.1.0"


logger = logging.getLogger("bos")

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
