"""Memory consolidation (BEP 10 §4) — proposes structured operations for
off-turn curation, and the run that applies them. Proposal runs a disposable
agent (BEP 12 AgentRunner) with a JSON schema; it never writes directly (writes
go through the L1 operation service)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

from bos.core.agent import StructuredOutputError
from bos.core.contract import ChatStore, Message

from ._watermark import WatermarkStore
from .operation_service import DefaultMemoryOperationService, MemoryOperation
from .scoped_memory import MemoryBackend, MemoryEntry

logger = logging.getLogger(__name__)


class ConsolidationUnavailable(Exception):
    """Raised when the consolidator cannot produce a trustworthy proposal (e.g.
    the model returned an unparseable response). The caller treats this as "no
    result" and leaves the watermark untouched so the turns are retried, rather
    than silently burning the window with an empty apply."""


@dataclass(frozen=True)
class MemoryConsolidationRequest:
    chat_id: str
    actor_name: str
    base_revision: int
    transcript_window: list[Message]
    candidate_memories: list[MemoryEntry]
    active_maxims: dict[str, str]


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
                "required": ["op", "reason", "source_turn_ids"],
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
    lines.append(f"\n## Agent\n{request.actor_name}")
    return "\n".join(lines)


class DefaultMemoryConsolidator:
    def __init__(self, agent_runner, *, maxim_keys: set[str], model: str | None = None) -> None:
        self._runner = agent_runner
        self._maxim_keys = set(maxim_keys)
        self._model = model

    async def propose(self, request: MemoryConsolidationRequest) -> list[MemoryOperation]:
        # A disposable, history-less agent: a fresh internal chat-id each run
        # (no parent turn → off-turn), a tunable consolidation system prompt, no
        # tools, and structured output validated against _RESPONSE_SCHEMA.
        # NOTE (future): the memory plugin could expose a config setting to run a
        # *registered* agent kind here (run(kind=…) instead of this ad-hoc
        # agent_cfg), giving consolidation a fully configurable model/prompt/tools
        # like any other agent. Tracked as BEP 12 Open Issue #4.
        # Model precedence: configured model > BOS_CONSOLIDATOR_MODEL env >
        # None (the runner then falls back to BOS_MODEL in the provider).
        model = self._model or os.environ.get("BOS_CONSOLIDATOR_MODEL") or None
        try:
            result = await self._runner.run(
                _render_user_prompt(request),
                agent_cfg={"system_prompt": _SYSTEM_PROMPT, "tools": []},
                schema=_RESPONSE_SCHEMA,
                model=model,
            )
        except StructuredOutputError as exc:
            # No valid structured proposal is NOT "nothing to consolidate" — it
            # is a failure. Surface it so the run leaves the watermark in place
            # and retries these turns later, instead of advancing past them.
            raise ConsolidationUnavailable("consolidator: model returned no valid structured proposal") from exc
        payload = result.output
        ops_in = payload.get("operations", []) if isinstance(payload, dict) else []
        out: list[MemoryOperation] = []
        for raw in ops_in:
            try:
                out.append(
                    MemoryOperation(
                        op=raw["op"],
                        reason=raw["reason"],
                        source_turn_ids=list(raw.get("source_turn_ids", [])),
                        target_id=raw.get("target_id"),
                        content=raw.get("content"),
                        summary=raw.get("summary"),
                        tags=raw.get("tags"),
                        importance=raw.get("importance"),
                        maxim_key=raw.get("maxim_key"),
                        requested_by=raw.get("requested_by", "consolidator"),
                    )
                )
            except (KeyError, TypeError):
                logger.warning("consolidator: dropping malformed op %r", raw)
        return out


async def run_consolidation(
    *,
    actor_name: str,
    chat_id: str,
    base_revision: int,
    chat_store: ChatStore,
    backend: MemoryBackend,
    consolidator: MemoryConsolidator,
    operation_service: DefaultMemoryOperationService,
    watermarks: WatermarkStore,
    maxim_keys: set[str],
) -> None:
    """Consolidate one chat's unprocessed turns: read the window past the
    watermark, propose operations, apply them, then advance the watermark.

    Runs in-line in the caller's task. ``boscli memory consolidate`` is the only
    caller; an external scheduler drives repeat runs."""
    watermark = await watermarks.get(chat_id)
    if base_revision <= watermark:
        logger.info(
            "consolidation skipped (no new turns) chat=%s rev=%d wm=%d",
            chat_id,
            base_revision,
            watermark,
        )
        return
    transcript = await chat_store.get_messages_since(chat_id, revision=watermark)
    candidates = await backend.search_memories("", top_k=10_000)
    active_maxims = {key: await backend.get_maxim(key) for key in maxim_keys}
    request = MemoryConsolidationRequest(
        chat_id=chat_id,
        actor_name=actor_name,
        base_revision=base_revision,
        transcript_window=transcript,
        candidate_memories=candidates,
        active_maxims=active_maxims,
    )
    # A propose() failure (e.g. ConsolidationUnavailable on an unparseable
    # response, or a transport error) raises here; the watermark is left
    # untouched so these turns are retried on the next run rather than silently
    # burned by an empty apply.
    ops = await consolidator.propose(request)
    # Authoritative provenance for this run: the distinct turn ids actually in
    # the consolidated window, app-derived (order-preserving), recorded on every
    # audit record for audit/reconciliation.
    window_turn_ids = list(dict.fromkeys(m.turn_id for m in transcript if m.turn_id))
    await operation_service.apply(ops, window_turn_ids=window_turn_ids)
    # Advance the watermark only after a trustworthy proposal was applied (an
    # empty-but-valid proposal legitimately means "nothing durable here" and may
    # advance). Failures never reach this line.
    await watermarks.set(chat_id, base_revision)
