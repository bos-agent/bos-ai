from __future__ import annotations

import importlib
import importlib.util
import json
import logging
from collections.abc import Callable, Collection
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

# The agent-core helpers are owned by the agent foundation and published from
# its package API (``bos.core.agent``). They are re-exported here so the shared
# ``_``-prefixed helper surface (and ``bos.core``'s public re-exports) keep
# resolving without duplicating code — imported from the package, not the
# foundation's private ``._utils`` leaf, so the assembly ring touches only
# published API (BEP 13 §1.6 rule 4).
from .agent import (
    _apply_async,
    _as_parts,
    _build_params,
    _compact,
    _strip_reply_artifacts,
    _strip_think,
    _xml_attr,
)

if TYPE_CHECKING:
    from .agent import LLMResponse, ToolCallRequest

__all__ = [
    "_apply_async",
    "_as_parts",
    "_build_params",
    "_compact",
    "_strip_reply_artifacts",
    "_strip_think",
    "_xml_attr",
]

logger = logging.getLogger("bos")


def _get_bos_home() -> Path:
    """Return the BOS home directory.

    Uses ``BOS_HOME`` env var if set, otherwise defaults to ``~/.bos``.
    """
    import os

    if bos_home := os.environ.get("BOS_HOME"):
        return Path(bos_home).expanduser()
    return Path.home() / ".bos"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _apply(fn: Callable, params: dict[str, Any]) -> Any:
    args, kwargs = _build_params(fn, params)
    return fn(*args, **kwargs)


def _safe_format(template: str, **kwargs: Any) -> str:
    class SafeMapping(dict):
        def __missing__(self, key: str) -> str:
            return f"{{{key}}}"

    return template.format_map(SafeMapping(kwargs))


def _load_json(source: Path | str, from_string: bool = False) -> dict[str, Any]:
    try:
        return json.loads(str(source) if from_string else Path(source).read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load JSON from %s", source, exc_info=True)
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to read text from %s", path, exc_info=True)
        return ""


def _resolve_path(path: str | Path = ".") -> Path:
    return Path(path).expanduser().resolve()


def _pick_collection(
    collection: dict[str, Any],
    include: Collection[str] | None = None,
    exclude: Collection[str] | None = None,
) -> dict[str, Any]:
    if include is not None:
        collection = {k: v for k, v in collection.items() if k in include}
    if exclude is not None:
        collection = {k: v for k, v in collection.items() if k not in exclude}
    return collection


def _allowed(name: str, include: Collection[str] | None = None, exclude: Collection[str] | None = None) -> bool:
    return (include is None or name in include) and (exclude is None or name not in exclude)


@contextmanager
def _flock(path: Path | str):
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
    from .agent import LLMResponse

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
    from .agent import ToolCallRequest

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
    for modname in modules:
        try:
            importlib.import_module(modname)
        except Exception:
            logger.warning("Failed to import extension module %s", modname)


def _load_ext_paths(paths: list[str | Path]) -> None:
    def _load_extension_module_file(path: Path) -> None:
        module_name = "agentloop_ext_" + str(abs(hash(str(path))))
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec and spec.loader:
            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                logger.warning("Failed to load extension file %s", path)
        else:
            logger.warning("Could not create import spec for extension file %s", path)

    files = {
        f.expanduser().resolve()
        for p in map(Path, paths)
        for f in ([p] if p.is_file() else (x for x in p.rglob("*.py") if not x.name.startswith("_")))
    }
    for f in files:
        _load_extension_module_file(f)


async def _aclose(instance: Any) -> None:
    if hasattr(instance, "aclose"):
        try:
            await instance.aclose()
        except Exception:
            logger.warning("aclose error in %s", type(instance).__name__, exc_info=True)
