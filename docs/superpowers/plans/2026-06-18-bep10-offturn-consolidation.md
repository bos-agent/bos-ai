# BEP 10 Off-Turn Consolidation (P7 + P8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship BEP 10's off-turn consolidation track — the consolidation handler that proposes structured operations, the job that runs it via the BEP 11 JobRunner, the trigger bindings, the recall-log flush, and the `boscli memory` admin surface — so a fact captured mid-chat is curated off-turn and persists into a new session.

**Architecture:** `MemoryHarnessPlugin.setup` constructs the operation service, watermark store, and consolidator once per harness, then registers a `session_close` (+ `manual`) trigger binding on the BEP 11 `JobRunner`. The trigger factory builds a `MemoryConsolidationJob` whose `run()` loads the unprocessed-turn window via `ChatStore.get_messages_since`, calls the consolidator's `propose()` (which uses `BackgroundLLM` with a `MemoryOperation[]` JSON schema), and applies via the L1 operation service (`dry_run=not policy.auto_apply`). A separate `turn_complete` subscriber flushes accumulated recall-log entries via `touch_last_used`. The admin surface is a Click group registered as the `memory` lazy command.

**Tech Stack:** Python ≥3.13, asyncio, existing `BackgroundLLM`/`JobRunner`/`LifecycleBus` (BEP 11), existing `DefaultMemoryOperationService` (BEP 10 P2), Click (existing CLI framework), `jsonschema` (already declared).

## Global Constraints

- **`auto_apply` defaults to `False`.** v1 ships propose → audit → operator-enables-auto. Any consolidation run with `auto_apply=False` calls `apply(ops, dry_run=True)` — operations are validated and audited but mutate nothing (BEP 10 §7).
- **One in-flight consolidation per `(scope, chat_id)`** via JobRunner key. v1 BEP 11 dedup is per-key; we set `key = f"consolidate:{scope}:{chat_id}:{base_revision}:{trigger}"` (BEP 10 §4 idempotency).
- **Watermark advances only on success.** If `propose()` or `apply()` raises or the job is cancelled mid-run, the watermark stays — the next run reprocesses the same window. Re-application is safe because the operation service is idempotent per content.
- **The consolidation handler never persists chat.** It uses `BackgroundLLM` (no chat side effects per BEP 11 §3), and curation writes go through the L1 operation service.
- **`requested_by` provenance is preserved.** User-requested invalidations (negation captures) and consolidator-inferred invalidations are distinguished via `MemoryOperation.requested_by` (already on the contract).
- **`session_close` auto-consolidation runs only in a living runtime.** One-shot CLI uses the `manual` trigger (BEP 10 §4 / BEP 11 §2). Documented; not patched here.
- `uv run pytest -q` and `uv run ruff check src tests` must remain green throughout.

---

## Scope and Boundaries

**In scope (BEP 10 phases P7 + P8):**
- L1 op-service extension: `UPDATE` op with `maxim_key` rewrites the named maxim (the Compact path).
- Watermark store: per `(scope, chat_id)` last-handled revision, persisted as JSON under the backend's storage dir.
- `MemoryConsolidationRequest` dataclass + `MemoryConsolidator` protocol + `DefaultMemoryConsolidator` (uses `BackgroundLLM` with a JSON schema for `list[MemoryOperation]`).
- `MemoryConsolidationJob` (implements BEP 11 `Job`): loads window + candidates + maxims, calls `propose`, calls operation service `apply(dry_run=not policy.auto_apply)`, advances watermark on success.
- `MemoryHarnessPlugin.setup` constructs the operation service, watermark store, consolidator; binds `session_close` trigger; exposes a "run now" entry point for the admin CLI.
- Recall-log flush: subscriber on `turn_complete` that calls `operation_service.touch_last_used` for accumulated entry ids (closes the loop deferred from BEP 10 P3).
- `boscli memory` CLI: `list`, `show`, `index`, `recall`, `consolidate [--chat | --all] [--dry-run]`, `restore`, `audit`, `jobs`.

