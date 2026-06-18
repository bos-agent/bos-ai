# BEP 10 Ready-Now Track (Storage + Capture/Read) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement BEP 10 phases 0–3 — upgrade the memory storage backend (metadata, soft-delete, ranked search, in-context index), add the L1 curation operation service, and rework the agent's capture/read path (in-context index, auto-recall, per-turn maxim rebuild, remove `Forget`) — without touching any off-turn consolidation machinery.

**Architecture:** Bottom-up by layer (BEP 10 §2). L0 storage gets a YAML-frontmatter metadata round-trip with soft-delete and ranked retrieval; the `MemoryBackend` protocol drops `forget_memory`/`optimize` and gains curation + index methods. L1 is a new `MemoryOperationService` — the single validated/audited/dry-run write door for curation, file-backed by an append-only audit JSONL. L6 reworks `MemoryAgentPlugin`: capture stays append-only (`Remember`/`ReviseMaxim`), `Forget` is removed, the maxim block + in-context index are memoized per turn (cache discipline, BEP 10 §5), and an auto-recall `TurnInterceptor` injects hits as ephemeral context after the cache breakpoint.

**Tech Stack:** Python ≥3.13, asyncio, `pyyaml` (added this plan), pytest/pytest-asyncio, the existing `pep_memory_backend` extension point and `ToolRegistry`/`TurnInterceptor` contracts.

---

## Scope and Boundaries

**In scope (BEP 10 §11 phases 0–3, the "ready-now track"):**
- P0 — Lock + seed: regression tests for current behavior; a minimal component-eval (retrieval recall@k) skeleton.
- P1 — L0 storage: metadata via YAML frontmatter incl. `source_turn_ids`; soft delete + default filtering; `list_index`; ranked `search`; clean-remove `optimize()` and `forget_memory` from the protocol.
- P2 — L1 operation service: `apply(ops, dry_run)` + audit + `requested_by`; `touch_last_used`; `search_candidates`; `restore`; `audit`.
- P3 — L6 capture+read: in-context index in the cached prefix; auto-recall interceptor; per-turn maxim/index rebuild (remove per-iteration backend re-read); keep `Remember`+`ReviseMaxim`; **remove `Forget`**.

**Explicitly OUT of scope — blocked on BEP 11 (no `JobRunner`/`LifecycleBus`/`BackgroundLLM` exist in the codebase):**
- P4 L3 lifecycle, P5 L2 `BackgroundLLM`, P6 L4 `JobRunner`, P7 L5 consolidation handler (`propose()`), P8 admin `consolidate`/`jobs` subcommands, P9 scored-ranking eval-gate + promote/reflect/link increments, P10 DSPy/GEPA.
- The auto-recall **off-turn flush** (recall log → `touch_last_used` at `turn_complete`) — the `touch_last_used` *API* ships in P2, but the `turn_complete` subscriber that calls it is BEP-11-gated. In this plan auto-recall only records surfaced ids on `TurnContext.metadata["recalled"]`.

## Design decisions / deviations from the BEP text (read before starting)

1. **`MemoryEntry.metadata` becomes non-optional** (`field(default_factory=dict)` instead of `dict | None = None`). All metadata reads go through a `_meta(entry, key, default)` helper so legacy `None` is tolerated. BEP §6 says metadata is "defaulted on read" — this realizes that.
2. **`last_used` is written through `update_memory`** (we add a `last_used=` kwarg to the *implementation* method). The BEP enumerates `update_memory(content,tags,importance,summary,links)` for curation; recency is written by the L1 `touch_last_used`, and routing it through the one backend write path avoids a second write door. Documented here so it is not read as scope creep.
3. **Per-turn maxim/index memoization lives in `MemoryAgentPlugin`** (keyed on `TurnContext.turn_id`), not in the agent loop. This is the smallest reversible diff that satisfies BEP §12 ("maxims read from storage at most once per turn") and keeps the agent runtime untouched. We do **not** remove the per-iteration `_build_system_prompt()` call at `agent.py:512`; instead the memory plugin returns a byte-identical cached section within a turn, which is what the cache discipline actually requires.
4. **`MemoryConsolidationRequest`, `MemoryOpKind=PROMOTE`-to-maxim folding, scored recency ranking** are defined where they are first *used* (P7/P9), not here, to keep this plan free of BEP-11 types (`JobTrigger`, `Message` windows). The `MemoryOperation` op-kinds `ADD/UPDATE/INVALIDATE/NOOP/LINK/PROMOTE` are all defined and validated in P2, but P2 tests exercise `ADD/UPDATE/INVALIDATE/NOOP` + dry-run (the v1 resolve set).
5. **Index ordering by importance happens in the backend** (`list_index` returns entries pre-ordered by `importance` desc then `created_at`), so `MemoryIndexEntry` need not expose `importance`; the plugin only caps to `index_max`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pyproject.toml` | declare `pyyaml` | modify (P1) |
| `src/bos/plugins/memory/scoped_memory.py` | `MemoryEntry`, `MemoryIndexEntry`, `RequestedBy`, `MemoryBackend` protocol, `ScopedMemory` | rewrite protocol + entry (P1) |
| `src/bos/plugins/memory/markdown_backend.py` | default file backend: frontmatter round-trip, soft-delete, ranked search, index | rewrite (P1) |
| `src/bos/extensions/memory_stores/in_memory.py` | in-memory backend mirroring the new protocol | rewrite (P1) |
| `src/bos/plugins/memory/operation_service.py` | **new** — `MemoryOperation`, `AuditRecord`, `RecallEvent`, `MemoryOperationService` protocol + `DefaultMemoryOperationService` | create (P2) |
| `src/bos/plugins/memory/_audit_log.py` | **new** — append-only JSONL audit + recall log helper | create (P2) |
| `src/bos/plugins/memory/auto_recall.py` | **new** — `AutoRecallInterceptor` | create (P3) |
| `src/bos/plugins/memory/plugin.py` | tools (drop `Forget`), per-turn memoized maxim+index section, retrieval config, interceptor wiring | modify (P3) |
| `src/bos/plugins/memory/__init__.py` | export new public types | modify (P1/P2) |
| `src/bos/plugins/memory/eval.py` | **new** — `recall_at_k` component-eval helper | create (P0) |
| `tests/test_markdown_backend.py` | **new** — regression + new-feature coverage for the file backend | create (P0/P1) |
| `tests/test_memory_operation_service.py` | **new** — L1 service coverage | create (P2) |
| `tests/test_memory_eval.py` | **new** — retrieval recall@k runs against a stub dataset | create (P0) |
| `tests/test_memory_extension.py` | existing tool/prompt tests — update for removed `Forget` + new behavior | modify (P1/P3) |

---

## Phase 0 — Lock + Seed

Pin current behavior with regression tests and stand up the cheapest eval rung *before* changing anything.

### Task 0.1: Regression test for the markdown backend (current behavior)

**Files:**
- Test: `tests/test_markdown_backend.py` (create)

- [ ] **Step 1: Write the regression test**

```python
"""Regression + feature tests for MarkdownMemoryBackend."""

import pytest

from bos.plugins.memory.markdown_backend import MarkdownMemoryBackend


def _backend(tmp_path):
    return MarkdownMemoryBackend(store_dir="memory", bos_dir=tmp_path)


