"""Memory consolidation handler (BEP 10 §4) — proposes structured operations
for off-turn curation. Uses BEP 11 BackgroundLLM with a JSON schema; never
writes directly (writes go through the L1 operation service)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from bos.core.contract import Message

from .operation_service import MemoryOperation
from .scoped_memory import MemoryEntry

logger = logging.getLogger(__name__)

JobTriggerName = Literal["session_close", "idle", "manual"]


@dataclass(frozen=True)
class ConsolidationPolicy:
    enabled: bool = False
    retention_days: int = 30
    auto_apply: bool = False


@dataclass(frozen=True)
class MemoryConsolidationRequest:
    chat_id: str
    actor_name: str | None
    scope: str
    base_revision: int
    trigger: JobTriggerName
    transcript_window: list[Message]
    raw_appends: list[MemoryEntry]
    candidate_memories: list[MemoryEntry]
    active_maxims: dict[str, str]
    policy: ConsolidationPolicy


class MemoryConsolidator(Protocol):
    async def propose(self, request: MemoryConsolidationRequest) -> list[MemoryOperation]: ...


_SYSTEM_PROMPT = """You are a memory consolidation agent.

Given a recent conversation window and the agent's existing memories, propose a
list of memory operations that:
- ADD durable user preferences, recurring feedback, or non-obvious project context
  worth recalling in future sessions.
- UPDATE an existing memory entry (target_id) when the conversation refines or
  corrects it.
- INVALIDATE an existing memory entry (target_id) when the conversation negates
  it; set requested_by="user" when the user explicitly said "stop using" or
  "forget" that fact, else "consolidator".
- NOOP when nothing in the window changes long-term memory.

Each op MUST include `reason` (one sentence rationale) and `source_turn_ids`
(turn ids from the window that justify it, when applicable). Do not ADD facts
derivable from current repository state or transient task chatter.

Reply ONLY with a JSON object matching the supplied schema."""


_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "op": {"enum": ["ADD", "UPDATE", "INVALIDATE", "NOOP"]},
                    "reason": {"type": "string"},
                    "source_turn_ids": {"type": "array", "items": {"type": "string"}},
                    "target_id": {"type": "string"},
                    "content": {"type": "string"},
                    "summary": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                    "maxim_key": {"type": "string"},
                    "requested_by": {"enum": ["user", "consolidator", "admin", "retention"]},
                },
                "required": ["op", "reason"],
            },
        },
    },
    "required": ["operations"],
}


def _render_user_prompt(request: MemoryConsolidationRequest) -> str:
    lines: list[str] = []
    lines.append("## Conversation window")
    for m in request.transcript_window:
        msg = m.llm_message
        role = msg.get("role", "?")
        content = msg.get("content", "")
        tid = m.turn_id or ""
        lines.append(f"[turn={tid}] {role}: {content}")
    lines.append("\n## Existing memories (candidates)")
    for e in request.candidate_memories:
        tags = ",".join(e.tags) if e.tags else ""
        lines.append(f"[id={e.id} tags={tags}] {e.content}")
    if request.active_maxims:
        lines.append("\n## Active maxims (note: 2048-char cap; consider Compact via UPDATE+maxim_key)")
        for key, text in request.active_maxims.items():
            lines.append(f"[maxim={key}] {text}")
    lines.append("\n## Policy")
    lines.append(f"scope={request.scope} trigger={request.trigger} auto_apply={request.policy.auto_apply}")
    return "\n".join(lines)


class DefaultMemoryConsolidator:
    def __init__(self, background_llm, *, maxim_keys: set[str], model: str | None = None) -> None:
        self._llm = background_llm
        self._maxim_keys = set(maxim_keys)
        self._model = model

    async def propose(self, request: MemoryConsolidationRequest) -> list[MemoryOperation]:
        resp = await self._llm.ask(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _render_user_prompt(request)},
            ],
            response_schema=_RESPONSE_SCHEMA,
            model=self._model,
        )
        try:
            payload = json.loads(resp.content or "")
        except json.JSONDecodeError:
            logger.warning("consolidator: failed to parse response JSON; treating as NOOP")
            return []
        ops_in = payload.get("operations", [])
        out: list[MemoryOperation] = []
        for raw in ops_in:
            try:
                out.append(MemoryOperation(
                    op=raw["op"], reason=raw["reason"],
                    source_turn_ids=list(raw.get("source_turn_ids", [])),
                    target_id=raw.get("target_id"),
                    content=raw.get("content"),
                    summary=raw.get("summary"),
                    tags=raw.get("tags"),
                    importance=raw.get("importance"),
                    maxim_key=raw.get("maxim_key"),
                    requested_by=raw.get("requested_by", "consolidator"),
                ))
            except (KeyError, TypeError):
                logger.warning("consolidator: dropping malformed op %r", raw)
        return out
