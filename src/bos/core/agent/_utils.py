from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any
from xml.sax.saxutils import escape

from ._content import MessageContent, content_as_parts

# The agent's own private helpers. Kept stdlib-only so ``core.agent`` is a
# dependency-free inner ring; ``core/_utils.py`` (outer) re-imports these for
# the shared ``_``-prefixed helper surface.


def _build_params(fn: Callable, params: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    sig = inspect.signature(fn)
    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    valid_params = params if has_varkw else {k: v for k, v in params.items() if k in sig.parameters}
    bound = sig.bind_partial(**valid_params)
    bound.apply_defaults()
    return bound.args, bound.kwargs


async def _apply_async(fn: Callable, params: dict[str, Any]) -> Any:
    args, kwargs = _build_params(fn, params)
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _compact(*dicts: dict, **kwargs: Any) -> dict[str, Any]:
    merged = {}
    [merged.update(d) for d in (*dicts, kwargs) if d is not None]
    return {k: v for k, v in merged.items() if v is not None}


def _xml_attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


_THINK_RE = re.compile(r"<think>[\s\S]*?</think>")
# Multi-actor history attribution labels assistant turns ("[assistant: Main]",
# "[assistant Main said]"). Thinking models tend to parrot the format they see
# in history straight back into their own reply.
_ECHOED_LABEL_RE = re.compile(r"\[assistant(?::[^\]\n]*|\s[^\]\n]*?\ssaid)\]")
# A leading "[thought: ...]" chain-of-thought prefix some models emit inline.
_LEADING_THOUGHT_RE = re.compile(r"^\s*\[thought:[^\]]*\]\s*", re.IGNORECASE)


def _strip_think(text: str | None) -> str | None:
    if not text:
        return None
    return _THINK_RE.sub("", text).strip() or None


def _strip_reply_artifacts(text: str | None, *, strip_labels: bool = False) -> str | None:
    """Clean reasoning/attribution noise a model leaks into a user-facing reply.

    - removes ``<think>...</think>`` blocks
    - when *strip_labels* (multi-actor chats), drops everything up to and
      including the last parroted ``[assistant: X]`` / ``[assistant X said]``
      label, which also discards any ``[thought: ...]`` that preceded it
    - removes a leading ``[thought: ...]`` chain-of-thought prefix
    """
    if not text:
        return None
    text = _THINK_RE.sub("", text)
    if strip_labels:
        labels = list(_ECHOED_LABEL_RE.finditer(text))
        if labels:
            text = text[labels[-1].end() :]
    text = _LEADING_THOUGHT_RE.sub("", text)
    return text.strip() or None


def _as_parts(content: MessageContent, cache: bool = False) -> list[dict[str, Any]]:
    parts = content_as_parts(content)
    return parts if not cache else parts[:-1] + [parts[-1] | {"cache_control": {"type": "ephemeral"}}]