class TestMarkdownRegression:
    @pytest.mark.asyncio
    async def test_maxim_roundtrip(self, tmp_path):
        b = _backend(tmp_path)
        await b.set_maxim("user", "likes Python")
        assert await b.get_maxim("user") == "likes Python"

    @pytest.mark.asyncio
    async def test_ingest_get_search(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("PostgreSQL 16 on RDS", tags=["db"])
        entry = await b.get_memory(eid)
        assert entry is not None and entry.content == "PostgreSQL 16 on RDS"
        assert "db" in entry.tags
        hits = await b.search_memories("postgresql")
        assert [h.id for h in hits] == [eid]
```

- [ ] **Step 2: Run to verify it passes against current code**

Run: `uv run pytest -q tests/test_markdown_backend.py`
Expected: PASS (3 tests). This locks current behavior before P1 rewrites the backend.

- [ ] **Step 3: Commit**

```bash
git add tests/test_markdown_backend.py
git commit -m "test(memory): lock current markdown backend behavior"
```

### Task 0.2: Component-eval skeleton (retrieval recall@k)

**Files:**
- Create: `src/bos/plugins/memory/eval.py`
- Test: `tests/test_memory_eval.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Component eval skeleton — retrieval recall@k over a stub dataset."""

import pytest
from conftest import InMemMemoryExtension

from bos.plugins.memory.eval import RetrievalCase, recall_at_k


@pytest.mark.asyncio
async def test_recall_at_k_perfect_retrieval():
    store = InMemMemoryExtension()
    pg = await store.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
    await store.ingest_memory("unrelated note about cats", tags=["pets"])
    cases = [RetrievalCase(query="postgresql", relevant_ids={pg})]
    score = await recall_at_k(store, cases, k=5)
    assert score == 1.0


@pytest.mark.asyncio
async def test_recall_at_k_miss():
    store = InMemMemoryExtension()
    await store.ingest_memory("note about cats", tags=["pets"])
    cases = [RetrievalCase(query="postgresql", relevant_ids={"missing"})]
    score = await recall_at_k(store, cases, k=5)
    assert score == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_memory_eval.py`
Expected: FAIL — `ModuleNotFoundError: bos.plugins.memory.eval`.

- [ ] **Step 3: Write the eval helper**

```python
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
    top-k search results, averaged over cases. Returns 0.0 for an empty set."""
    if not cases:
        return 0.0
    total = 0.0
    for case in cases:
        if not case.relevant_ids:
            continue
        hits = await backend.search_memories(case.query, top_k=k)
        found = {h.id for h in hits} & case.relevant_ids
        total += len(found) / len(case.relevant_ids)
    return total / len(cases)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/test_memory_eval.py`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/eval.py tests/test_memory_eval.py
git commit -m "feat(memory): add retrieval recall@k component-eval skeleton"
```

---

## Phase 1 — L0 Storage (metadata, soft-delete, ranked search, index)

Depends on P0. Rewrites the `MemoryBackend` protocol and both backends. After this phase: metadata round-trips, invalid entries are hidden by default, `restore` works, and `optimize()`/`forget_memory` are gone from the protocol.

### Task 1.1: Add the `pyyaml` dependency

**Files:**
- Modify: `pyproject.toml` (dependencies list, line ~8–24)

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to the `dependencies = [ ... ]` list (it is currently only a transitive dep via litellm):

```toml
    "pyyaml==6.0.3",
```

- [ ] **Step 2: Sync and verify import**

Run: `uv sync && uv run python -c "import yaml; print(yaml.__version__)"`
Expected: prints `6.0.3`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(memory): declare pyyaml dependency for frontmatter"
```

### Task 1.2: New `MemoryEntry`, `MemoryIndexEntry`, `RequestedBy`, and protocol

**Files:**
- Modify: `src/bos/plugins/memory/scoped_memory.py`
- Test: `tests/test_markdown_backend.py` (add cases in later tasks)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_markdown_backend.py`:

```python
class TestMemoryEntryDefaults:
    def test_metadata_defaults_to_dict(self):
        from bos.plugins.memory.scoped_memory import MemoryEntry

        e = MemoryEntry(id="x", content="c")
        assert e.metadata == {}

    def test_index_entry_shape(self):
        from bos.plugins.memory.scoped_memory import MemoryIndexEntry

        ie = MemoryIndexEntry(id="x", tags=["a"], summary="one line")
        assert (ie.id, ie.tags, ie.summary) == ("x", ["a"], "one line")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_markdown_backend.py::TestMemoryEntryDefaults`
Expected: FAIL — `ImportError: cannot import name 'MemoryIndexEntry'` and `metadata` is `None`.

- [ ] **Step 3: Rewrite `scoped_memory.py`**

Replace the entire file with:

```python
"""Memory backend types — entries, index, protocol, scoped wrapper (BEP 10 §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

RequestedBy = Literal["user", "consolidator", "admin", "retention"]


@dataclass
class MemoryEntry:
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    # importance:int(1-10), valid:bool, invalidated_at, invalidated_by,
    # last_used, links:list[str], source_turn_ids:list[str], summary
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryIndexEntry:
    id: str
    tags: list[str]
    summary: str


def _meta(entry: MemoryEntry, key: str, default):
    """Read a metadata field tolerating legacy entries (metadata None/missing)."""
    md = entry.metadata or {}
    val = md.get(key, default)
    return default if val is None else val


class MemoryBackend(Protocol):
    # maxims — unchanged
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...

    # capture (raw append) + read
    async def ingest_memory(
        self, content: str, *, tags: list[str] | None = None, importance: int = 5,
        summary: str | None = None, source_turn_ids: list[str] | None = None,
    ) -> str: ...
    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None: ...
    async def search_memories(
        self, query: str, *, top_k: int = 5, include_invalid: bool = False,
    ) -> list[MemoryEntry]: ...
    async def list_index(self) -> list[MemoryIndexEntry]: ...

    # curation writes (driven by the L1 operation service)
    async def update_memory(
        self, entry_id: str, *, content: str | None = None, tags: list[str] | None = None,
        importance: int | None = None, summary: str | None = None,
        links: list[str] | None = None, last_used: str | None = None,
    ) -> None: ...
    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None: ...
    async def restore_memory(self, entry_id: str) -> None: ...
    async def purge_invalidated(self, *, older_than_days: int) -> int: ...


class ScopedMemory:
    """MemoryBackend wrapper that presents an actor-scoped memory view."""

    def __init__(self, inner: MemoryBackend, scope: str) -> None:
        self._inner = inner
        self._scope = scope

    @property
    def scope(self) -> str:
        return self._scope

    def _maxim_key(self, key: str) -> str:
        key = key.lower()
        return "user" if key == "user" else f"actors:{self._scope}:{key}"

    def _scope_tag(self) -> str:
        return f"scope:{self._scope}"

    def _is_visible(self, entry: MemoryEntry) -> bool:
        tags = set(entry.tags)
        return self._scope_tag() in tags or "scope:global" in tags

    async def get_maxim(self, key: str) -> str:
        return await self._inner.get_maxim(self._maxim_key(key))

    async def set_maxim(self, key: str, content: str) -> None:
        await self._inner.set_maxim(self._maxim_key(key), content)

    async def ingest_memory(
        self, content: str, *, tags: list[str] | None = None, importance: int = 5,
        summary: str | None = None, source_turn_ids: list[str] | None = None,
    ) -> str:
        scoped_tags = [*(tags or []), self._scope_tag()]
        return await self._inner.ingest_memory(
            content, tags=scoped_tags, importance=importance,
            summary=summary, source_turn_ids=source_turn_ids,
        )

    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None:
        entry = await self._inner.get_memory(entry_id, include_invalid=include_invalid)
        return entry if entry is not None and self._is_visible(entry) else None

    async def search_memories(
        self, query: str, *, top_k: int = 5, include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        entries = await self._inner.search_memories(
            query, top_k=max(top_k * 4, 20), include_invalid=include_invalid,
        )
        return [e for e in entries if self._is_visible(e)][:top_k]

    async def list_index(self) -> list[MemoryIndexEntry]:
        # The inner backend orders by importance; we cannot re-check visibility on
        # an index entry (no tags-on-disk guarantee), so filter via the full list.
        full = await self._inner.search_memories("", top_k=10_000, include_invalid=False)
        visible = {e.id for e in full if self._is_visible(e)}
        return [ie for ie in await self._inner.list_index() if ie.id in visible]

    async def update_memory(self, entry_id: str, **kwargs) -> None:
        if await self.get_memory(entry_id, include_invalid=True) is not None:
            await self._inner.update_memory(entry_id, **kwargs)

    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None:
        if await self.get_memory(entry_id, include_invalid=True) is not None:
            await self._inner.invalidate_memory(entry_id, requested_by=requested_by)

    async def restore_memory(self, entry_id: str) -> None:
        if await self.get_memory(entry_id, include_invalid=True) is not None:
            await self._inner.restore_memory(entry_id)

    async def purge_invalidated(self, *, older_than_days: int) -> int:
        return await self._inner.purge_invalidated(older_than_days=older_than_days)
```

> Note: an empty-string `search_memories("")` must return all valid entries (used by `ScopedMemory.list_index` and the index path). The backends in 1.3/1.4 treat an empty query as "match all".

- [ ] **Step 4: Run to verify the entry/index test passes**

Run: `uv run pytest -q tests/test_markdown_backend.py::TestMemoryEntryDefaults`
Expected: PASS. (Backend tests will fail until 1.3 — expected; run only this class.)

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/scoped_memory.py tests/test_markdown_backend.py
git commit -m "feat(memory): new MemoryEntry metadata + MemoryBackend protocol (L0)"
```

### Task 1.3: Rewrite `MarkdownMemoryBackend` with frontmatter + soft-delete + ranked search + index

**Files:**
- Modify: `src/bos/plugins/memory/markdown_backend.py`
- Test: `tests/test_markdown_backend.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_markdown_backend.py`:

```python
class TestMarkdownMetadataAndSoftDelete:
    @pytest.mark.asyncio
    async def test_metadata_roundtrips(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory(
            "deploys on Fridays", tags=["ops"], importance=8,
            summary="Friday deploys", source_turn_ids=["t1", "t2"],
        )
        e = await b.get_memory(eid)
        assert e.metadata["importance"] == 8
        assert e.metadata["valid"] is True
        assert e.metadata["summary"] == "Friday deploys"
        assert e.metadata["source_turn_ids"] == ["t1", "t2"]
        assert "ops" in e.tags

    @pytest.mark.asyncio
    async def test_invalidate_hides_by_default_and_restore(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("temporary fact", tags=["x"])
        await b.invalidate_memory(eid, requested_by="user")
        assert await b.get_memory(eid) is None
        assert await b.search_memories("temporary") == []
        got = await b.get_memory(eid, include_invalid=True)
        assert got.metadata["valid"] is False
        assert got.metadata["invalidated_by"] == "user"
        await b.restore_memory(eid)
        assert (await b.get_memory(eid)).metadata["valid"] is True

    @pytest.mark.asyncio
    async def test_search_ranks_importance_over_recency(self, tmp_path):
        b = _backend(tmp_path)
        low = await b.ingest_memory("alpha project notes", importance=2)
        high = await b.ingest_memory("alpha project plan", importance=9)
        hits = await b.search_memories("alpha", top_k=5)
        assert [h.id for h in hits][:2] == [high, low]

    @pytest.mark.asyncio
    async def test_list_index_orders_by_importance_and_excludes_invalid(self, tmp_path):
        b = _backend(tmp_path)
        a = await b.ingest_memory("aaa", importance=3, summary="A")
        c = await b.ingest_memory("ccc", importance=9, summary="C")
        bad = await b.ingest_memory("bbb", importance=10)
        await b.invalidate_memory(bad, requested_by="consolidator")
        idx = await b.list_index()
        ids = [ie.id for ie in idx]
        assert ids == [c, a]
        assert bad not in ids
        assert idx[0].summary == "C"

    @pytest.mark.asyncio
    async def test_update_memory_changes_fields(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("old", importance=5)
        await b.update_memory(eid, content="new", importance=7, links=["other"])
        e = await b.get_memory(eid)
        assert e.content == "new"
        assert e.metadata["importance"] == 7
        assert e.metadata["links"] == ["other"]

    @pytest.mark.asyncio
    async def test_purge_invalidated_respects_age(self, tmp_path):
        b = _backend(tmp_path)
        eid = await b.ingest_memory("purge me")
        await b.invalidate_memory(eid, requested_by="retention")
        # invalidated just now -> not purged with a 30-day window
        assert await b.purge_invalidated(older_than_days=30) == 0
        assert await b.get_memory(eid, include_invalid=True) is not None
        # older_than_days=-1 forces purge regardless of age
        assert await b.purge_invalidated(older_than_days=-1) == 1
        assert await b.get_memory(eid, include_invalid=True) is None

    @pytest.mark.asyncio
    async def test_legacy_plain_file_defaults_metadata(self, tmp_path):
        b = _backend(tmp_path)
        # write a pre-BEP10 file (tags header + body, no frontmatter)
        (b._memories_dir / "legacy01.md").write_text("tags:db\nlegacy content", encoding="utf-8")
        e = await b.get_memory("legacy01")
        assert e.content == "legacy content"
        assert e.tags == ["db"]
        assert e.metadata["valid"] is True
        assert e.metadata["importance"] == 5
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_markdown_backend.py::TestMarkdownMetadataAndSoftDelete`
Expected: FAIL — `ingest_memory()` rejects `importance`/`summary`/`source_turn_ids`; no `invalidate_memory`/`list_index`/`update_memory`/`restore_memory`/`purge_invalidated`.

- [ ] **Step 3: Rewrite `markdown_backend.py`**

Replace the entire file with:

```python
"""Markdown-file memory backend with YAML frontmatter metadata (BEP 10 §6).

Maxims live in ``maxims/`` as plain text. Memories live in ``memories/`` as a
YAML frontmatter block followed by the content body. Legacy files (tags-header
or plain content, no frontmatter) are read with defaulted metadata."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from bos.core import _flock

from .plugin import pep_memory_backend
from .scoped_memory import MemoryEntry, MemoryIndexEntry, RequestedBy

logger = logging.getLogger(__name__)

_DEFAULT_META = {
    "importance": 5,
    "valid": True,
    "invalidated_at": None,
    "invalidated_by": None,
    "last_used": None,
    "links": [],
    "source_turn_ids": [],
    "summary": None,
}


@pep_memory_backend(name="_default")
class MarkdownMemoryBackend:
    def __init__(self, store_dir: str | Path | None = None, bos_dir: str | Path | None = None) -> None:
        store_dir = Path(store_dir).expanduser() if store_dir else "memory"
        self._dir = Path(bos_dir or ".").expanduser().resolve() / store_dir
        self._maxims_dir = self._dir / "maxims"
        self._memories_dir = self._dir / "memories"
        self._maxims_dir.mkdir(parents=True, exist_ok=True)
        self._memories_dir.mkdir(parents=True, exist_ok=True)

    # ── serialization helpers ──

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Failed to read text from %s", path, exc_info=False)
            return ""

    @classmethod
    def _file_to_entry(cls, path: Path) -> MemoryEntry | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return None
        if not raw.strip():
            return None
        meta = dict(_DEFAULT_META)
        tags: list[str] = []
        created_at = datetime.fromtimestamp(path.stat().st_ctime).isoformat()
        if raw.startswith("---\n"):
            _, _, rest = raw.partition("---\n")
            fm, _, body = rest.partition("\n---\n")
            try:
                front = yaml.safe_load(fm) or {}
            except yaml.YAMLError:
                logger.warning("Bad frontmatter in %s", path, exc_info=True)
                front = {}
            tags = list(front.pop("tags", []) or [])
            created_at = front.pop("created_at", created_at) or created_at
            for k, default in _DEFAULT_META.items():
                meta[k] = front.get(k, default)
                if meta[k] is None and default is not None:
                    meta[k] = default
            content = body.strip()
        else:  # legacy: optional "tags:" header + body
            lines = raw.splitlines()
            tags_line = lines[0] if lines and lines[0].startswith("tags:") else ""
            body = "\n".join(lines[1:]) if tags_line else raw
            tags = [t.strip() for t in tags_line.removeprefix("tags:").split(",") if t.strip()]
            content = body.strip()
        return MemoryEntry(id=path.stem, content=content, tags=tags, created_at=created_at, metadata=meta)

    @staticmethod
    def _serialize(entry: MemoryEntry) -> str:
        front = {"tags": entry.tags, "created_at": entry.created_at, **entry.metadata}
        fm = yaml.safe_dump(front, sort_keys=True, allow_unicode=True).strip()
        return f"---\n{fm}\n---\n{entry.content}\n"

    def _write_entry(self, entry: MemoryEntry) -> None:
        path = self._memories_dir / f"{entry.id}.md"
        with _flock(path):
            path.write_text(self._serialize(entry), encoding="utf-8")

    # ── maxims ──

    async def get_maxim(self, key: str) -> str:
        return await asyncio.to_thread(self._read_text_sync, self._maxims_dir / f"{key.lower()}.md")

    async def set_maxim(self, key: str, content: str) -> None:
        path = self._maxims_dir / f"{key.lower()}.md"

        def _write() -> None:
            with _flock(path):
                path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    # ── capture + read ──

    async def ingest_memory(
        self, content: str, *, tags: list[str] | None = None, importance: int = 5,
        summary: str | None = None, source_turn_ids: list[str] | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex[:12]
        meta = dict(_DEFAULT_META)
        meta["importance"] = importance
        meta["summary"] = summary
        meta["source_turn_ids"] = list(source_turn_ids or [])
        entry = MemoryEntry(
            id=entry_id, content=content, tags=list(tags or []),
            created_at=datetime.now().isoformat(), metadata=meta,
        )
        await asyncio.to_thread(self._write_entry, entry)
        return entry_id

    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None:
        entry = await asyncio.to_thread(self._file_to_entry, self._memories_dir / f"{entry_id}.md")
        if entry is None:
            return None
        if not include_invalid and not entry.metadata.get("valid", True):
            return None
        return entry

    def _all_entries(self, *, include_invalid: bool) -> list[MemoryEntry]:
        out = []
        for path in self._memories_dir.glob("*.md"):
            if entry := self._file_to_entry(path):
                if include_invalid or entry.metadata.get("valid", True):
                    out.append(entry)
        return out

    async def search_memories(
        self, query: str, *, top_k: int = 5, include_invalid: bool = False,
    ) -> list[MemoryEntry]:
        def _search() -> list[MemoryEntry]:
            tokens = [t for t in query.lower().split() if t]
            scored = []
            for e in self._all_entries(include_invalid=include_invalid):
                text = (e.content + " " + " ".join(e.tags)).lower()
                lex = sum(1 for t in tokens if t in text) if tokens else 1
                if lex == 0:
                    continue
                scored.append((lex, e.metadata.get("importance", 5), e.created_at, e))
            scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
            return [s[3] for s in scored[:top_k]]

        return await asyncio.to_thread(_search)

    async def list_index(self) -> list[MemoryIndexEntry]:
        def _index() -> list[MemoryIndexEntry]:
            entries = self._all_entries(include_invalid=False)
            entries.sort(key=lambda e: (e.metadata.get("importance", 5), e.created_at), reverse=True)
            out = []
            for e in entries:
                summary = e.metadata.get("summary") or (e.content[:80] + ("…" if len(e.content) > 80 else ""))
                out.append(MemoryIndexEntry(id=e.id, tags=e.tags, summary=summary))
            return out

        return await asyncio.to_thread(_index)

    # ── curation writes ──

    async def update_memory(
        self, entry_id: str, *, content=None, tags=None, importance=None,
        summary=None, links=None, last_used=None,
    ) -> None:
        def _update() -> None:
            entry = self._file_to_entry(self._memories_dir / f"{entry_id}.md")
            if entry is None:
                return
            if content is not None:
                entry.content = content
            if tags is not None:
                entry.tags = list(tags)
            if importance is not None:
                entry.metadata["importance"] = importance
            if summary is not None:
                entry.metadata["summary"] = summary
            if links is not None:
                entry.metadata["links"] = list(links)
            if last_used is not None:
                entry.metadata["last_used"] = last_used
            self._write_entry(entry)

        await asyncio.to_thread(_update)

    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None:
        def _invalidate() -> None:
            entry = self._file_to_entry(self._memories_dir / f"{entry_id}.md")
            if entry is None:
                return
            entry.metadata["valid"] = False
            entry.metadata["invalidated_at"] = datetime.now().isoformat()
            entry.metadata["invalidated_by"] = requested_by
            self._write_entry(entry)

        await asyncio.to_thread(_invalidate)

    async def restore_memory(self, entry_id: str) -> None:
        def _restore() -> None:
            entry = self._file_to_entry(self._memories_dir / f"{entry_id}.md")
            if entry is None:
                return
            entry.metadata["valid"] = True
            entry.metadata["invalidated_at"] = None
            entry.metadata["invalidated_by"] = None
            self._write_entry(entry)

        await asyncio.to_thread(_restore)

    async def purge_invalidated(self, *, older_than_days: int) -> int:
        def _purge() -> int:
            cutoff = datetime.now() - timedelta(days=older_than_days)
            count = 0
            for path in self._memories_dir.glob("*.md"):
                entry = self._file_to_entry(path)
                if entry is None or entry.metadata.get("valid", True):
                    continue
                inv_at = entry.metadata.get("invalidated_at")
                try:
                    when = datetime.fromisoformat(inv_at) if inv_at else cutoff
                except ValueError:
                    when = cutoff
                if when <= cutoff:
                    path.unlink(missing_ok=True)
                    count += 1
            return count

        return await asyncio.to_thread(_purge)
```

- [ ] **Step 4: Run to verify the new tests pass**

Run: `uv run pytest -q tests/test_markdown_backend.py`
Expected: PASS (all classes, including 0.1's `TestMarkdownRegression` and the legacy-file test).

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/markdown_backend.py tests/test_markdown_backend.py
git commit -m "feat(memory): markdown backend frontmatter, soft-delete, index, ranked search (L0)"
```

### Task 1.4: Mirror the new protocol in `InMemMemoryExtension`

**Files:**
- Modify: `src/bos/extensions/memory_stores/in_memory.py`
- Test: `tests/test_memory_extension.py` (`TestInMemBackend`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_memory_extension.py`, replace `test_optimize_is_noop` (it references a removed method) and `test_inmem_memory_get_and_forget` (references removed `forget_memory`) and add new cases. Replace the `TestInMemBackend` methods `test_inmem_memory_get_and_forget` and `test_optimize_is_noop` with:

```python
    @pytest.mark.asyncio
    async def test_inmem_metadata_and_invalidate(self):
        store = InMemMemoryExtension()
        eid = await store.ingest_memory("fact", tags=["t"], importance=7, summary="s")
        entry = await store.get_memory(eid)
        assert entry.metadata["importance"] == 7
        assert entry.metadata["valid"] is True
        await store.invalidate_memory(eid, requested_by="user")
        assert await store.get_memory(eid) is None
        assert (await store.get_memory(eid, include_invalid=True)).metadata["valid"] is False
        await store.restore_memory(eid)
        assert await store.get_memory(eid) is not None

    @pytest.mark.asyncio
    async def test_inmem_list_index_orders_by_importance(self):
        store = InMemMemoryExtension()
        a = await store.ingest_memory("a", importance=2, summary="A")
        b = await store.ingest_memory("b", importance=9, summary="B")
        idx = await store.list_index()
        assert [ie.id for ie in idx] == [b, a]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_memory_extension.py::TestInMemBackend`
Expected: FAIL — no `invalidate_memory`/`restore_memory`/`list_index`; `ingest_memory` rejects `importance`.

- [ ] **Step 3: Rewrite `in_memory.py`**

Replace the entire file with:

```python
"""In-memory memory backend mirroring the BEP 10 §6 MemoryBackend protocol."""

from __future__ import annotations

from datetime import datetime, timedelta

from bos.plugins.memory import MemoryEntry, MemoryIndexEntry, pep_memory_backend
from bos.plugins.memory.scoped_memory import RequestedBy

_DEFAULT_META = {
    "importance": 5, "valid": True, "invalidated_at": None, "invalidated_by": None,
    "last_used": None, "links": [], "source_turn_ids": [], "summary": None,
}


@pep_memory_backend(name="in_memory")
class InMemMemoryExtension:
    def __init__(self, **maxims: str) -> None:
        self._maxims = {k.lower(): v for k, v in maxims.items()}
        self._memories: dict[str, MemoryEntry] = {}
        self._counter = 0

    async def get_maxim(self, key: str) -> str:
        return self._maxims.get(key.lower(), "")

    async def set_maxim(self, key: str, content: str) -> None:
        self._maxims[key.lower()] = content

    async def ingest_memory(
        self, content: str, *, tags=None, importance: int = 5, summary=None, source_turn_ids=None,
    ) -> str:
        self._counter += 1
        entry_id = f"mem_{self._counter}"
        meta = dict(_DEFAULT_META)
        meta.update(importance=importance, summary=summary, source_turn_ids=list(source_turn_ids or []))
        self._memories[entry_id] = MemoryEntry(
            id=entry_id, content=content, tags=list(tags or []),
            created_at=datetime.now().isoformat(), metadata=meta,
        )
        return entry_id

    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None:
        e = self._memories.get(entry_id)
        if e is None or (not include_invalid and not e.metadata.get("valid", True)):
            return None
        return e

    async def search_memories(self, query: str, *, top_k: int = 5, include_invalid: bool = False):
        tokens = [t for t in query.lower().split() if t]
        scored = []
        for e in self._memories.values():
            if not include_invalid and not e.metadata.get("valid", True):
                continue
            text = (e.content + " " + " ".join(e.tags)).lower()
            lex = sum(1 for t in tokens if t in text) if tokens else 1
            if lex == 0:
                continue
            scored.append((lex, e.metadata.get("importance", 5), e.created_at, e))
        scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
        return [s[3] for s in scored[:top_k]]

    async def list_index(self):
        entries = [e for e in self._memories.values() if e.metadata.get("valid", True)]
        entries.sort(key=lambda e: (e.metadata.get("importance", 5), e.created_at), reverse=True)
        return [
            MemoryIndexEntry(
                id=e.id, tags=e.tags,
                summary=e.metadata.get("summary") or (e.content[:80] + ("…" if len(e.content) > 80 else "")),
            )
            for e in entries
        ]

    async def update_memory(
        self, entry_id: str, *, content=None, tags=None, importance=None, summary=None, links=None, last_used=None,
    ) -> None:
        e = self._memories.get(entry_id)
        if e is None:
            return
        if content is not None:
            e.content = content
        if tags is not None:
            e.tags = list(tags)
        if importance is not None:
            e.metadata["importance"] = importance
        if summary is not None:
            e.metadata["summary"] = summary
        if links is not None:
            e.metadata["links"] = list(links)
        if last_used is not None:
            e.metadata["last_used"] = last_used

    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None:
        e = self._memories.get(entry_id)
        if e is None:
            return
        e.metadata.update(valid=False, invalidated_at=datetime.now().isoformat(), invalidated_by=requested_by)

    async def restore_memory(self, entry_id: str) -> None:
        e = self._memories.get(entry_id)
        if e is None:
            return
        e.metadata.update(valid=True, invalidated_at=None, invalidated_by=None)

    async def purge_invalidated(self, *, older_than_days: int) -> int:
        cutoff = datetime.now() - timedelta(days=older_than_days)
        to_drop = []
        for eid, e in self._memories.items():
            if e.metadata.get("valid", True):
                continue
            inv_at = e.metadata.get("invalidated_at")
            try:
                when = datetime.fromisoformat(inv_at) if inv_at else cutoff
            except ValueError:
                when = cutoff
            if when <= cutoff:
                to_drop.append(eid)
        for eid in to_drop:
            del self._memories[eid]
        return len(to_drop)
```

- [ ] **Step 4: Update `__init__.py` exports**

In `src/bos/plugins/memory/__init__.py`, add `MemoryIndexEntry` and `RequestedBy` to the imports from `scoped_memory` and to `__all__` (mirror how `MemoryEntry` is exported). Verify by:

Run: `uv run python -c "from bos.plugins.memory import MemoryIndexEntry; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Run to verify the in-mem tests pass**

Run: `uv run pytest -q tests/test_memory_extension.py::TestInMemBackend`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bos/extensions/memory_stores/in_memory.py src/bos/plugins/memory/__init__.py tests/test_memory_extension.py
git commit -m "feat(memory): in-memory backend mirrors new protocol (L0)"
```

### Task 1.5: Confirm `optimize()`/`forget_memory` are fully removed

**Files:**
- Test: `tests/test_markdown_backend.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_markdown_backend.py`:

```python
class TestRemovedMethods:
    def test_protocol_has_no_optimize_or_forget(self):
        from bos.plugins.memory.scoped_memory import MemoryBackend

        names = set(dir(MemoryBackend))
        assert "optimize" not in names
        assert "forget_memory" not in names

    def test_backends_have_no_optimize(self):
        from bos.extensions.memory_stores.in_memory import InMemMemoryExtension
        from bos.plugins.memory.markdown_backend import MarkdownMemoryBackend

        assert not hasattr(InMemMemoryExtension, "optimize")
        assert not hasattr(MarkdownMemoryBackend, "optimize")
        assert not hasattr(InMemMemoryExtension, "forget_memory")
        assert not hasattr(MarkdownMemoryBackend, "forget_memory")
```

- [ ] **Step 2: Run**

Run: `uv run pytest -q tests/test_markdown_backend.py::TestRemovedMethods`
Expected: PASS (1.2–1.4 already removed them). If FAIL, grep for stragglers:

```bash
grep -rn "def optimize\|forget_memory\|\.optimize(" src/bos tests
```

Expected after fixes: matches only in the consolidation/chat-store `ep_consolidator` code (unrelated) — none in `src/bos/plugins/memory` or `src/bos/extensions/memory_stores`.

- [ ] **Step 3: Run the full suite to catch fallout**

Run: `uv run pytest -q`
Expected: failures only in tests that still call `Forget` or `optimize` (fixed in P3 / already removed in 1.4). Note any failing test names; the Forget-tool tests are removed in Task 3.1.

- [ ] **Step 4: Commit**

```bash
git add tests/test_markdown_backend.py
git commit -m "test(memory): assert optimize()/forget_memory removed from L0"
```

---

## Phase 2 — L1 Memory Operation Service

Depends on L0. The single validated/audited/dry-run write door for curation. Note: nothing *calls* `apply()` from a trigger yet (that is P7, blocked on BEP 11); P2 delivers the service + audit so the admin `dry-run`/`restore`/`audit` paths (a later, partial admin surface) and the future handler can use it.

### Task 2.1: Audit + recall log helper

**Files:**
- Create: `src/bos/plugins/memory/_audit_log.py`
- Test: `tests/test_memory_operation_service.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the L1 memory operation service and audit log."""

import pytest
from conftest import InMemMemoryExtension

from bos.plugins.memory._audit_log import JsonlLog


class TestJsonlLog:
    @pytest.mark.asyncio
    async def test_append_and_read_roundtrip(self, tmp_path):
        log = JsonlLog(tmp_path / "audit.jsonl")
        await log.append({"op": "ADD", "result": "applied"})
        await log.append({"op": "NOOP", "result": "noop"})
        rows = await log.read()
        assert [r["result"] for r in rows] == ["applied", "noop"]

    @pytest.mark.asyncio
    async def test_read_missing_file_is_empty(self, tmp_path):
        log = JsonlLog(tmp_path / "nope.jsonl")
        assert await log.read() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_memory_operation_service.py::TestJsonlLog`
Expected: FAIL — `ModuleNotFoundError: bos.plugins.memory._audit_log`.

- [ ] **Step 3: Write the helper**

```python
"""Append-only JSONL log used for the curation audit trail and recall log."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bos.core import _flock


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    async def append(self, row: dict) -> None:
        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with _flock(self._path):
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

        await asyncio.to_thread(_write)

    async def read(self) -> list[dict]:
        def _read() -> list[dict]:
            if not self._path.exists():
                return []
            rows = []
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
            return rows

        return await asyncio.to_thread(_read)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/test_memory_operation_service.py::TestJsonlLog`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/_audit_log.py tests/test_memory_operation_service.py
git commit -m "feat(memory): append-only JSONL audit/recall log helper (L1)"
```

### Task 2.2: `MemoryOperation` / `AuditRecord` / `RecallEvent` data contracts

**Files:**
- Create: `src/bos/plugins/memory/operation_service.py`
- Test: `tests/test_memory_operation_service.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_memory_operation_service.py`:

```python
class TestDataContracts:
    def test_operation_defaults(self):
        from bos.plugins.memory.operation_service import MemoryOperation

        op = MemoryOperation(op="ADD", reason="seen in transcript", content="a fact")
        assert op.requested_by == "consolidator"
        assert op.source_turn_ids == []
        assert op.target_id is None

    def test_audit_record_shape(self):
        from bos.plugins.memory.operation_service import AuditRecord, MemoryOperation

        rec = AuditRecord(
            op=MemoryOperation(op="NOOP", reason="declined"),
            result="noop", entry_id=None, at="2026-06-17T00:00:00",
        )
        assert rec.result == "noop"
        assert rec.error is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_memory_operation_service.py::TestDataContracts`
Expected: FAIL — module/classes missing.

- [ ] **Step 3: Write the data contracts (top of `operation_service.py`)**

```python
"""L1 memory operation service — the single validated/audited/dry-run write door
for curation (BEP 10 §4). Raw agent appends bypass this service and write to L0."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from .scoped_memory import MemoryBackend, MemoryEntry, RequestedBy

MemoryOpKind = Literal["ADD", "UPDATE", "INVALIDATE", "PROMOTE", "LINK", "NOOP"]


@dataclass(frozen=True)
class MemoryOperation:
    op: MemoryOpKind
    reason: str
    source_turn_ids: list[str] = field(default_factory=list)
    target_id: str | None = None
    content: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    importance: int | None = None
    maxim_key: str | None = None
    links: list[str] | None = None
    requested_by: RequestedBy = "consolidator"


@dataclass(frozen=True)
class AuditRecord:
    op: MemoryOperation
    result: Literal["applied", "dry_run", "rejected", "noop"]
    entry_id: str | None
    at: str
    error: str | None = None


@dataclass(frozen=True)
class RecallEvent:
    at: str
    chat_id: str
    turn_id: str
    entry_id: str
    source: Literal["auto_recall", "recall_tool"]
    query: str | None = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/test_memory_operation_service.py::TestDataContracts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/operation_service.py tests/test_memory_operation_service.py
git commit -m "feat(memory): MemoryOperation/AuditRecord/RecallEvent contracts (L1)"
```

### Task 2.3: `DefaultMemoryOperationService.apply()` with validation, dry-run, audit

**Files:**
- Modify: `src/bos/plugins/memory/operation_service.py`
- Test: `tests/test_memory_operation_service.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_operation_service.py`:

```python
from bos.plugins.memory.operation_service import (
    DefaultMemoryOperationService,
    MemoryOperation,
)


def _svc(tmp_path, backend, maxim_keys=("user", "soul", "identity", "rules")):
    return DefaultMemoryOperationService(
        backend, audit_path=tmp_path / "audit.jsonl", maxim_keys=set(maxim_keys),
    )


class TestApply:
    @pytest.mark.asyncio
    async def test_add_creates_entry_and_audits(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([
            MemoryOperation(op="ADD", reason="durable pref", content="likes dark mode", importance=6)
        ])
        assert recs[0].result == "applied"
        eid = recs[0].entry_id
        assert (await b.get_memory(eid)).content == "likes dark mode"
        audit = await svc.audit()
        assert audit[0].op.reason == "durable pref"

    @pytest.mark.asyncio
    async def test_dry_run_mutates_nothing(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply(
            [MemoryOperation(op="ADD", reason="x", content="should not persist")],
            dry_run=True,
        )
        assert recs[0].result == "dry_run"
        assert await b.search_memories("persist") == []
        # dry-run is still audited
        assert (await svc.audit())[0].result == "dry_run"

    @pytest.mark.asyncio
    async def test_invalidate_records_requested_by(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("stale fact")
        svc = _svc(tmp_path, b)
        recs = await svc.apply(
            [MemoryOperation(op="INVALIDATE", reason="user said stop", target_id=eid, requested_by="user")]
        )
        assert recs[0].result == "applied"
        assert await b.get_memory(eid) is None
        assert (await b.get_memory(eid, include_invalid=True)).metadata["invalidated_by"] == "user"

    @pytest.mark.asyncio
    async def test_update_missing_target_is_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(op="UPDATE", reason="x", target_id="ghost", content="y")])
        assert recs[0].result == "rejected"
        assert "ghost" in recs[0].error

    @pytest.mark.asyncio
    async def test_add_without_content_is_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(op="ADD", reason="x")])
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_promote_bad_maxim_key_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a durable pattern")
        svc = _svc(tmp_path, b)
        recs = await svc.apply(
            [MemoryOperation(op="PROMOTE", reason="x", target_id=eid, maxim_key="bogus")]
        )
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_noop_is_recorded(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(op="NOOP", reason="considered, declined")])
        assert recs[0].result == "noop"


class TestServiceHelpers:
    @pytest.mark.asyncio
    async def test_search_candidates(self, tmp_path):
        b = InMemMemoryExtension()
        await b.ingest_memory("alpha plan")
        svc = _svc(tmp_path, b)
        hits = await svc.search_candidates("alpha", top_k=5)
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_touch_last_used(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a fact")
        svc = _svc(tmp_path, b)
        await svc.touch_last_used([eid])
        assert (await b.get_memory(eid)).metadata["last_used"] is not None

    @pytest.mark.asyncio
    async def test_restore(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a fact")
        await b.invalidate_memory(eid, requested_by="consolidator")
        svc = _svc(tmp_path, b)
        await svc.restore(eid)
        assert await b.get_memory(eid) is not None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_memory_operation_service.py::TestApply tests/test_memory_operation_service.py::TestServiceHelpers`
Expected: FAIL — `DefaultMemoryOperationService` missing.

- [ ] **Step 3: Write the service (append to `operation_service.py`)**

```python
class MemoryOperationService(Protocol):
    async def apply(self, ops: list[MemoryOperation], *, dry_run: bool = False) -> list[AuditRecord]: ...
    async def search_candidates(self, query: str, *, top_k: int) -> list[MemoryEntry]: ...
    async def touch_last_used(self, entry_ids: list[str]) -> None: ...
    async def restore(self, entry_id: str) -> None: ...
    async def audit(self, *, filter: dict | None = None) -> list[AuditRecord]: ...


class DefaultMemoryOperationService:
    """Validates each op, applies it via the L0 backend, appends an AuditRecord.
    Applies are serialized (one scope per service instance) so writes never
    interleave; dry-run validates and audits but mutates nothing."""

    def __init__(self, backend: MemoryBackend, *, audit_path=None, maxim_keys: set[str] | None = None) -> None:
        from ._audit_log import JsonlLog

        self._backend = backend
        self._maxim_keys = maxim_keys or set()
        self._lock = asyncio.Lock()
        self._log = JsonlLog(audit_path) if audit_path is not None else None
        self._mem_audit: list[AuditRecord] = []

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    def _validate(self, op: MemoryOperation) -> str | None:
        """Return an error string if invalid, else None."""
        if op.op in ("ADD",) and not op.content:
            return "ADD requires content"
        if op.op in ("UPDATE", "INVALIDATE", "PROMOTE", "LINK") and not op.target_id:
            return f"{op.op} requires target_id"
        if op.op == "UPDATE" and op.content is None and op.tags is None and op.importance is None:
            return "UPDATE requires at least one of content/tags/importance"
        if op.op == "PROMOTE":
            if not op.maxim_key:
                return "PROMOTE requires maxim_key"
            if op.maxim_key not in self._maxim_keys:
                return f"PROMOTE maxim_key {op.maxim_key!r} not in allowed set"
        if op.op == "LINK" and not op.links:
            return "LINK requires links"
        return None

    async def _record(self, op, result, entry_id, error=None) -> AuditRecord:
        rec = AuditRecord(op=op, result=result, entry_id=entry_id, at=self._now(), error=error)
        self._mem_audit.append(rec)
        if self._log is not None:
            row = {**asdict(op), "_result": result, "_entry_id": entry_id, "_at": rec.at, "_error": error}
            await self._log.append(row)
        return rec

    async def _apply_one(self, op: MemoryOperation, *, dry_run: bool) -> AuditRecord:
        err = self._validate(op)
        if err is not None:
            return await self._record(op, "rejected", None, error=err)
        if op.op == "NOOP":
            return await self._record(op, "noop", None)
        # target existence check for ops that need it
        if op.op in ("UPDATE", "INVALIDATE", "PROMOTE", "LINK"):
            if await self._backend.get_memory(op.target_id, include_invalid=True) is None:
                return await self._record(op, "rejected", op.target_id, error=f"target {op.target_id} not found")
        if dry_run:
            return await self._record(op, "dry_run", op.target_id)
        entry_id = op.target_id
        if op.op == "ADD":
            entry_id = await self._backend.ingest_memory(
                op.content, tags=op.tags, importance=op.importance or 5,
                summary=op.summary, source_turn_ids=op.source_turn_ids,
            )
        elif op.op == "UPDATE":
            await self._backend.update_memory(
                op.target_id, content=op.content, tags=op.tags,
                importance=op.importance, summary=op.summary, links=op.links,
            )
        elif op.op == "INVALIDATE":
            await self._backend.invalidate_memory(op.target_id, requested_by=op.requested_by)
        elif op.op == "LINK":
            existing = await self._backend.get_memory(op.target_id, include_invalid=True)
            merged = list({*(existing.metadata.get("links") or []), *op.links})
            await self._backend.update_memory(op.target_id, links=merged)
        elif op.op == "PROMOTE":
            entry = await self._backend.get_memory(op.target_id, include_invalid=True)
            gist = (op.content or entry.content).strip()
            current = await self._backend.get_maxim(op.maxim_key)
            ts = self._now()[:16].replace("T", " ")
            revised = f"{current}\n[{ts}] {gist}" if current else f"[{ts}] {gist}"
            await self._backend.set_maxim(op.maxim_key, revised)
        return await self._record(op, "applied", entry_id)

    async def apply(self, ops: list[MemoryOperation], *, dry_run: bool = False) -> list[AuditRecord]:
        async with self._lock:
            return [await self._apply_one(op, dry_run=dry_run) for op in ops]

    async def search_candidates(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        return await self._backend.search_memories(query, top_k=top_k)

    async def touch_last_used(self, entry_ids: list[str]) -> None:
        now = self._now()
        for eid in entry_ids:
            await self._backend.update_memory(eid, last_used=now)

    async def restore(self, entry_id: str) -> None:
        await self._backend.restore_memory(entry_id)

    async def audit(self, *, filter: dict | None = None) -> list[AuditRecord]:
        records = list(self._mem_audit)
        if filter:
            for key, val in filter.items():
                records = [r for r in records if getattr(r.op, key, None) == val or getattr(r, key, None) == val]
        return records
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest -q tests/test_memory_operation_service.py`
Expected: PASS (all classes).

- [ ] **Step 5: Export the service**

In `src/bos/plugins/memory/__init__.py`, export `MemoryOperation`, `AuditRecord`, `MemoryOperationService`, `DefaultMemoryOperationService` from `operation_service`. Verify:

Run: `uv run python -c "from bos.plugins.memory import DefaultMemoryOperationService; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/bos/plugins/memory/operation_service.py src/bos/plugins/memory/__init__.py tests/test_memory_operation_service.py
git commit -m "feat(memory): L1 operation service (apply/dry-run/audit/restore/touch)"
```

---

## Phase 3 — L6 Capture + Read Path

Depends on L0 (and uses L1 types). Reworks `MemoryAgentPlugin`: remove `Forget`, add the in-context index, memoize the maxim+index section per turn, and add the auto-recall interceptor.

### Task 3.1: Remove the `Forget` tool

**Files:**
- Modify: `src/bos/plugins/memory/plugin.py`
- Test: `tests/test_memory_extension.py`

- [ ] **Step 1: Write the failing test (and delete the obsolete Forget tests)**

In `tests/test_memory_extension.py`, delete the entire `class TestForgetTool` (its two methods call the removed tool). Add:

```python
class TestForgetRemoved:
    @pytest.mark.asyncio
    async def test_forget_tool_not_registered(self):
        agent = _create_memory_agent()
        with pytest.raises(Exception):
            await agent._invoke_tool("Forget", entry_id="x")
```

- [ ] **Step 2: Run to verify the new test fails**

Run: `uv run pytest -q tests/test_memory_extension.py::TestForgetRemoved`
Expected: FAIL — `Forget` is still registered, so the call succeeds instead of raising.

- [ ] **Step 3: Remove `Forget` from `plugin.py`**

In `src/bos/plugins/memory/plugin.py`:
- Delete the `"Forget": """..."""` entry from `_MEMORY_TOOL_USAGE` (lines ~62–70).
- Delete the entire `@registry(name="Forget", ...)` block and its `async def forget(...)` (lines ~253–285).
- In `_MEMORY_PROMPT_SECTION`, replace the `Forget` bullet line:
  ```
  - Use Forget when the user asks you to forget something or when a memory is clearly stale or superseded.
  ```
  with:
  ```
  - To stop using something, Remember it as a negation (e.g. "X is no longer true"); curation removes it off-turn — there is no destructive delete.
  ```
- In `_MEMORY_TOOL_USAGE["Remember"]`, replace the bullet `- When a memory is clearly stale or superseded, use Forget to remove it.` with `- To retract a fact, Remember a negation; off-turn curation invalidates the stale entry.`

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/test_memory_extension.py`
Expected: PASS — `TestForgetRemoved` passes; `TestRememberTool`/`TestRecallTool`/`TestReviseMaximTool`/`TestSystemPromptIntegration` still pass.

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/plugin.py tests/test_memory_extension.py
git commit -m "feat(memory): remove destructive Forget tool (capture is append-only)"
```

### Task 3.2: Per-turn memoized maxim + in-context index section

**Files:**
- Modify: `src/bos/plugins/memory/plugin.py`
- Test: `tests/test_memory_extension.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_extension.py`:

```python
class TestInContextIndex:
    @pytest.mark.asyncio
    async def test_index_rendered_in_prompt(self):
        store = InMemMemoryExtension()
        await store.ingest_memory("deploys happen on Fridays", tags=["ops"], summary="Friday deploys")
        agent = _create_memory_agent(memory=store, maxim_keys={"user"})
        prompt = await agent._build_system_prompt()
        assert "<memory_index>" in prompt
        assert "Friday deploys" in prompt

    @pytest.mark.asyncio
    async def test_index_capped_by_index_max(self):
        store = InMemMemoryExtension()
        for i in range(5):
            await store.ingest_memory(f"fact {i}", importance=i + 1)
        plugin = MemoryAgentPlugin(store, {"user"}, index_max=2)
        agent = create_test_agent(plugins=[plugin])
        prompt = await agent._build_system_prompt()
        # only the 2 highest-importance summaries appear
        assert prompt.count("<index_entry") == 2


class TestPerTurnMemoization:
    @pytest.mark.asyncio
    async def test_section_byte_identical_within_a_turn(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "v1")
        plugin = MemoryAgentPlugin(store, {"user"})

        class _Ctx:
            turn_id = "turn-A"

        ctx = _Ctx()
        first = await plugin.get_system_prompt_section(ctx)
        await store.set_maxim("user", "v2-changed-mid-turn")  # backend mutates mid-turn
        second = await plugin.get_system_prompt_section(ctx)
        assert first == second  # cached: no mid-turn cache bust
        assert "v2-changed-mid-turn" not in second

    @pytest.mark.asyncio
    async def test_new_turn_reflects_backend_change(self):
        store = InMemMemoryExtension()
        await store.set_maxim("user", "v1")
        plugin = MemoryAgentPlugin(store, {"user"})

        class _Ctx:
            def __init__(self, tid):
                self.turn_id = tid

        first = await plugin.get_system_prompt_section(_Ctx("turn-A"))
        await store.set_maxim("user", "v2")
        second = await plugin.get_system_prompt_section(_Ctx("turn-B"))
        assert "v1" in first and "v2" in second
```

(Add `create_test_agent` to the imports at the top of the file if not already present: `from conftest import InMemMemoryExtension, create_test_agent`.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_memory_extension.py::TestInContextIndex tests/test_memory_extension.py::TestPerTurnMemoization`
Expected: FAIL — no `<memory_index>`, `MemoryAgentPlugin` has no `index_max` kwarg, no memoization.

- [ ] **Step 3: Update `MemoryAgentPlugin.__init__` and the prompt section**

In `src/bos/plugins/memory/plugin.py`, change the constructor and `get_system_prompt_section`:

```python
class MemoryAgentPlugin:
    def __init__(
        self, backend: MemoryBackend, maxim_keys: set[str], *,
        index_in_prompt: bool = True, index_max: int = 50,
        auto_recall: bool = True, top_k: int = 5,
    ) -> None:
        self._backend = backend
        self._maxim_keys = maxim_keys
        self._index_in_prompt = index_in_prompt
        self._index_max = index_max
        self._auto_recall = auto_recall
        self._top_k = top_k
        self._cached_turn_id: str | None = None
        self._cached_section: str | None = None
```

Replace `get_system_prompt_section` with:

```python
    async def _render_section(self) -> str:
        sections = [_MEMORY_PROMPT_SECTION]
        if self._index_in_prompt:
            index = await self._backend.list_index()
            if index:
                items = "\n".join(
                    f'<index_entry id="{_xml_attr(ie.id)}" tags="{_xml_attr(",".join(ie.tags))}">'
                    f"{escape(ie.summary).strip()}</index_entry>"
                    for ie in index[: self._index_max]
                )
                sections.append(f"<memory_index>\n{items}\n</memory_index>")
        if self._maxim_keys:
            items = []
            for key in sorted(self._maxim_keys):
                content = await self._backend.get_maxim(key)
                scope = _MAXIM_DESCRIPTIONS.get(key, "")
                items.append(
                    f'<maxim name="{_xml_attr(key)}" scope="{_xml_attr(scope)}">\n'
                    f"{escape(content).strip()}\n</maxim>"
                )
            sections.append("<active_maxims>\n" + "\n".join(items) + "\n</active_maxims>")
        return "\n\n".join(sections)

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:
        turn_id = getattr(context, "turn_id", None)
        if turn_id is not None and turn_id == self._cached_turn_id:
            return self._cached_section
        section = await self._render_section()
        if turn_id is not None:
            self._cached_turn_id = turn_id
            self._cached_section = section
        return section
```

> The index + maxims are read from the backend at most once per `turn_id` (BEP 10 §5/§12). When `context` has no `turn_id` (e.g. a direct `_build_system_prompt()` call in a test), memoization is skipped and the section is rebuilt — correctness preserved, caching simply not exercised.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest -q tests/test_memory_extension.py::TestInContextIndex tests/test_memory_extension.py::TestPerTurnMemoization tests/test_memory_extension.py::TestSystemPromptIntegration`
Expected: PASS. (The existing `test_empty_maxims_are_not_injected_into_prompt` etc. still pass — `<active_maxims>` and `<memory_workflow>` remain.)

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/plugin.py tests/test_memory_extension.py
git commit -m "feat(memory): in-context index + per-turn memoized maxim section (L6, cache discipline)"
```

### Task 3.3: Auto-recall interceptor

**Files:**
- Create: `src/bos/plugins/memory/auto_recall.py`
- Modify: `src/bos/plugins/memory/plugin.py` (`get_interceptors`)
- Test: `tests/test_memory_extension.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_extension.py`:

```python
class TestAutoRecall:
    @pytest.mark.asyncio
    async def test_interceptor_injects_ephemeral_hits(self):
        from bos.plugins.memory.auto_recall import AutoRecallInterceptor

        store = InMemMemoryExtension()
        eid = await store.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
        interceptor = AutoRecallInterceptor(store, top_k=5)

        class _Ctx:
            turn_id = "t1"
            chat_id = "c1"
            current = [{"role": "user", "content": "what database do I like? postgresql?"}]

            def __init__(self):
                self.metadata = {}
                self.ephemeral_set = {}

            def set_ephemeral_message(self, key, msg):
                self.ephemeral_set[key] = msg

        ctx = _Ctx()
        await interceptor.intercept("prepare", ctx)
        assert "memory_auto_recall" in ctx.ephemeral_set
        assert "PostgreSQL" in str(ctx.ephemeral_set["memory_auto_recall"])
        assert eid in ctx.metadata["recalled"]

    @pytest.mark.asyncio
    async def test_interceptor_only_runs_on_prepare(self):
        from bos.plugins.memory.auto_recall import AutoRecallInterceptor

        store = InMemMemoryExtension()
        await store.ingest_memory("postgresql fact")
        interceptor = AutoRecallInterceptor(store, top_k=5)

        class _Ctx:
            turn_id = "t1"
            chat_id = "c1"
            current = [{"role": "user", "content": "postgresql"}]

            def __init__(self):
                self.metadata = {}
                self.ephemeral_set = {}

            def set_ephemeral_message(self, key, msg):
                self.ephemeral_set[key] = msg

        ctx = _Ctx()
        await interceptor.intercept("before_llm", ctx)  # not "prepare"
        assert ctx.ephemeral_set == {}

    @pytest.mark.asyncio
    async def test_plugin_registers_interceptor_when_enabled(self):
        store = InMemMemoryExtension()
        plugin = MemoryAgentPlugin(store, {"user"}, auto_recall=True)
        assert len(plugin.get_interceptors()) == 1
        plugin_off = MemoryAgentPlugin(store, {"user"}, auto_recall=False)
        assert plugin_off.get_interceptors() == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_memory_extension.py::TestAutoRecall`
Expected: FAIL — `bos.plugins.memory.auto_recall` missing; `get_interceptors` returns `[]`.

- [ ] **Step 3: Write `auto_recall.py`**

```python
"""Auto-recall turn interceptor (BEP 10 §3) — retrieves on the incoming message
and injects top hits as ephemeral context after the cache breakpoint. Records
surfaced entry-ids on TurnContext.metadata['recalled'] for the off-turn recall
log (the off-turn flush itself is BEP-11-gated)."""

from __future__ import annotations

from xml.sax.saxutils import escape

from .scoped_memory import MemoryBackend

_EPHEMERAL_KEY = "memory_auto_recall"


def _incoming_text(context) -> str:
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

    async def intercept(self, stage, context) -> None:
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
        recalled = context.metadata.setdefault("recalled", [])
        recalled.extend(h.id for h in hits)
```

> `top_k` is passed positionally in tests via the keyword in `__init__`; the test constructs `AutoRecallInterceptor(store, top_k=5)`. Keep `top_k` keyword-only as written.

Fix the test constructor mismatch: the interceptor takes `top_k` keyword-only, and the tests call `AutoRecallInterceptor(store, top_k=5)` — consistent. Good.

- [ ] **Step 4: Wire `get_interceptors` in `plugin.py`**

Replace `get_interceptors` in `MemoryAgentPlugin`:

```python
    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        if not self._auto_recall:
            return []
        from .auto_recall import AutoRecallInterceptor

        return [AutoRecallInterceptor(self._backend, top_k=self._top_k)]
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest -q tests/test_memory_extension.py::TestAutoRecall`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/bos/plugins/memory/auto_recall.py src/bos/plugins/memory/plugin.py tests/test_memory_extension.py
git commit -m "feat(memory): auto-recall interceptor injects ephemeral hits (L6)"
```

### Task 3.4: Wire retrieval config through `MemoryHarnessPlugin.bind`

**Files:**
- Modify: `src/bos/plugins/memory/plugin.py` (`default_config`, `bind`)
- Test: `tests/test_memory_extension.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_memory_extension.py`:

```python
class TestHarnessConfig:
    def test_default_config_has_retrieval_block(self):
        from bos.plugins.memory.plugin import MemoryHarnessPlugin

        cfg = MemoryHarnessPlugin().default_config()
        assert cfg["retrieval"]["index_max"] == 50
        assert cfg["retrieval"]["auto_recall"] is True
        assert cfg["retrieval"]["top_k"] == 5

    @pytest.mark.asyncio
    async def test_bind_passes_retrieval_into_agent_plugin(self, tmp_path):
        from bos.core.contract import PluginServices
        from bos.plugins.memory.plugin import MemoryHarnessPlugin

        h = MemoryHarnessPlugin()
        await h.setup(PluginServices(
            bos_dir=tmp_path, workspace=tmp_path, llm=None, consolidator=None, subagents=None,
        ))
        agent_plugin = h.bind({
            "maxims": ["user"], "scope": "workspace", "backend": "in_memory",
            "retrieval": {"index_max": 7, "auto_recall": False, "top_k": 3, "index_in_prompt": True},
        })
        assert agent_plugin._index_max == 7
        assert agent_plugin._auto_recall is False
        assert agent_plugin._top_k == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_memory_extension.py::TestHarnessConfig`
Expected: FAIL — `default_config` has no `retrieval`; `bind` does not pass retrieval kwargs.

- [ ] **Step 3: Update `default_config` and `bind`**

In `MemoryHarnessPlugin.default_config`:

```python
    def default_config(self) -> Mapping[str, Any]:
        return {
            "maxims": ["user", "soul", "identity", "rules"],
            "scope": "workspace",
            "backend": "_default",
            "retrieval": {"auto_recall": True, "index_in_prompt": True, "index_max": 50, "top_k": 5},
        }
```

At the end of `bind`, replace the `return MemoryAgentPlugin(backend, maxim_keys)` with:

```python
        maxim_keys = set(config.get("maxims", []))
        retrieval = dict(config.get("retrieval", {}))
        return MemoryAgentPlugin(
            backend, maxim_keys,
            index_in_prompt=retrieval.get("index_in_prompt", True),
            index_max=retrieval.get("index_max", 50),
            auto_recall=retrieval.get("auto_recall", True),
            top_k=retrieval.get("top_k", 5),
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/test_memory_extension.py::TestHarnessConfig`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/plugin.py tests/test_memory_extension.py
git commit -m "feat(memory): wire [retrieval] config (auto_recall/index_max/top_k) through bind (L6)"
```

---

## Final Verification

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: all green. Investigate any failure referencing `Forget`, `optimize`, `forget_memory`, or `metadata is None` — those are direct fallout of this plan and must be fixed before claiming done.

- [ ] **Step 2: Lint**

Run: `uv run ruff check src tests`
Expected: no *new* findings in the files this plan touched (pre-existing findings elsewhere are acceptable per CLAUDE.md).

- [ ] **Step 3: Re-check the eval still runs**

Run: `uv run pytest -q tests/test_memory_eval.py`
Expected: PASS — the ranked search now backs recall@k.

- [ ] **Step 4: Smoke the CLI memory path is intact**

Run: `uv run boscli --help`
Expected: exits 0 (confirms imports across the touched modules resolve).

---

## Deferred / Blocked (do NOT attempt in this plan)

These BEP 10 phases are gated on BEP 11 infra that does not exist (`JobRunner`, `LifecycleBus`, `BackgroundLLM`; `PluginServices` exposes only `llm`/`consolidator`/`subagents`/`chat_store`):

- **P4 L3 lifecycle** — `turn_complete`/`session_close` events carrying `base_revision`. Unblocks the recall-log off-turn flush (the subscriber that calls `operation_service.touch_last_used`).
- **P5 L2 `BackgroundLLM`** — provider-level structured-output call.
- **P6 L4 `JobRunner`** — durable jobs, idempotency `(scope, chat_id, actor_name, base_revision, trigger)`, retry, `drain()`.
- **P7 L5 consolidation handler** — `MemoryConsolidator.propose()` → `MemoryOperation[]`, the `MemoryConsolidationRequest` dataclass, route/resolve/promote/compact; applied via the P2 operation service. (Operation service `apply()` is ready and waiting.)
- **P8 L7 admin** — `boscli memory consolidate/jobs`. The read-only/L1-only subset (`list`/`show`/`index`/`recall`/`restore`/`audit`) is buildable now against L0+L1 if desired as a follow-up, but is not in this plan's scope.
- **P9 ranking + increments** — full scored ranking with the `last_used` recency term (eval-gated against the §8 eval), then promote → reflect → link.
- **P10 DSPy/GEPA** — offline prompt compilation.

When BEP 11 graduates its contracts into code, write a follow-up plan for P4–P9 that consumes them.

---

## Self-Review (performed against BEP 10 §11 phases 0–3)

- **P0 coverage:** regression tests (Task 0.1) + component-eval skeleton in stub mode (Task 0.2). ✓ (Routing eval explicitly deferred — its target, the handler, is P7/blocked.)
- **P1 coverage:** frontmatter + metadata incl. `source_turn_ids` (1.3), soft delete + default filtering (1.3/1.4), `list_index` (1.3/1.4), ranked search (1.3/1.4), clean-remove `optimize()`/`forget_memory` (1.2 protocol, 1.3/1.4 backends, 1.5 assertion). ✓
- **P2 coverage:** `apply(ops, dry_run)` + audit + `requested_by` (2.3), `touch_last_used` (2.3), `search_candidates` (2.3), `restore`/`audit` (2.3), JSONL audit log (2.1), op contracts (2.2). ✓
- **P3 coverage:** index in cached prefix (3.2), auto-recall interceptor (3.3), per-turn maxim rebuild — memoized, no mid-turn re-read (3.2), keep `Remember`/`ReviseMaxim` (untouched), remove `Forget` (3.1), config wiring (3.4). ✓
- **Type consistency:** `MemoryEntry.metadata: dict` (default_factory) used uniformly; `RequestedBy` literal shared by protocol + service; `MemoryIndexEntry(id, tags, summary)` consistent across backend, ScopedMemory, plugin render; `MemoryOperation` field names match service `_apply_one` reads; `update_memory(..., last_used=)` signature matches `touch_last_used` caller. ✓
- **Placeholder scan:** none — every code step shows full code; no "TBD"/"add error handling"/"similar to". ✓
- **Acceptance (BEP §12) reachable now:** append+read surface with no `Forget` (3.1), within-turn byte-identical maxim/index block (3.2), soft delete reversible + retention purge (1.3/1.4), component eval reports recall@k (0.2). The off-turn-curation acceptance items (§12 bullets on curation provenance, "stop using X" INVALIDATE) require P7 and are correctly out of scope.
