"""Auto-recall turn interceptor (BEP 10 §3) — retrieves on the incoming message
and injects top hits as ephemeral context after the cache breakpoint. Records
surfaced entry-ids on TurnContext.metadata['recalled'] for the off-turn recall
log (the off-turn flush itself is BEP-11-gated)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from xml.sax.saxutils import escape

from .scoped_memory import MemoryBackend

if TYPE_CHECKING:
    from bos.core.agent import TurnContext

_EPHEMERAL_KEY = "memory_auto_recall"

InterceptStage = Literal[
    "prepare", "before_llm", "after_llm", "after_tool",
    "final_response", "max_iteration", "error",
]


def _incoming_text(context: "TurnContext") -> str:
    """Compatibility shim — prefers `context.get_last_user_text()` when present,
    falls back to a raw-dict scan so unit tests that pass mock contexts still work."""
    getter = getattr(context, "get_last_user_text", None)
    if callable(getter):
        return getter()
    # Fallback for test doubles whose `current` holds raw dicts rather than Messages.
    for msg in reversed(getattr(context, "current", []) or []):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "user":
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
            return content if isinstance(content, str) else str(content)
    return ""


class AutoRecallInterceptor:
    def __init__(self, backend: MemoryBackend, *, top_k: int = 5) -> None:
        self._backend = backend
        self._top_k = top_k

    async def intercept(self, stage: InterceptStage, context: "TurnContext") -> None:
        if stage != "prepare":
            return
        query = _incoming_text(context).strip()
        if not query:
            return
        hits = await self._backend.search_memories(query, top_k=self._top_k)
        if not hits:
            return
        items = "\n".join(
            f'<recalled id="{escape(h.id)}">{escape(h.content[:300])}</recalled>' for h in hits
        )
        block = f"<auto_recall>\nPossibly-relevant memories (context, not proof):\n{items}\n</auto_recall>"
        context.set_ephemeral_message(_EPHEMERAL_KEY, {"role": "user", "content": block})
        recalled: list[str] = context.metadata.setdefault("recalled", [])
        recalled.extend(h.id for h in hits)