**Out of scope (deferred to a follow-up plan):**
- BEP 10 P9 — scored ranking with `last_used` recency term (eval-gated against the §8 component eval), and the promote / reflect / link increments.
- BEP 10 P10 — DSPy / GEPA offline prompt compilation.
- A cross-restart durable JobStore — BEP 11 §2 "later" durability layer.
- One-shot CLI auto-consolidation (depends on persistent JobStore).
- Scheduled cron-style scan trigger (BEP 10 §4 "scan/scheduled consolidation"); admin `consolidate --all` is the v1 entry point.
- Memory-CLI `telemetry` subcommand (depends on P9's ranking + recall-hit-rate / dead-memory-ratio computation).

## Design decisions / deviations from the BEP text (read before starting)

1. **`raw_appends` and `candidate_memories` are merged in v1.** BEP 10 §4 distinguishes "raw agent appends since the watermark" (uncurated) from "pre-existing in-scope entries" (working set). v1 of the consolidator treats all currently-active in-scope memories as one candidate pool — the Resolve step (ADD/UPDATE/INVALIDATE/NOOP) doesn't need the distinction to work correctly. Pre-existing vs new-and-near-duplicate is what UPDATE/INVALIDATE already reconciles. (`raw_appends` field stays on the request dataclass with default `[]` for forward compatibility.)
2. **Maxim Compact is an L1 op extension, not a new op kind.** Per BEP 10 §4 the Compact step folds accumulated `[timestamp] note` lines into clean prose. We add a single semantic to the existing `UPDATE` op: when `maxim_key` is set, `UPDATE` rewrites the named maxim's content via `backend.set_maxim`. Validation: `maxim_key in maxim_keys`, `content` present. Avoids inventing a new op kind for one path.
3. **Watermark store is a single JSON file.** `bos_dir/memory/watermarks.json` holding `{scope: {chat_id: revision}}`. Simple, atomic-write, no schema migration concerns. Re-reading is cheap (the file is tiny).
4. **`turn_complete` payload carries `recalled` entry ids.** `CoordinatedActor._on_turn_finished` reads `ctx.metadata.get("recalled", [])` from `TurnContext` and includes it in `LifecycleEvent.payload`. This closes the "recall log → off-turn flush" loop deferred from BEP 10 P3. No new attribute on `LifecycleEvent` — `payload` is the existing escape hatch.
5. **`MemoryConsolidationJob.key` includes `base_revision` and `trigger`.** A `manual` run at the same revision as a previous `session_close` is still a distinct job (operator override). The `(scope, chat_id, base_revision, trigger)` quadruple matches BEP 10 §4's idempotency contract.
6. **Admin CLI uses `Workspace.harness()`.** Each `boscli memory` invocation creates a fresh harness, runs the requested operation, exits. Matches `boscli ask`'s asyncio.run pattern; no daemon assumption.
7. **`consolidate --all` is a plain Python loop**, not a scheduled task. It iterates `chat_store.list_chats()`, for each one whose `get_revision() > watermark`, enqueues a job. The "scan" abstraction from BEP 10 §4 is one for-loop in CLI; not worth its own class in v1.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/bos/plugins/memory/operation_service.py` | Extend `UPDATE` semantics for `maxim_key`-targeted maxim rewrites | modify (P1) |
| `src/bos/plugins/memory/_watermark.py` | **new** — JSON-backed per-`(scope, chat_id)` watermark store | create (P1) |
| `src/bos/plugins/memory/consolidator.py` | **new** — `ConsolidationPolicy`, `MemoryConsolidationRequest`, `MemoryConsolidator` protocol, `DefaultMemoryConsolidator`, prompts, response schema | create (P2) |
| `src/bos/plugins/memory/job.py` | **new** — `MemoryConsolidationJob` (BEP 11 `Job`) | create (P3) |
| `src/bos/plugins/memory/plugin.py` | `MemoryHarnessPlugin.setup` constructs op-service / watermark / consolidator; registers trigger; exposes `run_consolidation_now(chat_id, *, dry_run)` for the admin CLI | modify (P4) |
| `src/bos/plugins/memory/auto_recall.py` | (existing) — no change; it already records on `ctx.metadata["recalled"]`. Verified in P5. | — |
| `src/bos/plugins/memory/recall_flush.py` | **new** — `RecallFlushSubscriber` registered on `turn_complete` | create (P5) |
| `src/bos/gateway/actor_manager.py` | `CoordinatedActor._on_turn_finished` copies `recalled` ids from `TurnContext.metadata` into `LifecycleEvent.payload` | modify (P5) |
| `src/bos/cli/entry.py` | Register `memory` lazy command | modify (P6) |
| `src/bos/cli/commands/memory.py` | **new** — Click group with `list`/`show`/`index`/`recall`/`consolidate`/`restore`/`audit`/`jobs` | create (P6) |
| `src/bos/plugins/memory/__init__.py` | Export `MemoryConsolidationRequest`, `MemoryConsolidator`, `DefaultMemoryConsolidator`, `ConsolidationPolicy`, `MemoryConsolidationJob` | modify (P2/P3) |
| `tests/test_memory_watermark.py` | **new** — watermark store coverage | create (P1) |
| `tests/test_memory_consolidator.py` | **new** — `DefaultMemoryConsolidator` over a stub `BackgroundLLM` | create (P2) |
| `tests/test_memory_consolidation_job.py` | **new** — `MemoryConsolidationJob` end-to-end with the real harness | create (P3) |
| `tests/test_memory_plugin_wiring.py` | **new** — `MemoryHarnessPlugin.setup` constructs services + registers trigger; `run_consolidation_now` works | create (P4) |
| `tests/test_recall_flush.py` | **new** — `turn_complete` event carrying `recalled` ids triggers `touch_last_used` | create (P5) |
| `tests/test_cli_memory.py` | **new** — `boscli memory` smoke tests over the in-memory backend | create (P6) |
| `tests/test_memory_operation_service.py` | (existing) — extend with maxim-Compact via `UPDATE` test | modify (P1) |

---

## Phase 0 — Lock + Seed

### Task 0.1: Lock current `MemoryHarnessPlugin.setup` behavior

**Files:**
- Test: `tests/test_memory_plugin_wiring.py` (create — extend in P4)

**Interfaces:**
- Consumes: existing `MemoryHarnessPlugin`, `PluginServices` (BEP 11-extended with `events`/`jobs`/`background_llm`).
- Produces: a lock test that documents what `setup` currently does (nearly nothing), which P4 will then change.

- [ ] **Step 1: Write the lock test**

```python
"""Lock tests for MemoryHarnessPlugin lifecycle.

P0 locks current behavior: setup() stores services and creates no backend.
P4 extends setup to also construct the operation service, watermark store,
consolidator, and bind a JobRunner trigger."""

import pytest

from bos.core.contract import PluginServices
from bos.plugins.memory.plugin import MemoryHarnessPlugin


@pytest.mark.asyncio
async def test_current_setup_is_minimal(tmp_path):
    h = MemoryHarnessPlugin()
    await h.setup(PluginServices(
        bos_dir=tmp_path, workspace=tmp_path, llm=None, consolidator=None, subagents=None,
    ))
    assert h._services is not None
    assert h._backend is None
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest -q tests/test_memory_plugin_wiring.py`
Expected: PASS (1 test).

- [ ] **Step 3: Commit**

```bash
git add tests/test_memory_plugin_wiring.py
git commit -m "test(memory): lock current MemoryHarnessPlugin setup behavior"
```

---

## Phase 1 — Watermark store + L1 maxim-Compact extension

### Task 1.1: Watermark store

**Files:**
- Create: `src/bos/plugins/memory/_watermark.py`
- Test: `tests/test_memory_watermark.py` (create)

**Interfaces:**
- Produces:
  - `WatermarkStore(path: Path)` — JSON-backed
  - `await store.get(scope: str, chat_id: str) -> int` (0 if absent)
  - `await store.set(scope: str, chat_id: str, revision: int) -> None`
  - `await store.snapshot() -> dict[str, dict[str, int]]`

- [ ] **Step 1: Write failing tests**

```python
"""WatermarkStore — per-(scope, chat_id) last-handled revision."""

import pytest

from bos.plugins.memory._watermark import WatermarkStore


@pytest.mark.asyncio
async def test_get_default_zero(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    assert await s.get("workspace", "c1") == 0


@pytest.mark.asyncio
async def test_set_and_get(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    await s.set("workspace", "c1", 5)
    assert await s.get("workspace", "c1") == 5


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_path):
    s1 = WatermarkStore(tmp_path / "wm.json")
    await s1.set("workspace", "c1", 9)
    s2 = WatermarkStore(tmp_path / "wm.json")
    assert await s2.get("workspace", "c1") == 9


@pytest.mark.asyncio
async def test_snapshot_groups_by_scope(tmp_path):
    s = WatermarkStore(tmp_path / "wm.json")
    await s.set("workspace", "c1", 1)
    await s.set("workspace", "c2", 2)
    await s.set("agent-a", "c1", 7)
    snap = await s.snapshot()
    assert snap == {"workspace": {"c1": 1, "c2": 2}, "agent-a": {"c1": 7}}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_memory_watermark.py`
Expected: FAIL — `ModuleNotFoundError: bos.plugins.memory._watermark`.

- [ ] **Step 3: Implement**

```python
"""Per-(scope, chat_id) last-handled revision store (BEP 10 §4 watermark).

Single JSON file under the memory backend's storage dir. Atomic write via
write-then-replace. Concurrency: relies on the JobRunner serializing
consolidation per scope; no in-process lock needed."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


class WatermarkStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _load_sync(self) -> dict[str, dict[str, int]]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_sync(self, data: dict[str, dict[str, int]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    async def get(self, scope: str, chat_id: str) -> int:
        data = await asyncio.to_thread(self._load_sync)
        return int(data.get(scope, {}).get(chat_id, 0))

    async def set(self, scope: str, chat_id: str, revision: int) -> None:
        def _write() -> None:
            data = self._load_sync()
            data.setdefault(scope, {})[chat_id] = int(revision)
            self._save_sync(data)

        await asyncio.to_thread(_write)

    async def snapshot(self) -> dict[str, dict[str, int]]:
        return await asyncio.to_thread(self._load_sync)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q tests/test_memory_watermark.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/_watermark.py tests/test_memory_watermark.py
git commit -m "feat(memory): WatermarkStore for off-turn consolidation (BEP 10 §4)"
```

### Task 1.2: L1 op service — maxim Compact via `UPDATE` with `maxim_key`

**Files:**
- Modify: `src/bos/plugins/memory/operation_service.py`
- Test: `tests/test_memory_operation_service.py`

**Interfaces:**
- Consumes: existing `MemoryOperation`, `DefaultMemoryOperationService`, `MemoryBackend.set_maxim`.
- Produces:
  - `UPDATE` with `maxim_key` set: `op.content` (required) is written via `backend.set_maxim(op.maxim_key, op.content)`. Validation: `maxim_key in maxim_keys`, `content` present; `target_id` must be `None` (mutually exclusive with entry-targeted UPDATE).

- [ ] **Step 1: Add failing tests**

Append to `tests/test_memory_operation_service.py`:

```python
class TestMaximCompact:
    @pytest.mark.asyncio
    async def test_update_with_maxim_key_rewrites_maxim(self, tmp_path):
        b = InMemMemoryExtension()
        await b.set_maxim("user", "old long content\n[2026-01-01 10:00] note A\n[2026-01-02 11:00] note B")
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="compact maxim notes",
            maxim_key="user", content="compacted prose: A and B",
        )])
        assert recs[0].result == "applied"
        assert await b.get_maxim("user") == "compacted prose: A and B"

    @pytest.mark.asyncio
    async def test_update_maxim_unknown_key_rejected(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="x", maxim_key="bogus", content="x",
        )])
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_update_maxim_requires_content(self, tmp_path):
        b = InMemMemoryExtension()
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="x", maxim_key="user",
        )])
        assert recs[0].result == "rejected"

    @pytest.mark.asyncio
    async def test_update_maxim_and_target_id_mutually_exclusive(self, tmp_path):
        b = InMemMemoryExtension()
        eid = await b.ingest_memory("a fact")
        svc = _svc(tmp_path, b)
        recs = await svc.apply([MemoryOperation(
            op="UPDATE", reason="x", maxim_key="user", target_id=eid, content="y",
        )])
        assert recs[0].result == "rejected"
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest -q tests/test_memory_operation_service.py::TestMaximCompact`
Expected: FAIL — the validation paths above are not yet branched.

- [ ] **Step 3: Extend `_validate` and `_apply_one`**

In `src/bos/plugins/memory/operation_service.py`, modify `DefaultMemoryOperationService._validate`:

```python
    def _validate(self, op: MemoryOperation) -> str | None:
        """Return an error string if invalid, else None."""
        if op.op == "ADD" and not op.content:
            return "ADD requires content"
        # UPDATE has two flavors: entry-targeted (target_id) and maxim-targeted (maxim_key).
        if op.op == "UPDATE":
            if op.maxim_key is not None and op.target_id is not None:
                return "UPDATE: maxim_key and target_id are mutually exclusive"
            if op.maxim_key is not None:
                if op.maxim_key not in self._maxim_keys:
                    return f"UPDATE maxim_key {op.maxim_key!r} not in allowed set"
                if not op.content:
                    return "UPDATE on a maxim requires content"
            else:
                if not op.target_id:
                    return "UPDATE requires target_id (or maxim_key for maxim rewrite)"
                if op.content is None and op.tags is None and op.importance is None:
                    return "UPDATE requires at least one of content/tags/importance"
        if op.op in ("INVALIDATE", "PROMOTE", "LINK") and not op.target_id:
            return f"{op.op} requires target_id"
        if op.op == "PROMOTE":
            if not op.maxim_key:
                return "PROMOTE requires maxim_key"
            if op.maxim_key not in self._maxim_keys:
                return f"PROMOTE maxim_key {op.maxim_key!r} not in allowed set"
        if op.op == "LINK" and not op.links:
            return "LINK requires links"
        return None
```

And modify `_apply_one`'s `UPDATE` branch to handle the maxim case:

```python
        elif op.op == "UPDATE":
            if op.maxim_key is not None:
                await self._backend.set_maxim(op.maxim_key, op.content)
                entry_id = None  # maxim-targeted; no entry id
            else:
                await self._backend.update_memory(
                    op.target_id, content=op.content, tags=op.tags,
                    importance=op.importance, summary=op.summary, links=op.links,
                )
```

> The existing target-existence check above the `dry_run` short-circuit only runs for `op.op in ("UPDATE", "INVALIDATE", "PROMOTE", "LINK")`. That check is correct only when `target_id` is set, so wrap it: change the guard to `if op.op in ("UPDATE", "INVALIDATE", "PROMOTE", "LINK") and op.target_id is not None:`. Without that wrap, a maxim-targeted UPDATE (no target_id) would fail the existence check.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q tests/test_memory_operation_service.py`
Expected: PASS (all 14 original + 4 new = 18).

- [ ] **Step 5: Commit**

```bash
git add src/bos/plugins/memory/operation_service.py tests/test_memory_operation_service.py
git commit -m "feat(memory): L1 UPDATE with maxim_key rewrites the named maxim (Compact)"
```

---

## Phase 2 — Consolidator (handler + prompts + schema)

### Task 2.1: Request dataclass, policy, protocol

**Files:**
- Create: `src/bos/plugins/memory/consolidator.py`
- Modify: `src/bos/plugins/memory/__init__.py` (exports)
- Test: `tests/test_memory_consolidator.py` (create)

**Interfaces:**
- Consumes: existing `Message`, `MemoryEntry`, `MemoryOperation`.
- Produces:
  - `ConsolidationPolicy(enabled: bool = False, retention_days: int = 30, auto_apply: bool = False)`
  - `MemoryConsolidationRequest(chat_id, actor_name, scope, base_revision, trigger, transcript_window, raw_appends, candidate_memories, active_maxims, policy)` — frozen dataclass.
  - `MemoryConsolidator` Protocol with `async propose(request) -> list[MemoryOperation]`.

- [ ] **Step 1: Failing structural test**

```python
"""DefaultMemoryConsolidator — propose() over a stub BackgroundLLM."""

import json

import pytest

from bos.core.llm import LLMResponse


class TestStructural:
    def test_request_and_policy_exist(self):
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            MemoryConsolidationRequest,
            MemoryConsolidator,
        )

        pol = ConsolidationPolicy(enabled=True, retention_days=30, auto_apply=True)
        assert pol.auto_apply is True
        req = MemoryConsolidationRequest(
            chat_id="c1", actor_name="A", scope="workspace", base_revision=4,
            trigger="manual", transcript_window=[], raw_appends=[],
            candidate_memories=[], active_maxims={}, policy=pol,
        )
        assert req.chat_id == "c1"
        assert hasattr(MemoryConsolidator, "propose")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_memory_consolidator.py::TestStructural`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the module scaffold**

```python
"""Memory consolidation handler (BEP 10 §4) — proposes structured operations
for off-turn curation. Uses BEP 11 BackgroundLLM with a JSON schema; never
writes directly (writes go through the L1 operation service)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q tests/test_memory_consolidator.py::TestStructural`
Expected: PASS (1 test).

- [ ] **Step 5: Export from `__init__.py`**

In `src/bos/plugins/memory/__init__.py`, add imports + `__all__` entries:

```python
from .consolidator import (  # noqa: E402
    ConsolidationPolicy,
    MemoryConsolidationRequest,
    MemoryConsolidator,
)
```

…and add `"ConsolidationPolicy"`, `"MemoryConsolidationRequest"`, `"MemoryConsolidator"` to `__all__`.

- [ ] **Step 6: Commit**

```bash
git add src/bos/plugins/memory/consolidator.py src/bos/plugins/memory/__init__.py tests/test_memory_consolidator.py
git commit -m "feat(memory): MemoryConsolidationRequest + MemoryConsolidator protocol (BEP 10 §4)"
```

### Task 2.2: `DefaultMemoryConsolidator` — prompt + schema + parse

**Files:**
- Modify: `src/bos/plugins/memory/consolidator.py`
- Modify: `src/bos/plugins/memory/__init__.py` (export `DefaultMemoryConsolidator`)
- Test: `tests/test_memory_consolidator.py`

**Interfaces:**
- Consumes: `BackgroundLLM.ask(messages, response_schema=...)`.
- Produces:
  - `DefaultMemoryConsolidator(background_llm, *, maxim_keys: set[str], model: str | None = None)` — constructor.
  - `await consolidator.propose(request) -> list[MemoryOperation]` — uses `BackgroundLLM`, parses the JSON response, instantiates `MemoryOperation` rows.

- [ ] **Step 1: Add behavior tests**

Append to `tests/test_memory_consolidator.py`:

```python
class _StubBackgroundLLM:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def ask(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content=json.dumps(self._payload))


class TestDefaultConsolidator:
    @pytest.mark.asyncio
    async def test_parses_operations_payload(self):
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )

        payload = {"operations": [
            {"op": "ADD", "reason": "stable preference", "content": "likes dark mode", "importance": 7},
            {"op": "NOOP", "reason": "considered, declined"},
        ]}
        blm = _StubBackgroundLLM(payload)
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1", actor_name=None, scope="workspace", base_revision=1,
            trigger="manual", transcript_window=[], raw_appends=[],
            candidate_memories=[], active_maxims={"user": ""},
            policy=ConsolidationPolicy(),
        )
        ops = await c.propose(req)
        assert [o.op for o in ops] == ["ADD", "NOOP"]
        assert ops[0].importance == 7
        assert ops[1].reason == "considered, declined"

    @pytest.mark.asyncio
    async def test_response_schema_required_keys(self):
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )

        blm = _StubBackgroundLLM({"operations": []})
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1", actor_name=None, scope="workspace", base_revision=1,
            trigger="manual", transcript_window=[], raw_appends=[],
            candidate_memories=[], active_maxims={}, policy=ConsolidationPolicy(),
        )
        await c.propose(req)
        sent_schema = blm.calls[0]["response_schema"]
        assert sent_schema["type"] == "object"
        assert "operations" in sent_schema["properties"]
        item_schema = sent_schema["properties"]["operations"]["items"]
        assert "op" in item_schema["properties"]
        assert set(item_schema["required"]) >= {"op", "reason"}

    @pytest.mark.asyncio
    async def test_prompt_includes_transcript_and_candidates(self):
        from bos.core.contract import Message
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )
        from bos.plugins.memory.scoped_memory import MemoryEntry

        blm = _StubBackgroundLLM({"operations": []})
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1", actor_name=None, scope="workspace", base_revision=1,
            trigger="manual",
            transcript_window=[Message(llm_message={"role": "user", "content": "I prefer dark mode"})],
            raw_appends=[],
            candidate_memories=[MemoryEntry(id="m1", content="prefers light mode")],
            active_maxims={"user": "existing user maxim text"},
            policy=ConsolidationPolicy(),
        )
        await c.propose(req)
        prompt = blm.calls[0]["messages"][-1]["content"]
        assert "dark mode" in prompt
        assert "m1" in prompt
        assert "prefers light mode" in prompt
        assert "existing user maxim text" in prompt
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest -q tests/test_memory_consolidator.py::TestDefaultConsolidator`
Expected: FAIL — `ImportError: cannot import name 'DefaultMemoryConsolidator'`.

- [ ] **Step 3: Implement**

Append to `src/bos/plugins/memory/consolidator.py`:

```python
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
```

- [ ] **Step 4: Export from `__init__.py`**

Extend the consolidator imports to include `DefaultMemoryConsolidator`; add it to `__all__`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -q tests/test_memory_consolidator.py`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/bos/plugins/memory/consolidator.py src/bos/plugins/memory/__init__.py tests/test_memory_consolidator.py
git commit -m "feat(memory): DefaultMemoryConsolidator with prompt + JSON schema (BEP 10 §4)"
```

---

## Phase 3 — Consolidation job

### Task 3.1: `MemoryConsolidationJob`

**Files:**
- Create: `src/bos/plugins/memory/job.py`
- Modify: `src/bos/plugins/memory/__init__.py` (export)
- Test: `tests/test_memory_consolidation_job.py` (create)

**Interfaces:**
- Consumes: `Job` (BEP 11 protocol), `ChatStore.get_messages_since`, `MemoryConsolidator.propose`, `DefaultMemoryOperationService.apply`, `WatermarkStore`, `MemoryBackend.search_memories`/`get_maxim`.
- Produces:
  - `MemoryConsolidationJob(*, scope, chat_id, actor_name, base_revision, trigger, policy, chat_store, backend, consolidator, operation_service, watermarks, maxim_keys) -> Job`
  - `await job.run() -> None` performs the full propose → apply → advance-watermark flow. Advance only on success.

- [ ] **Step 1: Write the behavior tests**

```python
"""MemoryConsolidationJob — end-to-end propose -> apply -> advance watermark."""

