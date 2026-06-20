"""Component evaluation harness for the memory subsystem (BEP 10 §8).

Cheapest eval rung: retrieval recall@k over a labeled set. Runs in seconds and
unblocks prompt iteration. Routing eval (transcript -> action) is deferred until
the consolidation handler exists (BEP 10 P7, blocked on BEP 11)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievalCase:
    query: str
    relevant_ids: set[str] = field(default_factory=set)


async def recall_at_k(backend, cases: list[RetrievalCase], *, k: int = 5) -> float:
    """Mean recall@k: fraction of each case's relevant ids that appear in the
    top-k search results, averaged over the cases that have relevant ids. Cases
    with no relevant ids are skipped (not counted in the denominator). Returns
    0.0 when there are no cases, or none with relevant ids."""
    if not cases:
        return 0.0
    total = 0.0
    scored = 0
    for case in cases:
        if not case.relevant_ids:
            continue
        hits = await backend.search_memories(case.query, top_k=k)
        found = {h.id for h in hits} & case.relevant_ids
        total += len(found) / len(case.relevant_ids)
        scored += 1
    # Average only over scored cases; cases with no relevant ids are not
    # measurable recall and must not dilute the mean. No scorable case -> 0.0.
    if scored == 0:
        return 0.0
    return total / scored