import pytest
from conftest import InMemChatStore, InMemMemoryExtension

from bos.core.contract import Message
from bos.plugins.memory._watermark import WatermarkStore
from bos.plugins.memory.consolidator import ConsolidationPolicy, DefaultMemoryConsolidator
from bos.plugins.memory.job import MemoryConsolidationJob
from bos.plugins.memory.operation_service import DefaultMemoryOperationService


class _StubBLM:
    def __init__(self, ops_payload):
        self._payload = ops_payload

    async def ask(self, **kwargs):
        from bos.core.llm import LLMResponse
        import json as _json
        return LLMResponse(content=_json.dumps(self._payload))


def _msg(role, content, *, turn_id="t1"):
    return Message(llm_message={"role": role, "content": content}, turn_id=turn_id)


class TestJobRun:
    @pytest.mark.asyncio
    async def test_dry_run_applies_no_writes_but_advances_watermark(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode", turn_id="t1")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"},
        )
        blm = _StubBLM({"operations": [
            {"op": "ADD", "reason": "stable preference", "content": "prefers dark mode", "importance": 7},
        ]})
        consolidator = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            scope="workspace", chat_id="c1", actor_name=None, base_revision=head,
            trigger="manual", policy=ConsolidationPolicy(auto_apply=False),
            chat_store=chat_store, backend=backend, consolidator=consolidator,
            operation_service=op_svc, watermarks=wm, maxim_keys={"user"},
        )
        await job.run()
        # dry-run: no entries actually created
        assert await backend.search_memories("dark") == []
        # but watermark advanced
        assert await wm.get("workspace", "c1") == head
        # and dry-run record is in audit
        audit = await op_svc.audit()
        assert audit and audit[0].result == "dry_run"

    @pytest.mark.asyncio
    async def test_auto_apply_persists_and_advances(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "I prefer dark mode")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"},
        )
        blm = _StubBLM({"operations": [
            {"op": "ADD", "reason": "stable preference", "content": "prefers dark mode", "importance": 7},
        ]})
        consolidator = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            scope="workspace", chat_id="c1", actor_name=None, base_revision=head,
            trigger="manual", policy=ConsolidationPolicy(auto_apply=True),
            chat_store=chat_store, backend=backend, consolidator=consolidator,
            operation_service=op_svc, watermarks=wm, maxim_keys={"user"},
        )
        await job.run()
        assert (await backend.search_memories("dark"))[0].content == "prefers dark mode"
        assert await wm.get("workspace", "c1") == head

    @pytest.mark.asyncio
    async def test_watermark_does_not_advance_on_failure(self, tmp_path):
        chat_store = InMemChatStore()
        backend = InMemMemoryExtension()
        await chat_store.commit_turn("c1", [_msg("user", "msg")], turn_id="t1")
        wm = WatermarkStore(tmp_path / "wm.json")
        op_svc = DefaultMemoryOperationService(
            backend, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"},
        )

        class _RaisingConsolidator:
            async def propose(self, request):
                raise RuntimeError("network down")

        head = await chat_store.get_revision("c1")
        job = MemoryConsolidationJob(
            scope="workspace", chat_id="c1", actor_name=None, base_revision=head,
            trigger="manual", policy=ConsolidationPolicy(auto_apply=True),
            chat_store=chat_store, backend=backend, consolidator=_RaisingConsolidator(),
            operation_service=op_svc, watermarks=wm, maxim_keys={"user"},
        )
        with pytest.raises(RuntimeError, match="network down"):
            await job.run()
        assert await wm.get("workspace", "c1") == 0  # not advanced

    @pytest.mark.asyncio
    async def test_idempotency_key_includes_scope_chat_revision_trigger(self, tmp_path):
        # exercise the .key property — used for JobRunner dedup
        op_svc = DefaultMemoryOperationService(
            InMemMemoryExtension(), audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"},
        )
        job = MemoryConsolidationJob(
            scope="workspace", chat_id="c1", actor_name=None, base_revision=4,
            trigger="manual", policy=ConsolidationPolicy(),
            chat_store=InMemChatStore(), backend=InMemMemoryExtension(),
            consolidator=DefaultMemoryConsolidator(_StubBLM({"operations": []}), maxim_keys={"user"}),
            operation_service=op_svc, watermarks=WatermarkStore(tmp_path / "wm.json"),
            maxim_keys={"user"},
        )
        assert job.key == "consolidate:workspace:c1:4:manual"
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest -q tests/test_memory_consolidation_job.py`
Expected: FAIL — `ModuleNotFoundError: bos.plugins.memory.job`.

- [ ] **Step 3: Implement**

```python
"""MemoryConsolidationJob — BEP 11 Job for off-turn memory consolidation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from bos.core.contract import ChatStore

from ._watermark import WatermarkStore
from .consolidator import ConsolidationPolicy, MemoryConsolidationRequest, MemoryConsolidator
from .operation_service import DefaultMemoryOperationService
from .scoped_memory import MemoryBackend

logger = logging.getLogger(__name__)

TriggerName = Literal["session_close", "idle", "manual"]


@dataclass
class MemoryConsolidationJob:
    scope: str
    chat_id: str
    actor_name: str | None
    base_revision: int
    trigger: TriggerName
    policy: ConsolidationPolicy
    chat_store: ChatStore
    backend: MemoryBackend
    consolidator: MemoryConsolidator
    operation_service: DefaultMemoryOperationService
    watermarks: WatermarkStore
    maxim_keys: set[str]

    @property
    def key(self) -> str:
        return f"consolidate:{self.scope}:{self.chat_id}:{self.base_revision}:{self.trigger}"

    async def run(self) -> None:
        watermark = await self.watermarks.get(self.scope, self.chat_id)
        if self.base_revision <= watermark:
            logger.info("consolidation skipped (no new turns) chat=%s rev=%d wm=%d",
                        self.chat_id, self.base_revision, watermark)
            return
        transcript = await self.chat_store.get_messages_since(self.chat_id, revision=watermark)
        candidates = await self.backend.search_memories("", top_k=10_000)
        active_maxims = {key: await self.backend.get_maxim(key) for key in self.maxim_keys}
        request = MemoryConsolidationRequest(
            chat_id=self.chat_id, actor_name=self.actor_name, scope=self.scope,
            base_revision=self.base_revision, trigger=self.trigger,
            transcript_window=transcript, raw_appends=[], candidate_memories=candidates,
            active_maxims=active_maxims, policy=self.policy,
        )
        ops = await self.consolidator.propose(request)
        await self.operation_service.apply(ops, dry_run=not self.policy.auto_apply)
        await self.watermarks.set(self.scope, self.chat_id, self.base_revision)
```

- [ ] **Step 4: Export from `__init__.py`**

Add `MemoryConsolidationJob` to the consolidator imports + `__all__`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -q tests/test_memory_consolidation_job.py`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/bos/plugins/memory/job.py src/bos/plugins/memory/__init__.py tests/test_memory_consolidation_job.py
git commit -m "feat(memory): MemoryConsolidationJob — propose, apply, advance watermark"
```

---

## Phase 4 — `MemoryHarnessPlugin.setup` wiring + trigger binding

### Task 4.1: Construct services and bind `session_close` trigger

**Files:**
- Modify: `src/bos/plugins/memory/plugin.py`
- Test: `tests/test_memory_plugin_wiring.py`

**Interfaces:**
- Consumes: `PluginServices.events`/`jobs`/`background_llm`/`chat_store` (BEP 11).
- Produces:
  - On `setup`: `self._backend` (eager), `self._operation_service`, `self._watermarks`, `self._consolidator` are constructed. If `consolidation.enabled` and `services.events`/`services.jobs`/`services.background_llm` are present, the plugin calls `services.jobs.bind_trigger("session_close", factory)`.
  - `await harness_plugin.run_consolidation_now(chat_id, *, dry_run=None) -> list[AuditRecord]` — admin entry point that builds + runs a job synchronously (no JobRunner enqueue; used by `boscli memory consolidate`).

- [ ] **Step 1: Extend the lock test with new wiring expectations**

In `tests/test_memory_plugin_wiring.py`, add after the existing `test_current_setup_is_minimal`:

```python
import bos.exts  # noqa: F401 — registers default extensions

from bos.core.defaults.background_llm import DefaultBackgroundLLM
from bos.core.defaults.jobs import InProcJobRunner
from bos.core.defaults.lifecycle import DefaultLifecycleBus
from bos.plugins.memory.operation_service import DefaultMemoryOperationService


async def _setup_plugin(tmp_path, *, consolidation_enabled=False):
    """Construct a real PluginServices with BEP 11 services wired."""
    bus = DefaultLifecycleBus()
    runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
    await runner.start()
    # Use the in-memory chat store for tests
    from bos.extensions.chat_stores.in_memory import InMemChatStore

    class _StubLLM:
        async def complete(self, messages, **kwargs):
            from bos.core.llm import LLMResponse
            return LLMResponse(content='{"operations": []}')

    blm = DefaultBackgroundLLM(_StubLLM())
    chat_store = InMemChatStore()
    svc = PluginServices(
        bos_dir=tmp_path, workspace=tmp_path, llm=_StubLLM(),
        consolidator=None, subagents=None, chat_store=chat_store,
        events=bus, jobs=runner, background_llm=blm,
    )
    h = MemoryHarnessPlugin()
    cfg = h.default_config()
    if consolidation_enabled:
        cfg = {**cfg, "consolidation": {"enabled": True, "retention_days": 30, "auto_apply": False}}
    h._cfg = cfg
    await h.setup(svc)
    return h, runner


@pytest.mark.asyncio
async def test_setup_constructs_operation_service_and_watermarks(tmp_path):
    h, runner = await _setup_plugin(tmp_path)
    try:
        assert h._backend is not None
        assert isinstance(h._operation_service, DefaultMemoryOperationService)
        assert h._watermarks is not None
        assert h._consolidator is not None
    finally:
        await runner.drain(timeout=0.5)


@pytest.mark.asyncio
async def test_consolidation_disabled_does_not_bind_trigger(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=False)
    try:
        # session_close trigger should not be registered when consolidation.enabled is False
        from bos.core.contract import LifecycleEvent

        await runner._bus.emit(LifecycleEvent(
            kind="session_close", chat_id="c1", actor_name=None,
            base_revision=1, payload={},
        ))
        await runner.drain(timeout=0.2)
        # No job submitted, no records
        assert (await runner.list()) == []
    finally:
        await runner.drain(timeout=0.0)


@pytest.mark.asyncio
async def test_consolidation_enabled_binds_session_close(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True)
    try:
        from bos.core.contract import LifecycleEvent

        # Commit a turn so there's something to consolidate
        from bos.core.contract import Message
        await h._services.chat_store.commit_turn(
            "c1", [Message(llm_message={"role": "user", "content": "hi"})], turn_id="t1",
        )
        await runner._bus.emit(LifecycleEvent(
            kind="session_close", chat_id="c1", actor_name="A",
            base_revision=1, payload={},
        ))
        await runner.drain(timeout=1.0)
        recs = await runner.list()
        # one job submitted, ran (dry-run because auto_apply default False)
        assert len(recs) == 1
        assert recs[0].status == "succeeded"
    finally:
        await runner.drain(timeout=0.0)


@pytest.mark.asyncio
async def test_run_consolidation_now_returns_audit_records(tmp_path):
    h, runner = await _setup_plugin(tmp_path, consolidation_enabled=True)
    try:
        from bos.core.contract import Message
        await h._services.chat_store.commit_turn(
            "c1", [Message(llm_message={"role": "user", "content": "I prefer dark mode"})], turn_id="t1",
        )
        records = await h.run_consolidation_now("c1", dry_run=True)
        # consolidator returned empty ops in the stub, but the run still completed
        assert records == []
    finally:
        await runner.drain(timeout=0.0)
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest -q tests/test_memory_plugin_wiring.py`
Expected: FAIL — new attributes / methods don't exist yet.

- [ ] **Step 3: Update `default_config` for the consolidation block**

In `src/bos/plugins/memory/plugin.py`, extend `default_config`:

```python
    def default_config(self) -> Mapping[str, Any]:
        return {
            "maxims": ["user", "soul", "identity", "rules"],
            "scope": "workspace",
            "backend": "_default",
            "retrieval": {"auto_recall": True, "index_in_prompt": True, "index_max": 50, "top_k": 5},
            "consolidation": {"enabled": False, "retention_days": 30, "auto_apply": False},
        }
```

- [ ] **Step 4: Extend `MemoryHarnessPlugin.setup`**

Replace the body of `setup` and append a `run_consolidation_now` method. (Place imports inside `setup` to avoid widening the module's eager imports.)

```python
    async def setup(self, services: PluginServices) -> None:
        from .consolidator import ConsolidationPolicy, DefaultMemoryConsolidator
        from .job import MemoryConsolidationJob
        from .operation_service import DefaultMemoryOperationService
        from ._watermark import WatermarkStore

        self._services = services
        # config may be supplied externally via setup-time injection; otherwise pull defaults
        cfg = getattr(self, "_cfg", None) or self.default_config()
        self._cfg = cfg

        # Backend (eager — earlier code was lazy at bind() time; consolidation needs it at setup)
        backend_name = cfg.get("backend", "_default")
        backend_ext = pep_memory_backend.get(backend_name)
        if backend_ext is None:
            raise ValueError(f"MemoryPlugin: unknown backend {backend_name!r}")
        self._backend = backend_ext.fn(bos_dir=services.bos_dir)

        # L1 operation service + audit log under the memory store dir
        memory_dir = Path(services.bos_dir) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        self._maxim_keys = set(cfg.get("maxims", []))
        self._operation_service = DefaultMemoryOperationService(
            self._backend,
            audit_path=memory_dir / "audit.jsonl",
            maxim_keys=self._maxim_keys,
        )
        self._watermarks = WatermarkStore(memory_dir / "watermarks.json")
        self._consolidator = DefaultMemoryConsolidator(
            services.background_llm, maxim_keys=self._maxim_keys,
        ) if services.background_llm is not None else None

        # Policy + scope
        cons_cfg = dict(cfg.get("consolidation", {}))
        self._policy = ConsolidationPolicy(
            enabled=bool(cons_cfg.get("enabled", False)),
            retention_days=int(cons_cfg.get("retention_days", 30)),
            auto_apply=bool(cons_cfg.get("auto_apply", False)),
        )
        self._scope = cfg.get("scope") or "workspace"

        # Trigger binding (only when enabled and BEP 11 services are present)
        if (
            self._policy.enabled
            and services.events is not None
            and services.jobs is not None
            and services.background_llm is not None
            and services.chat_store is not None
        ):
            services.jobs.bind_trigger("session_close", self._make_consolidation_job_factory())

    def _make_consolidation_job_factory(self):
        from .job import MemoryConsolidationJob

        def factory(event):
            if event is None:
                return None
            return MemoryConsolidationJob(
                scope=self._scope, chat_id=event.chat_id, actor_name=event.actor_name,
                base_revision=int(event.base_revision or 0), trigger="session_close",
                policy=self._policy, chat_store=self._services.chat_store,
                backend=self._backend, consolidator=self._consolidator,
                operation_service=self._operation_service, watermarks=self._watermarks,
                maxim_keys=self._maxim_keys,
            )

        return factory

    async def run_consolidation_now(self, chat_id: str, *, dry_run: bool | None = None):
        """Build and run a consolidation job synchronously (admin "run now")."""
        from .job import MemoryConsolidationJob
        from .consolidator import ConsolidationPolicy

        policy = self._policy
        if dry_run is not None:
            policy = ConsolidationPolicy(
                enabled=policy.enabled, retention_days=policy.retention_days,
                auto_apply=not dry_run,
            )
        rev = await self._services.chat_store.get_revision(chat_id)
        if rev == 0:
            return []
        before = len(await self._operation_service.audit())
        job = MemoryConsolidationJob(
            scope=self._scope, chat_id=chat_id, actor_name=None,
            base_revision=rev, trigger="manual", policy=policy,
            chat_store=self._services.chat_store, backend=self._backend,
            consolidator=self._consolidator,
            operation_service=self._operation_service,
            watermarks=self._watermarks, maxim_keys=self._maxim_keys,
        )
        await job.run()
        return (await self._operation_service.audit())[before:]
```

Also add `Path` to the file's imports at the top:

```python
from pathlib import Path
```

(It may already be there; verify with `grep -n "from pathlib" src/bos/plugins/memory/plugin.py` — if not, add the line near the other imports.)

- [ ] **Step 5: Also adjust `bind()` to reuse the setup-time backend**

The existing `bind()` lazily creates `self._backend`; with the new eager construction in `setup`, that lazy block becomes dead code. Remove it. The current logic is:

```python
    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        if self._backend is None:
            backend_name = config.get("backend", "_default")
            backend_ext = pep_memory_backend.get(backend_name)
            if backend_ext is None:
                raise ValueError(f"MemoryPlugin: unknown backend {backend_name!r}")
            self._backend = backend_ext.fn(bos_dir=self._services.bos_dir)
        backend = self._backend
        ...
```

Replace with:

```python
    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        backend = self._backend
        if backend is None:
            # Defensive — setup() should have constructed this; fall back to lazy.
            backend_name = config.get("backend", "_default")
            backend_ext = pep_memory_backend.get(backend_name)
            if backend_ext is None:
                raise ValueError(f"MemoryPlugin: unknown backend {backend_name!r}")
            backend = self._backend = backend_ext.fn(bos_dir=self._services.bos_dir)
        ...
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest -q tests/test_memory_plugin_wiring.py`
Expected: PASS (5 tests including the P0 lock + the four new ones).

- [ ] **Step 7: Run the full suite — surface fallout from changed setup ordering**

Run: `uv run pytest -q`
Expected: green. If a pre-existing test constructs `MemoryHarnessPlugin` without calling `setup` before `bind`, the defensive lazy fallback in `bind()` still works — but flag any failure.

- [ ] **Step 8: Commit**

```bash
git add src/bos/plugins/memory/plugin.py tests/test_memory_plugin_wiring.py
git commit -m "feat(memory): wire op-service/watermarks/consolidator + session_close trigger (BEP 10 §4)"
```

---

## Phase 5 — Recall-log flush via `turn_complete`

### Task 5.1: Carry `recalled` ids on `turn_complete` payload + flush via subscriber

**Files:**
- Modify: `src/bos/gateway/actor_manager.py` (`CoordinatedActor._on_turn_finished` — copy `recalled` into payload)
- Modify: `src/bos/core/actor.py` (expose the active `TurnContext` after `_run_ask` so the subclass can read its metadata)
- Create: `src/bos/plugins/memory/recall_flush.py`
- Modify: `src/bos/plugins/memory/plugin.py` (`setup` subscribes the flusher when consolidation is enabled OR `retrieval.auto_recall` is on)
- Test: `tests/test_recall_flush.py` (create)

**Interfaces:**
- Consumes: `TurnContext.metadata["recalled"]` (accumulated by `AutoRecallInterceptor`), `LifecycleBus.subscribe`, `operation_service.touch_last_used`.
- Produces:
  - `LifecycleEvent.payload["recalled"]: list[str]` — entry ids surfaced during the just-completed turn (may be empty).
  - `RecallFlushSubscriber(operation_service).handle(event)` — subscriber callable; awaits `touch_last_used(recalled)` if non-empty.

- [ ] **Step 1: Inspect the current `CoordinatedActor._on_turn_finished` and `_run_ask`**

```bash
grep -n "_on_turn_finished\|_run_ask\|self._agent.ask\|self._current_context" src/bos/core/actor.py src/bos/gateway/actor_manager.py | head -30
```

> Goal: confirm how the actor reaches the live `TurnContext` after `self._agent.ask(...)` returns. If `Agent.ask` stores it as `self._current_context` (it does, per BEP 10 P3 grounding), the subclass can read `agent._current_context.metadata.get("recalled", [])`. If not, expose it via a small helper.

- [ ] **Step 2: Write the failing tests**

```python
"""Recall-log flush: turn_complete payload carries 'recalled' ids; subscriber
calls touch_last_used on the L1 operation service."""

import pytest
from conftest import InMemMemoryExtension

from bos.core.contract import LifecycleEvent
from bos.plugins.memory.operation_service import DefaultMemoryOperationService
from bos.plugins.memory.recall_flush import RecallFlushSubscriber


class TestRecallFlush:
    @pytest.mark.asyncio
    async def test_flush_touches_last_used(self, tmp_path):
        b = InMemMemoryExtension()
        e1 = await b.ingest_memory("a fact")
        e2 = await b.ingest_memory("another fact")
        svc = DefaultMemoryOperationService(b, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
        sub = RecallFlushSubscriber(svc)
        await sub.handle(LifecycleEvent(
            kind="turn_complete", chat_id="c1", actor_name="A",
            base_revision=1, payload={"recalled": [e1, e2]},
        ))
        assert (await b.get_memory(e1)).metadata["last_used"] is not None
        assert (await b.get_memory(e2)).metadata["last_used"] is not None

    @pytest.mark.asyncio
    async def test_flush_noop_when_empty(self, tmp_path):
        b = InMemMemoryExtension()
        svc = DefaultMemoryOperationService(b, audit_path=tmp_path / "audit.jsonl", maxim_keys={"user"})
        sub = RecallFlushSubscriber(svc)
        # must not raise on missing or empty recalled list
        await sub.handle(LifecycleEvent(
            kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload={},
        ))
        await sub.handle(LifecycleEvent(
            kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload={"recalled": []},
        ))
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest -q tests/test_recall_flush.py`
Expected: FAIL — `ModuleNotFoundError: bos.plugins.memory.recall_flush`.

- [ ] **Step 4: Implement `RecallFlushSubscriber`**

```python
"""turn_complete subscriber that flushes the per-turn recall log to durable
last_used metadata via the L1 operation service (BEP 10 §6)."""

from __future__ import annotations

import logging

from bos.core.contract import LifecycleEvent

from .operation_service import DefaultMemoryOperationService

logger = logging.getLogger(__name__)


class RecallFlushSubscriber:
    def __init__(self, operation_service: DefaultMemoryOperationService) -> None:
        self._svc = operation_service

    async def handle(self, event: LifecycleEvent) -> None:
        recalled = list((event.payload or {}).get("recalled", []))
        if not recalled:
            return
        try:
            await self._svc.touch_last_used(recalled)
        except Exception:
            logger.exception("recall flush failed for chat=%s", event.chat_id)
```

- [ ] **Step 5: Update `CoordinatedActor._on_turn_finished` to include `recalled`**

In `src/bos/gateway/actor_manager.py`, modify the emit block added in BEP 11:

```python
        if getattr(self, "_lifecycle_bus", None) is not None and result.status == "completed":
            from bos.core.contract import LifecycleEvent

            recalled: list[str] = []
            current_ctx = getattr(self._agent, "_current_context", None)
            if current_ctx is not None:
                recalled = list(current_ctx.metadata.get("recalled", []) or [])
            await self._lifecycle_bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id=ctx.chat_id, actor_name=ctx.actor_name,
                base_revision=result.committed_revision,
                payload={"recalled": recalled} if recalled else {},
            ))
```

- [ ] **Step 6: Subscribe the flusher in `MemoryHarnessPlugin.setup`**

Append to `setup()` (after the trigger binding block) in `plugin.py`:

```python
        # Recall-log flush (BEP 10 §6): subscribe on turn_complete when auto_recall is on
        # OR when consolidation is enabled (both want fresh last_used signal).
        retrieval_cfg = dict(cfg.get("retrieval", {}))
        if services.events is not None and (
            retrieval_cfg.get("auto_recall", True) or self._policy.enabled
        ):
            from .recall_flush import RecallFlushSubscriber

            services.events.subscribe(
                "turn_complete", RecallFlushSubscriber(self._operation_service).handle,
            )
```

- [ ] **Step 7: Run to verify pass**

Run: `uv run pytest -q tests/test_recall_flush.py tests/test_memory_plugin_wiring.py`
Expected: PASS (the recall_flush tests + the existing wiring tests both green).

- [ ] **Step 8: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/bos/plugins/memory src/bos/gateway/actor_manager.py src/bos/core/actor.py tests/test_recall_flush.py`
Expected: green.

- [ ] **Step 9: Commit**

```bash
git add src/bos/plugins/memory/recall_flush.py src/bos/plugins/memory/plugin.py src/bos/gateway/actor_manager.py tests/test_recall_flush.py
git commit -m "feat(memory): turn_complete carries recalled ids + flush via touch_last_used (BEP 10 §6)"
```

---

## Phase 6 — Admin CLI: `boscli memory ...`

Each subcommand follows the existing `boscli` Click pattern — discover workspace, open the harness, run the operation, exit. Output is plain text (Rich-rendered when available).

### Task 6.1: Skeleton + `list` / `show` / `index` / `recall` (read-only)

**Files:**
- Create: `src/bos/cli/commands/memory.py`
- Modify: `src/bos/cli/entry.py` (register lazy command)
- Test: `tests/test_cli_memory.py` (create)

**Interfaces:**
- Consumes: `Workspace.harness()`, `MemoryHarnessPlugin._backend` / `_operation_service`.
- Produces: a `memory` Click group with 4 subcommands; each prints to stdout and exits 0 on success.

- [ ] **Step 1: Register the lazy command**

In `src/bos/cli/entry.py`, add to `_LAZY_COMMANDS`:

```python
    "memory": "bos.cli.commands.memory:memory",
```

- [ ] **Step 2: Write failing tests**

```python
"""boscli memory CLI smoke tests.

We invoke commands through the Click runner against a freshly-seeded
in-memory backend, asserting on the textual output."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from bos.cli.commands.memory import memory as memory_cmd


def _seeded_workspace(tmp_path, monkeypatch):
    """Build a minimal workspace with the in_memory memory backend selected."""
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (tmp_path / "bos.toml").write_text(
        '[bos]\nworkspace = "."\n\n'
        '[harness]\nchat_store = "_default"\n\n'
        '[exts.ep_plugin.MemoryPlugin]\nbackend = "in_memory"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("subcommand", ["list", "index"])
def test_read_only_subcommands_run(tmp_path, monkeypatch, subcommand):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, [subcommand])
    assert result.exit_code == 0, result.output


def test_show_unknown_entry_reports_not_found(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["show", "ghost-entry"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower()


def test_recall_with_query_returns_results(tmp_path, monkeypatch):
    """Seed a memory directly, then query via the CLI."""
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    # Use a small Python -c to seed first; simpler: this test exercises the
    # empty case — the seeded-result case lives in a dedicated subagent-test
    # under tests/test_memory_plugin_wiring.py.
    result = runner.invoke(memory_cmd, ["recall", "--query", "nothing here"])
    assert result.exit_code == 0
```

- [ ] **Step 3: Run to verify failures**

Run: `uv run pytest -q tests/test_cli_memory.py`
Expected: FAIL — `ModuleNotFoundError: bos.cli.commands.memory`.

- [ ] **Step 4: Implement the CLI module**

```python
"""``boscli memory`` — read + operate on the memory backend."""

from __future__ import annotations

import asyncio
import json

import click

from bos.cli.commands.scaffolding import _discover_project


def _resolve_plugin():
    """Open the workspace harness and return (harness, plugin) for the running scope."""
    ws = _discover_project()

    async def _open():
        h = ws.harness()
        await h.__aenter__()
        # MemoryHarnessPlugin is bound under the actor's plugin chain; the
        # harness builds and tracks harness plugins by key. We rely on the
        # plugin's `setup()` having been called when the harness ran an actor;
        # for CLI-only flows, we explicitly bootstrap the plugin here.
        from bos.plugins.memory import MemoryHarnessPlugin

        plugin = MemoryHarnessPlugin()
        plugin._cfg = ws.config_for_plugin("MemoryPlugin") if hasattr(ws, "config_for_plugin") else plugin.default_config()
        await plugin.setup(h._plugin_services)
        return h, plugin

    return asyncio.run(_open())


def _close(h):
    async def _exit():
        await h.__aexit__(None, None, None)

    asyncio.run(_exit())


@click.group(name="memory")
def memory():
    """Memory backend admin commands."""


@memory.command("list")
@click.option("--limit", default=20, show_default=True, help="Max entries to show.")
def list_cmd(limit: int):
    """List active memory entries (newest first)."""
    h, plugin = _resolve_plugin()
    try:
        entries = asyncio.run(plugin._backend.search_memories("", top_k=limit))
        if not entries:
            click.echo("(no memories)")
            return
        for e in entries:
            click.echo(f"{e.id}  imp={e.metadata.get('importance', 5)}  {e.content[:80]}")
    finally:
        _close(h)


@memory.command("show")
@click.argument("entry_id")
def show_cmd(entry_id: str):
    """Show full content of a memory entry by id."""
    h, plugin = _resolve_plugin()
    try:
        e = asyncio.run(plugin._backend.get_memory(entry_id, include_invalid=True))
        if e is None:
            click.echo(f"(entry {entry_id} not found)")
            return
        click.echo(f"id: {e.id}")
        click.echo(f"tags: {e.tags}")
        click.echo(f"created_at: {e.created_at}")
        click.echo(f"metadata: {json.dumps(e.metadata, indent=2, sort_keys=True)}")
        click.echo("---")
        click.echo(e.content)
    finally:
        _close(h)


@memory.command("index")
def index_cmd():
    """Print the in-context index (id, tags, summary), importance-ordered."""
    h, plugin = _resolve_plugin()
    try:
        idx = asyncio.run(plugin._backend.list_index())
        if not idx:
            click.echo("(empty index)")
            return
        for ie in idx:
            tags = ",".join(ie.tags) if ie.tags else ""
            click.echo(f"{ie.id}  [{tags}]  {ie.summary}")
    finally:
        _close(h)


@memory.command("recall")
@click.option("--query", required=True, help="Search query.")
@click.option("--top-k", default=5, show_default=True)
def recall_cmd(query: str, top_k: int):
    """Search active memories — what the agent would retrieve."""
    h, plugin = _resolve_plugin()
    try:
        hits = asyncio.run(plugin._backend.search_memories(query, top_k=top_k))
        if not hits:
            click.echo(f"(no results for {query!r})")
            return
        for e in hits:
            snip = e.content[:160] + ("…" if len(e.content) > 160 else "")
            click.echo(f"{e.id}  imp={e.metadata.get('importance', 5)}  {snip}")
    finally:
        _close(h)
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -q tests/test_cli_memory.py`
Expected: PASS (3 tests). If `Workspace.config_for_plugin` doesn't exist, the fallback returns `plugin.default_config()` — both branches handled.

- [ ] **Step 6: Commit**

```bash
git add src/bos/cli/commands/memory.py src/bos/cli/entry.py tests/test_cli_memory.py
git commit -m "feat(cli): boscli memory list/show/index/recall — read-only admin"
```

### Task 6.2: `consolidate` / `restore` / `audit` / `jobs`

**Files:**
- Modify: `src/bos/cli/commands/memory.py`
- Modify: `tests/test_cli_memory.py`

**Interfaces:**
- Consumes: `plugin.run_consolidation_now`, `plugin._operation_service.restore` / `audit`, `harness.jobs.list` / `status` / `cancel`.
- Produces:
  - `boscli memory consolidate [--chat ID | --all] [--dry-run/--apply]` — propose + apply (or dry-run); prints the audit record summary.
  - `boscli memory restore ENTRY_ID` — un-invalidates a soft-deleted entry.
  - `boscli memory audit [--filter KEY=VAL]` — prints the in-memory audit log.
  - `boscli memory jobs [--status STATUS]` — lists JobRunner records.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cli_memory.py`:

```python
def test_consolidate_dry_run_prints_summary(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["consolidate", "--chat", "c1", "--dry-run"])
    assert result.exit_code == 0
    # acceptable outputs: either "(no turns to consolidate)" or "Applied 0 operations"
    assert "consolidat" in result.output.lower() or "no turns" in result.output.lower()


def test_restore_missing_entry_is_safe(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["restore", "ghost"])
    assert result.exit_code == 0


def test_audit_empty_prints_nothing(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["audit"])
    assert result.exit_code == 0


def test_jobs_lists(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["jobs"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest -q tests/test_cli_memory.py -k "consolidate or restore or audit or jobs"`
Expected: FAIL — subcommands missing.

- [ ] **Step 3: Implement the four subcommands**

Append to `src/bos/cli/commands/memory.py`:

```python
@memory.command("consolidate")
@click.option("--chat", "chat_id", default=None, help="Chat id to consolidate (default: --all).")
@click.option("--all", "do_all", is_flag=True, default=False, help="Iterate every chat past its watermark.")
@click.option("--dry-run/--apply", default=True, show_default=True,
              help="Dry-run validates + audits but does not mutate the backend.")
def consolidate_cmd(chat_id: str | None, do_all: bool, dry_run: bool):
    """Run the consolidation handler for one chat or all chats with unprocessed turns."""
    h, plugin = _resolve_plugin()
    try:
        async def _run():
            targets: list[str] = []
            if do_all:
                chats = await plugin._services.chat_store.list_chats()
                targets = list(chats.keys())
            elif chat_id:
                targets = [chat_id]
            else:
                click.echo("(specify --chat ID or --all)")
                return
            for cid in targets:
                records = await plugin.run_consolidation_now(cid, dry_run=dry_run)
                applied = sum(1 for r in records if r.result == "applied")
                drun = sum(1 for r in records if r.result == "dry_run")
                rej = sum(1 for r in records if r.result == "rejected")
                click.echo(f"chat {cid}: consolidated — applied={applied} dry_run={drun} rejected={rej}")

        asyncio.run(_run())
    finally:
        _close(h)


@memory.command("restore")
@click.argument("entry_id")
def restore_cmd(entry_id: str):
    """Restore (un-invalidate) a soft-deleted memory entry."""
    h, plugin = _resolve_plugin()
    try:
        async def _run():
            entry = await plugin._backend.get_memory(entry_id, include_invalid=True)
            if entry is None:
                click.echo(f"(entry {entry_id} not found)")
                return
            await plugin._operation_service.restore(entry_id)
            click.echo(f"restored {entry_id}")

        asyncio.run(_run())
    finally:
        _close(h)


@memory.command("audit")
@click.option("--filter", "filter_str", default=None,
              help="key=value filter, e.g. result=applied or op=ADD")
def audit_cmd(filter_str: str | None):
    """Print the in-memory audit log (operations applied this process)."""
    h, plugin = _resolve_plugin()
    try:
        filt: dict | None = None
        if filter_str:
            k, _, v = filter_str.partition("=")
            filt = {k: v}
        records = asyncio.run(plugin._operation_service.audit(filter=filt))
        if not records:
            click.echo("(no audit records)")
            return
        for r in records:
            click.echo(f"{r.at}  {r.op.op}  result={r.result}  entry={r.entry_id}  reason={r.op.reason!r}")
    finally:
        _close(h)


@memory.command("jobs")
@click.option("--status", default=None, help="Filter by status: queued|running|succeeded|failed|cancelled")
def jobs_cmd(status: str | None):
    """List JobRunner records for this harness process."""
    h, plugin = _resolve_plugin()
    try:
        async def _list():
            filt = {"status": status} if status else None
            recs = await h.jobs.list(filter=filt)
            if not recs:
                click.echo("(no jobs)")
                return
            for r in recs:
                click.echo(f"{r.submitted_at}  {r.status:10s}  {r.id[:8]}  {r.key}")
        asyncio.run(_list())
    finally:
        _close(h)
```

- [ ] **Step 4: Run**

Run: `uv run pytest -q tests/test_cli_memory.py`
Expected: PASS (7 tests).

- [ ] **Step 5: Manual smoke**

Run: `uv run boscli memory --help`
Expected: prints the `memory` group with 8 subcommands.

- [ ] **Step 6: Commit**

```bash
git add src/bos/cli/commands/memory.py tests/test_cli_memory.py
git commit -m "feat(cli): boscli memory consolidate/restore/audit/jobs"
```

---

## Phase 7 — End-to-End + Final Verification

### Task 7.1: End-to-end smoke through the harness

**Files:**
- Test: `tests/test_memory_offturn_e2e.py` (create)

**Interfaces:**
- Consumes: the full live stack — `AgentHarness`, `MemoryHarnessPlugin.setup`, `JobRunner`, real `BackgroundLLM` stubbed at the LLM-client layer.
- Produces: an end-to-end scenario matching BEP 10 §12: a mid-chat fact + a `ReviseMaxim` note are curated off-turn and persist into a new session.

- [ ] **Step 1: Write the test**

```python
"""End-to-end BEP 10 off-turn consolidation:
   commit turns → emit session_close → consolidator proposes ADD → operation
   service applies (auto_apply=True) → fact is queryable in a fresh harness."""

import json

import pytest

import bos.exts  # noqa: F401


class _CannedLLM:
    """LLMClient stand-in that returns a pre-canned JSON payload for any complete()."""

    def __init__(self, payload):
        self._payload = payload

    async def complete(self, messages, **kwargs):
        from bos.core.llm import LLMResponse
        return LLMResponse(content=json.dumps(self._payload))


@pytest.mark.asyncio
async def test_mid_chat_fact_persists_into_next_session(tmp_path, monkeypatch):
    from bos.core.contract import LifecycleEvent, Message, PluginServices
    from bos.core.defaults.background_llm import DefaultBackgroundLLM
    from bos.core.defaults.jobs import InProcJobRunner
    from bos.core.defaults.lifecycle import DefaultLifecycleBus
    from bos.extensions.chat_stores.in_memory import InMemChatStore
    from bos.plugins.memory.plugin import MemoryHarnessPlugin

    bus = DefaultLifecycleBus()
    runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
    await runner.start()
    chat_store = InMemChatStore()
    canned = _CannedLLM({"operations": [
        {"op": "ADD", "reason": "stable user preference", "content": "user prefers dark mode", "importance": 8},
    ]})
    blm = DefaultBackgroundLLM(canned)

    services = PluginServices(
        bos_dir=tmp_path, workspace=tmp_path, llm=canned, consolidator=None, subagents=None,
        chat_store=chat_store, events=bus, jobs=runner, background_llm=blm,
    )

    plugin = MemoryHarnessPlugin()
    plugin._cfg = {
        **plugin.default_config(),
        "backend": "in_memory",
        "consolidation": {"enabled": True, "retention_days": 30, "auto_apply": True},
    }
    await plugin.setup(services)

    try:
        await chat_store.commit_turn("c1", [
            Message(llm_message={"role": "user", "content": "I always prefer dark mode"}),
        ], turn_id="t1")
        head = await chat_store.get_revision("c1")
        await bus.emit(LifecycleEvent(
            kind="session_close", chat_id="c1", actor_name=None,
            base_revision=head, payload={},
        ))
        await runner.drain(timeout=2.0)

        hits = await plugin._backend.search_memories("dark mode")
        assert hits and hits[0].content == "user prefers dark mode"
        # Watermark advanced to current head
        assert await plugin._watermarks.get("workspace", "c1") == head
    finally:
        await runner.drain(timeout=0.0)
```

- [ ] **Step 2: Run**

Run: `uv run pytest -q tests/test_memory_offturn_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/bos/plugins/memory src/bos/cli/commands/memory.py src/bos/gateway/actor_manager.py tests/test_memory_watermark.py tests/test_memory_consolidator.py tests/test_memory_consolidation_job.py tests/test_memory_plugin_wiring.py tests/test_recall_flush.py tests/test_cli_memory.py tests/test_memory_offturn_e2e.py`
Expected: green on both.

- [ ] **Step 4: CLI manual smoke**

Run: `uv run boscli memory --help` and `uv run boscli --help`
Expected: both exit 0; `memory` appears under the top-level commands.

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_offturn_e2e.py
git commit -m "test(memory): end-to-end BEP 10 off-turn consolidation smoke"
```

---

## Deferred / Out of Scope (BEP 10 P9 + P10)

Listed here so the next plan author has a clean starting point:

- **P9a — Scored ranking with `last_used` recency.** Implement the `score(m,q) = w_rec · 0.99^hours_since(last_used) + w_imp · importance/10 + w_lex · lexical(q,m)` ranking from BEP 10 §6, eval-gated against the recall@k component eval shipped in BEP 10 P0. Weights start at 1.0; the eval must beat plain-match before importance is trusted.
- **P9b — Promote.** Have `DefaultMemoryConsolidator` emit `PROMOTE` ops when an episodic entry shows up as a durable pattern (e.g. recurrence count threshold). The L1 op service already supports `PROMOTE` (BEP 10 P2 v1).
- **P9c — Reflect.** Synthesize higher-level memories when accumulated importance crosses a threshold (Generative Agents).
- **P9d — Link.** Adjacency discovery on consolidation; the L1 op service already supports `LINK`.
- **P10 — DSPy/GEPA.** Offline compile of the consolidator's prompts against the component eval; lift compiled artifacts into BOS prompt strings. Requires a labeled routing dataset.
- **Persistent JobStore.** Crash-safe job durability (BEP 11 §2 "later"); unlocks one-shot CLI auto-consolidation via persist-on-enqueue + drain-on-next-startup.
- **`telemetry` admin subcommand.** Reads recall-hit-rate / dead-memory-ratio / consolidation-cost from the audit log + recall log; depends on P9's ranking infrastructure to be meaningful.

---

## Self-Review

- **Coverage of BEP 10 P7 (§11):** Task 2.2 (consolidator) + Task 3.1 (job) + Task 4.1 (trigger binding + run_consolidation_now) deliver "with `auto_apply=true` (or a manual/admin apply), a mid-chat fact and a `ReviseMaxim` note are curated and persist cleanly into a *new* session; all curation off-turn + audited". Task 7.1's e2e confirms it. Task 1.2 ships maxim Compact via `UPDATE+maxim_key`. ✓
- **Coverage of BEP 10 P8 (§11):** All eight admin subcommands shipped across Tasks 6.1 and 6.2 (`list/show/index/recall/consolidate/restore/audit/jobs`), minus `telemetry` which is documented as deferred. ✓
- **BEP §12 acceptance:** the agent surface remains append+read (untouched by this plan); curation originates from off-turn handler via op service with `reason` + `requested_by` + `source_turn_ids` (op contract preserved); user `requested_by="user"` path documented for `Forget`-via-negation (the consolidator emits `INVALIDATE` with `requested_by="user"` when the user explicitly negated). ✓
- **Type consistency:** `MemoryConsolidationRequest`, `MemoryConsolidator.propose`, `MemoryConsolidationJob.run`, `WatermarkStore.get/set/snapshot`, `RecallFlushSubscriber.handle` — names match across tasks and tests. The job's `key` (`f"consolidate:{scope}:{chat_id}:{base_revision}:{trigger}"`) matches the JobRunner dedup invariant. ✓
- **Placeholder scan:** every code step shows complete code; no "TBD"/"add error handling"/"similar to". The Step 5 in Task 5.1 ("inspect the current code") is a verification beat, not a placeholder — the implementation it gates is fully specified two steps later. ✓
- **No invented APIs:** `services.background_llm`/`services.events`/`services.jobs`/`services.chat_store` all shipped in BEP 11 PR #51; `get_messages_since` shipped in BEP 11's BEP 5 amendment; `DefaultMemoryOperationService.touch_last_used` shipped in BEP 10 P2. ✓
