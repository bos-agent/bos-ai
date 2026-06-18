# BEP 11 Async Tasks and Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement BEP 11 v1 — the three generic, memory-agnostic platform services (`LifecycleBus`, `JobRunner`, `BackgroundLLM`) that BEP 10's off-turn track and other future consumers depend on — plus the BEP 5 `ChatStore` revision-window read amendment that consolidation needs.

**Architecture:** Three deliberately-separate primitives wired into the existing harness:
- `LifecycleBus` — in-process pub/sub, ephemeral, single-producer (`AgentActor`), no durability;
- `JobRunner` — in-process reliable execution with idempotency and graceful drain; pluggable via a new `ep_job_runner` extension point; default impl owns its loop task;
- `BackgroundLLM` — a one-off, side-effect-free wrapper over `LLMClient` with local `response_schema` validation and bounded retry.

These hang off `PluginServices` alongside the existing `llm`/`consolidator`/`subagents`/`chat_store`. The harness constructs them in `__aenter__`, drains the job runner in `__aexit__` before other owned services are torn down. The actor emits `turn_complete` (via `ActorManager._on_turn_finished`) and `session_close` (via `AgentActor.retire_session`).

**Tech Stack:** Python ≥3.13, asyncio (Queue, create_task, TimerHandle), pydantic for `HarnessConfig`, existing `ExtensionPoint` registry, `jsonschema` (added if not transitive). No new heavyweight dependencies.

## Global Constraints

- **In-process only.** No message broker, no persistent `JobStore`, no cross-process workers. (Persistent durability is deferred per BEP 11 §2 "Durability vocabulary".)
- **No `session_open`/`every_n_turns` events.** Only `turn_complete` and `session_close` are emitted in v1 (BEP 11 §1).
- **Consolidation never blocks exit.** `drain(timeout)` gives **already-running** jobs a bounded window; queued-but-unstarted jobs are dropped on shutdown (BEP 11 §2 "Durability vocabulary").
- **Subscriber-failure isolation.** A subscriber exception is logged but does not stop fan-out (BEP 11 §1).
- **Idempotency.** One in-flight job per `key`; resubmits with an in-flight key are no-ops. `base_revision` from the lifecycle event is part of the BEP 10 key.
- **`BackgroundLLM` does NOT persist chat.** It must not touch `ChatStore`, must not mutate sessions, must not send mailbox events (BEP 11 §3).
- **Local schema validation always runs** on `BackgroundLLM` responses when `response_schema` is supplied (BEP 11 §3).
- `uv run pytest -q` and `uv run ruff check src tests` must remain green throughout.

---

## Scope and Boundaries

**In scope (BEP 11 v1, §1–§5):**
- BEP 5 amendment: `ChatStore.get_revision()` and `ChatStore.get_messages_since(*, revision)` on the protocol and both default backends (`JsonlChatStore`, `InMemChatStore`).
- `LifecycleBus` (protocol + default in-process impl) emitting `turn_complete` and `session_close`.
- `JobRunner` (protocol + default in-process impl) with `submit`, `bind_trigger`, `drain`, `status`, `list`, `retry`, `cancel`; per-key idempotency; runner-owned per-chat `idle` timer.
- `BackgroundLLM` (protocol + default impl) over `LLMClient` with `response_schema` JSON-Schema validation and one bounded retry.
- New extension point `ep_job_runner` declared on `bos.core.contract`; `_default` impl registered.
- `HarnessConfig.job_runner: str = "_default"` field added.
- `PluginServices` gains three fields: `events`, `jobs`, `background_llm`.
- `AgentHarness.__aenter__` constructs all three; `__aexit__` drains `jobs` before `_aclose`-ing owned services.
- `ActorManager._on_turn_finished` emits `turn_complete` with `base_revision` from `ActorTurnResult.committed_revision`.
- `AgentActor.retire_session` emits `session_close`.
- End-to-end smoke test: a plugin binds a trigger and receives a `LifecycleEvent` with the correct `base_revision`.

**Out of scope (deferred per BEP 11 §2 / §5):**
- Persistent `JobStore` (sqlite-backed durable queue).
- Cron / interval triggers (`"cron"` source feeding the same queue).
- Backpressure policies beyond bounded `asyncio.Queue` semantics.
- `BackgroundLLM` tool-using multi-step (BEP 11 §3).
- Auto-consolidation for one-shot `boscli ask` (BEP 11 §2; needs persist-on-enqueue).
- Distributed/multi-process workers.

## Design decisions / deviations from the BEP text (read before starting)

1. **Protocols live in `bos.core.contract`** alongside `ChatStore`, `EventSink`, `TurnInterceptor`. Default implementations live under `src/bos/core/defaults/`. Mirrors the existing pattern (`JsonlChatStore`, `LLMConsolidator`, `JsonlMailRoute`).
2. **`turn_complete` is emitted from `ActorManager._on_turn_finished`** (gateway/actor_manager.py:44) rather than from `AgentActor._on_turn_finished` (actor.py:371). `ActorManager` is the gateway-side override that already has access to the harness; injecting the bus there is one line. The base `AgentActor` stays bus-agnostic so unit tests that construct an actor without a harness keep working.
3. **`session_close` is emitted from `AgentActor.retire_session`** (actor.py:190). Since the actor doesn't have a harness handle today, the gateway's `ActorManager` injects a small `lifecycle_emitter` callback into the actor at construction; `retire_session` calls it if set, no-ops otherwise. This keeps base `AgentActor` testable in isolation.
4. **`JobRunner` owns its own loop task.** `start()` schedules `asyncio.create_task(self._run_loop())`; `drain(timeout)` cancels the loop after the bounded in-flight window. The harness calls `start()` after construction and `drain()` first thing in `__aexit__`. This matches existing per-actor / per-channel `create_task` patterns rather than introducing a central `TaskGroup` the codebase doesn't have today.
5. **`idle_after`** accepts a string like `"5m"` (parsed by a tiny helper) or an int seconds. Stored canonically as seconds.
6. **`BackgroundLLM` schema validation uses `jsonschema`** (Draft 2020-12). If `jsonschema` isn't already installed as a transitive dependency, P3 adds it explicitly.
7. **`ep_job_runner._default` instances receive the `LifecycleBus`** at construction so they can wire `bind_trigger` subscriptions without a circular import. The harness constructs `events` first, then `jobs`, and passes the bus via the factory's kwargs.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/bos/core/contract.py` | Declare `LifecycleKind`, `LifecycleEvent`, `LifecycleBus`, `JobTrigger`, `JobStatus`, `JobRecord`, `Job`, `JobRunner`, `BackgroundLLM`; declare `ep_job_runner`; add `get_revision`/`get_messages_since` to `ChatStore`; add `events`/`jobs`/`background_llm` fields to `PluginServices` | modify (P1/P2/P3/P4/P5) |
| `src/bos/core/defaults/lifecycle.py` | **new** — `DefaultLifecycleBus` in-process impl | create (P2) |
| `src/bos/core/defaults/jobs.py` | **new** — `InProcJobRunner`, `JobRecord`, time-string parser, registered as `ep_job_runner(name="_default")` | create (P4) |
| `src/bos/core/defaults/background_llm.py` | **new** — `DefaultBackgroundLLM` wrapping `LLMClient` with `jsonschema` validation + bounded retry | create (P3) |
| `src/bos/core/defaults/jsonl_chat_store.py` | implement `get_revision` + `get_messages_since` | modify (P1) |
| `src/bos/extensions/chat_stores/in_memory.py` | implement `get_revision` + `get_messages_since` | modify (P1) |
| `src/bos/config/schema.py` | add `HarnessConfig.job_runner` field | modify (P5) |
| `src/bos/core/harness.py` | construct three new services; pass into `PluginServices`; call `drain` in `__aexit__` | modify (P5) |
| `src/bos/core/actor.py` | optional `lifecycle_emitter` hook; call from `retire_session` | modify (P5) |
| `src/bos/gateway/actor_manager.py` | inject `lifecycle_emitter`; emit `turn_complete` in `_on_turn_finished` | modify (P5) |
| `src/bos/core/__init__.py` | re-export new public types so `from bos.core import LifecycleBus, JobRunner, BackgroundLLM` works | modify (P2/P3/P4) |
| `pyproject.toml` | declare `jsonschema>=4.21` if not transitive | modify (P3) |
| `tests/test_chat_store_revision_window.py` | **new** — BEP 5 amendment coverage on both backends | create (P1) |
| `tests/test_lifecycle_bus.py` | **new** — emit/subscribe/isolation tests | create (P2) |
| `tests/test_background_llm.py` | **new** — schema validation, retry, no-side-effects | create (P3) |
| `tests/test_job_runner.py` | **new** — submit, dedup, drain, bind_trigger, idle timer | create (P4) |
| `tests/test_harness_bep11_wiring.py` | **new** — end-to-end: a plugin gets `turn_complete` and runs a job | create (P5/P6) |

---

## Phase 0 — Lock + Seed

Pin current behavior of touched modules.

### Task 0.1: Regression lock on `ChatStore.commit_turn` revision

**Files:**
- Test: `tests/test_chat_store_revision_window.py` (create)

**Interfaces:**
- Consumes: `bos.core.contract.ChatStore.commit_turn` (existing), `bos.core.contract.ChatCommit` (existing — has `.revision`)
- Produces: a test module that later tasks (P1) extend with `get_revision`/`get_messages_since` cases

- [ ] **Step 1: Write the lock test**

```python
"""BEP 5 amendment: revision-window reads on ChatStore.

P0 locks current behavior: commit_turn returns ChatCommit with a per-chat
monotonically increasing revision starting at 1. P1 adds get_revision and
get_messages_since on the same module."""

import pytest
from conftest import InMemChatStore  # re-exported by tests/conftest.py

from bos.core.contract import Message
from bos.core.defaults.jsonl_chat_store import JsonlChatStore


def _msg(role, content):
    return Message(llm_message={"role": role, "content": content})


@pytest.fixture(params=["jsonl", "inmem"])
def store(request, tmp_path):
    if request.param == "jsonl":
        return JsonlChatStore(bos_dir=tmp_path)
    return InMemChatStore()


class TestCommitRevisionMonotonic:
    @pytest.mark.asyncio
    async def test_first_commit_is_revision_1(self, store):
        commit = await store.commit_turn("c1", [_msg("user", "hi")], turn_id="t1")
        assert commit.revision == 1

    @pytest.mark.asyncio
    async def test_revisions_increment_per_chat(self, store):
        a = await store.commit_turn("c1", [_msg("user", "a")], turn_id="t1")
        b = await store.commit_turn("c1", [_msg("user", "b")], turn_id="t2")
        c = await store.commit_turn("c2", [_msg("user", "c")], turn_id="t3")
        assert (a.revision, b.revision, c.revision) == (1, 2, 1)
```

- [ ] **Step 2: Run to verify pass against current code**

Run: `uv run pytest -q tests/test_chat_store_revision_window.py`
Expected: PASS (4 cases — 2 parametrized × 2 tests).

- [ ] **Step 3: Commit**

```bash
git add tests/test_chat_store_revision_window.py
git commit -m "test(chat-store): lock current commit_turn revision behavior"
```

---

## Phase 1 — BEP 5 Amendment: ChatStore Revision-Window Read

Adds `get_revision(chat_id) -> int` and `get_messages_since(chat_id, *, revision) -> list[Message]` to the `ChatStore` protocol and both default backends. BEP 10's consolidation watermark depends on this; nothing else in BEP 11 does, so it's standalone.

### Task 1.1: Extend the protocol and both backends

**Files:**
- Modify: `src/bos/core/contract.py` (`ChatStore` protocol, ~lines 144–183)
- Modify: `src/bos/core/defaults/jsonl_chat_store.py`
- Modify: `src/bos/extensions/chat_stores/in_memory.py`
- Test: `tests/test_chat_store_revision_window.py`

**Interfaces:**
- Consumes: existing per-message `chat_revision` metadata stamped by `commit_turn` (already implemented in both stores)
- Produces:
  - `ChatStore.get_revision(chat_id: str) -> int` — current head; returns 0 for an empty chat
  - `ChatStore.get_messages_since(chat_id: str, *, revision: int) -> list[Message]` — messages whose `metadata["chat_revision"] > revision`, in commit order

- [ ] **Step 1: Add failing tests**

Append to `tests/test_chat_store_revision_window.py`:

```python
class TestGetRevision:
    @pytest.mark.asyncio
    async def test_empty_chat_is_zero(self, store):
        assert await store.get_revision("never-existed") == 0

    @pytest.mark.asyncio
    async def test_reflects_latest_commit(self, store):
        await store.commit_turn("c1", [_msg("user", "a")], turn_id="t1")
        await store.commit_turn("c1", [_msg("user", "b")], turn_id="t2")
        assert await store.get_revision("c1") == 2


class TestGetMessagesSince:
    @pytest.mark.asyncio
    async def test_returns_only_messages_after_revision(self, store):
        await store.commit_turn("c1", [_msg("user", "a"), _msg("assistant", "A")], turn_id="t1")
        await store.commit_turn("c1", [_msg("user", "b"), _msg("assistant", "B")], turn_id="t2")
        await store.commit_turn("c1", [_msg("user", "c")], turn_id="t3")
        since1 = await store.get_messages_since("c1", revision=1)
        assert [m.llm_message["content"] for m in since1] == ["b", "B", "c"]

    @pytest.mark.asyncio
    async def test_at_head_returns_empty(self, store):
        await store.commit_turn("c1", [_msg("user", "a")], turn_id="t1")
        head = await store.get_revision("c1")
        assert await store.get_messages_since("c1", revision=head) == []

    @pytest.mark.asyncio
    async def test_unknown_chat_returns_empty(self, store):
        assert await store.get_messages_since("ghost", revision=0) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_chat_store_revision_window.py::TestGetRevision tests/test_chat_store_revision_window.py::TestGetMessagesSince`
Expected: FAIL — `AttributeError: 'JsonlChatStore' object has no attribute 'get_revision'` (and the same for `get_messages_since`).

- [ ] **Step 3: Extend the protocol**

In `src/bos/core/contract.py`, find the `class ChatStore(Protocol):` block (around line 144). Add two methods at the end of the protocol body, before the closing of the class:

```python
    async def get_revision(self, chat_id: str) -> int: ...
    async def get_messages_since(self, chat_id: str, *, revision: int) -> list[Message]: ...
```

- [ ] **Step 4: Implement in `JsonlChatStore`**

In `src/bos/core/defaults/jsonl_chat_store.py`, add two methods to the class. The existing per-message `metadata["chat_revision"]` is already stamped at commit time. Use the existing `_load_messages` helper (the file walks one JSONL per chat):

```python
    async def get_revision(self, chat_id: str) -> int:
        messages = await asyncio.to_thread(self._read_all_messages_sync, chat_id)
        return max((m.metadata.get("chat_revision", 0) for m in messages), default=0)

    async def get_messages_since(self, chat_id: str, *, revision: int) -> list[Message]:
        messages = await asyncio.to_thread(self._read_all_messages_sync, chat_id)
        return [m for m in messages if m.metadata.get("chat_revision", 0) > revision]
```

> If the existing helper has a different name (e.g. `_load_messages_sync` or `_read_all`), use that name — both helpers in this file already iterate the JSONL on a thread. Look for the function used by `get_messages`.

- [ ] **Step 5: Implement in `InMemChatStore`**

In `src/bos/extensions/chat_stores/in_memory.py`, add to the class (the store keeps an in-memory list per chat, typically `self._messages: dict[str, list[Message]]`):

```python
    async def get_revision(self, chat_id: str) -> int:
        msgs = self._messages.get(chat_id, [])
        return max((m.metadata.get("chat_revision", 0) for m in msgs), default=0)

    async def get_messages_since(self, chat_id: str, *, revision: int) -> list[Message]:
        return [m for m in self._messages.get(chat_id, []) if m.metadata.get("chat_revision", 0) > revision]
```

> Match the field name actually used by `InMemChatStore` for its message dict — read the file first to confirm whether it's `self._messages` or `self._chats[chat_id].messages`.

- [ ] **Step 6: Run to verify all pass**

Run: `uv run pytest -q tests/test_chat_store_revision_window.py`
Expected: PASS (10 cases — 5 tests × 2 backends).

- [ ] **Step 7: Commit**

```bash
git add src/bos/core/contract.py src/bos/core/defaults/jsonl_chat_store.py src/bos/extensions/chat_stores/in_memory.py tests/test_chat_store_revision_window.py
git commit -m "feat(chat-store): add get_revision + get_messages_since (BEP 5 amendment)"
```

---

## Phase 2 — LifecycleBus

A thin in-process pub/sub. Two kinds: `turn_complete`, `session_close`. Subscriber-failure isolated.

### Task 2.1: Types + protocol + default impl

**Files:**
- Modify: `src/bos/core/contract.py` (add types + protocol)
- Create: `src/bos/core/defaults/lifecycle.py`
- Modify: `src/bos/core/__init__.py` (re-exports)
- Test: `tests/test_lifecycle_bus.py` (create)

**Interfaces:**
- Consumes: nothing (foundational)
- Produces:
  - `LifecycleKind = Literal["turn_complete", "session_close"]`
  - `LifecycleEvent(kind, chat_id, actor_name, base_revision, payload)`
  - `LifecycleBus.subscribe(kind, handler)` (sync method, handler is `Callable[[LifecycleEvent], Awaitable[None]]`)
  - `LifecycleBus.emit(event)` (async)
  - `DefaultLifecycleBus` — default in-process impl

- [ ] **Step 1: Write failing tests**

```python
"""LifecycleBus — in-process pub/sub for turn_complete / session_close."""

import asyncio

import pytest

from bos.core.contract import LifecycleEvent
from bos.core.defaults.lifecycle import DefaultLifecycleBus


def _event(kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload=None):
    return LifecycleEvent(
        kind=kind, chat_id=chat_id, actor_name=actor_name,
        base_revision=base_revision, payload=payload or {},
    )


class TestLifecycleBus:
    @pytest.mark.asyncio
    async def test_subscriber_receives_event(self):
        bus = DefaultLifecycleBus()
        seen = []

        async def handler(e):
            seen.append(e)

        bus.subscribe("turn_complete", handler)
        await bus.emit(_event())
        assert len(seen) == 1
        assert seen[0].base_revision == 1

    @pytest.mark.asyncio
    async def test_subscribers_only_get_their_kind(self):
        bus = DefaultLifecycleBus()
        turns, closes = [], []
        bus.subscribe("turn_complete", lambda e: _append(turns, e))
        bus.subscribe("session_close", lambda e: _append(closes, e))
        await bus.emit(_event(kind="session_close"))
        assert closes and not turns

    @pytest.mark.asyncio
    async def test_subscriber_failure_does_not_break_fanout(self, caplog):
        bus = DefaultLifecycleBus()
        delivered = []

        async def angry(e):
            raise RuntimeError("boom")

        async def calm(e):
            delivered.append(e)

        bus.subscribe("turn_complete", angry)
        bus.subscribe("turn_complete", calm)
        await bus.emit(_event())
        assert len(delivered) == 1
        # The exception is logged but does not propagate
        assert any("boom" in r.getMessage() or "RuntimeError" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_subscribers_is_noop(self):
        bus = DefaultLifecycleBus()
        # must not raise
        await bus.emit(_event())


async def _append(into, e):
    """Async handler that appends; pytest can't await a lambda."""
    into.append(e)
```

> Replace the lambda subscribers above with `_append`-based async handlers (a lambda returning a coroutine is awkward). Final form:

```python
        async def t_handler(e):
            turns.append(e)
        async def c_handler(e):
            closes.append(e)
        bus.subscribe("turn_complete", t_handler)
        bus.subscribe("session_close", c_handler)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_lifecycle_bus.py`
Expected: FAIL — `ImportError: cannot import name 'LifecycleEvent' from 'bos.core.contract'`.

- [ ] **Step 3: Add types + protocol to `contract.py`**

In `src/bos/core/contract.py`, add near the `EventSink` definition (or in a clearly delimited "Lifecycle" block):

```python
LifecycleKind = Literal["turn_complete", "session_close"]


@dataclass(frozen=True)
class LifecycleEvent:
    kind: LifecycleKind
    chat_id: str
    actor_name: str | None
    base_revision: int | None
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LifecycleBus(Protocol):
    def subscribe(self, kind: LifecycleKind, handler: Callable[["LifecycleEvent"], Awaitable[None]]) -> None: ...
    async def emit(self, event: "LifecycleEvent") -> None: ...
```

> If `Callable` / `Awaitable` are not already imported at the top of `contract.py`, add them to the `from collections.abc import ...` line that's already there.

- [ ] **Step 4: Write the default impl**

```python
"""Default in-process LifecycleBus — ephemeral pub/sub with isolated fan-out."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from bos.core.contract import LifecycleEvent, LifecycleKind

logger = logging.getLogger(__name__)

_Handler = Callable[[LifecycleEvent], Awaitable[None]]


class DefaultLifecycleBus:
    """In-process, ephemeral, single-producer-tolerant pub/sub.

    Subscribers register per-kind; emit awaits each subscriber sequentially
    and isolates exceptions so one failing handler cannot break delivery to
    the others (BEP 11 §1)."""

    def __init__(self) -> None:
        self._subscribers: dict[LifecycleKind, list[_Handler]] = defaultdict(list)

    def subscribe(self, kind: LifecycleKind, handler: _Handler) -> None:
        self._subscribers[kind].append(handler)

    async def emit(self, event: LifecycleEvent) -> None:
        for handler in list(self._subscribers.get(event.kind, ())):
            try:
                await handler(event)
            except Exception:
                logger.exception("LifecycleBus handler raised on %r", event.kind)
```

- [ ] **Step 5: Re-export from `bos.core.__init__`**

In `src/bos/core/__init__.py`, add `LifecycleBus`, `LifecycleEvent`, `LifecycleKind` to the imports from `contract` and to `__all__`. Mirror the existing pattern there.

- [ ] **Step 6: Run to verify all pass**

Run: `uv run pytest -q tests/test_lifecycle_bus.py`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add src/bos/core/contract.py src/bos/core/defaults/lifecycle.py src/bos/core/__init__.py tests/test_lifecycle_bus.py
git commit -m "feat(harness): LifecycleBus protocol + default in-process impl (BEP 11 §1)"
```

---

## Phase 3 — BackgroundLLM

A one-off, side-effect-free LLM call. Reuses `LLMClient`. Adds local JSON-Schema validation with one bounded retry. Must NOT persist chat or mutate sessions.

### Task 3.1: Declare `jsonschema` dependency

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `jsonschema>=4.21` available at runtime; `uv sync` regenerates `uv.lock`.

- [ ] **Step 1: Check whether `jsonschema` is already importable under uv**

Run: `uv run python -c "import jsonschema; print(jsonschema.__version__)" 2>&1 | tail -1`
Expected: prints a version string (transitive via litellm) OR raises `ModuleNotFoundError`.

- [ ] **Step 2: Add to `pyproject.toml`**

In `pyproject.toml`, add to the `dependencies = [ ... ]` list (after the pyyaml entry added by the BEP 10 plan):

```toml
    "jsonschema>=4.21",
```

- [ ] **Step 3: Sync and verify**

Run: `uv sync && uv run python -c "import jsonschema; print('ok', jsonschema.__version__)"`
Expected: prints `ok` and a 4.x version.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(harness): declare jsonschema dependency for BackgroundLLM"
```

### Task 3.2: `BackgroundLLM` protocol + default impl

**Files:**
- Modify: `src/bos/core/contract.py` (declare `BackgroundLLM` protocol)
- Create: `src/bos/core/defaults/background_llm.py`
- Modify: `src/bos/core/__init__.py` (re-exports)
- Test: `tests/test_background_llm.py` (create)

**Interfaces:**
- Consumes: `LLMClient.complete(messages, **kwargs)` → `LLMResponse`; `jsonschema.validate`
- Produces:
  - `BackgroundLLM.ask(*, messages, model=None, reasoning_effort=None, tools=None, response_schema=None, metadata=None) -> LLMResponse`
  - `DefaultBackgroundLLM(llm: LLMClient, *, max_retries: int = 1)` — default impl

- [ ] **Step 1: Write failing tests**

```python
"""BackgroundLLM — side-effect-free LLM call with local schema validation."""

import json

import pytest

from bos.core.contract import LLMResponse
from bos.core.defaults.background_llm import DefaultBackgroundLLM


class _StubLLM:
    """Stand-in for LLMClient that records calls and returns canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._responses.pop(0)


def _r(content):
    return LLMResponse(content=content)


SCHEMA = {
    "type": "object",
    "properties": {"op": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["op", "reason"],
}


class TestBackgroundLLM:
    @pytest.mark.asyncio
    async def test_passes_through_when_no_schema(self):
        stub = _StubLLM([_r("hello")])
        blm = DefaultBackgroundLLM(stub)
        resp = await blm.ask(messages=[{"role": "user", "content": "hi"}])
        assert resp.content == "hello"
        assert stub.calls[0]["kwargs"].get("tools") is None

    @pytest.mark.asyncio
    async def test_validates_against_schema_and_returns(self):
        good = _r(json.dumps({"op": "ADD", "reason": "ok"}))
        stub = _StubLLM([good])
        blm = DefaultBackgroundLLM(stub)
        resp = await blm.ask(messages=[{"role": "user", "content": "x"}], response_schema=SCHEMA)
        assert json.loads(resp.content)["op"] == "ADD"

    @pytest.mark.asyncio
    async def test_retries_once_on_schema_failure_then_succeeds(self):
        bad = _r("{not-json")
        good = _r(json.dumps({"op": "ADD", "reason": "ok"}))
        stub = _StubLLM([bad, good])
        blm = DefaultBackgroundLLM(stub, max_retries=1)
        resp = await blm.ask(messages=[{"role": "user", "content": "x"}], response_schema=SCHEMA)
        assert "ADD" in resp.content
        assert len(stub.calls) == 2

    @pytest.mark.asyncio
    async def test_raises_after_retries_exhausted(self):
        bad = _r("{not-json")
        stub = _StubLLM([bad, bad])
        blm = DefaultBackgroundLLM(stub, max_retries=1)
        with pytest.raises(ValueError, match="schema"):
            await blm.ask(messages=[{"role": "user", "content": "x"}], response_schema=SCHEMA)

    @pytest.mark.asyncio
    async def test_passes_model_and_kwargs_through(self):
        stub = _StubLLM([_r("ok")])
        blm = DefaultBackgroundLLM(stub)
        await blm.ask(
            messages=[{"role": "user", "content": "x"}],
            model="anthropic/claude-3", reasoning_effort="low",
            tools=[{"name": "t"}], metadata={"k": "v"},
        )
        kwargs = stub.calls[0]["kwargs"]
        assert kwargs["model"] == "anthropic/claude-3"
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["tools"] == [{"name": "t"}]
        assert kwargs["metadata"] == {"k": "v"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_background_llm.py`
Expected: FAIL — `ModuleNotFoundError: bos.core.defaults.background_llm`.

- [ ] **Step 3: Declare the protocol in `contract.py`**

In `src/bos/core/contract.py`, add near the `LifecycleBus` block:

```python
@runtime_checkable
class BackgroundLLM(Protocol):
    async def ask(
        self, *, messages: list[dict[str, Any]], model: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "LLMResponse": ...
```

- [ ] **Step 4: Write the default impl**

```python
"""Default BackgroundLLM — wraps LLMClient with local JSON-Schema validation.

BEP 11 §3: provider-native structured output is a hint; BackgroundLLM always
validates locally. Validation failure → bounded retry → surface."""

from __future__ import annotations

import json
import logging
from typing import Any

import jsonschema

from bos.core.contract import LLMResponse, ReasoningEffort

logger = logging.getLogger(__name__)


class DefaultBackgroundLLM:
    def __init__(self, llm, *, max_retries: int = 1) -> None:
        self._llm = llm
        self._max_retries = max_retries

    async def ask(
        self, *, messages: list[dict[str, Any]], model: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model, "reasoning_effort": reasoning_effort,
            "tools": tools, "metadata": metadata,
        }
        if response_schema is not None:
            kwargs["response_schema"] = response_schema
        attempt = 0
        last_error: str | None = None
        while True:
            resp = await self._llm.complete(messages, **kwargs)
            if response_schema is None:
                return resp
            try:
                parsed = json.loads(resp.content or "")
                jsonschema.validate(parsed, response_schema)
                return resp
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                last_error = str(e)
                logger.warning("BackgroundLLM schema validation failed (attempt %d): %s", attempt, last_error)
                if attempt >= self._max_retries:
                    raise ValueError(f"BackgroundLLM response failed schema validation: {last_error}")
                attempt += 1
                # Append the failure as a corrective hint and retry
                messages = [*messages, {
                    "role": "user",
                    "content": f"Previous response failed schema validation: {last_error}. Reply ONLY with JSON matching the schema.",
                }]
```

- [ ] **Step 5: Re-export from `bos.core.__init__`**

Add `BackgroundLLM` to the imports + `__all__`.

- [ ] **Step 6: Run to verify all pass**

Run: `uv run pytest -q tests/test_background_llm.py`
Expected: PASS (5 tests).

- [ ] **Step 7: Verify no chat-store side effects**

Run: `grep -n "chat_store\|commit_turn\|save_summary" src/bos/core/defaults/background_llm.py`
Expected: no matches (the file must not touch the chat store). If any line appears, remove it.

- [ ] **Step 8: Commit**

```bash
git add src/bos/core/contract.py src/bos/core/defaults/background_llm.py src/bos/core/__init__.py tests/test_background_llm.py
git commit -m "feat(harness): BackgroundLLM protocol + default with schema validation (BEP 11 §3)"
```

---

## Phase 4 — JobRunner

In-process, idempotent, drain-on-shutdown. Owns its loop. Pluggable via `ep_job_runner`.

### Task 4.1: Types, protocol, `ep_job_runner` extension point

**Files:**
- Modify: `src/bos/core/contract.py` (types, protocol, EP declaration)
- Modify: `src/bos/core/__init__.py` (re-exports)
- Test: `tests/test_job_runner.py` (create)

**Interfaces:**
- Consumes: existing `ExtensionPoint`, `LifecycleEvent`
- Produces:
  - `JobTrigger = Literal["session_close", "idle", "manual"]`
  - `JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]`
  - `Job` runtime-checkable protocol with `key: str` and `async def run(self) -> None`
  - `JobRecord(id, key, status, error, submitted_at, finished_at)`
  - `JobRunner` protocol with `submit`, `bind_trigger`, `drain`, `status`, `list`, `retry`, `cancel`
  - `ep_job_runner = ExtensionPoint(name="ep_job_runner", ...)`

- [ ] **Step 1: Write failing structural tests**

```python
"""InProcJobRunner — submit, dedup, drain, bind_trigger, idle timer."""

import asyncio
from dataclasses import dataclass

import pytest

from bos.core.contract import LifecycleEvent
from bos.core.defaults.jobs import InProcJobRunner
from bos.core.defaults.lifecycle import DefaultLifecycleBus


@dataclass
class _RecJob:
    key: str
    log: list[str]
    delay: float = 0.0

    async def run(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.log.append(self.key)


class TestStructural:
    def test_types_exist(self):
        from bos.core.contract import (
            Job, JobRecord, JobRunner, JobStatus, JobTrigger, ep_job_runner,
        )
        assert "session_close" in JobTrigger.__args__
        assert "manual" in JobTrigger.__args__
        assert "queued" in JobStatus.__args__
        assert hasattr(Job, "run")
        assert hasattr(JobRunner, "submit")
        assert hasattr(JobRunner, "drain")
        # ep_job_runner exists with name attribute
        assert ep_job_runner.name == "ep_job_runner"
        # JobRecord has the right shape
        rec = JobRecord(id="x", key="k", status="queued", error=None,
                        submitted_at="2026-06-17T00:00:00", finished_at=None)
        assert rec.id == "x"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_job_runner.py::TestStructural`
Expected: FAIL — `ImportError: cannot import name 'JobTrigger' from 'bos.core.contract'`.

- [ ] **Step 3: Add types + protocol + EP to `contract.py`**

In `src/bos/core/contract.py`, add a block (placement: near `LifecycleBus`):

```python
JobTrigger = Literal["session_close", "idle", "manual"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@runtime_checkable
class Job(Protocol):
    key: str
    async def run(self) -> None: ...


@dataclass(frozen=True)
class JobRecord:
    id: str
    key: str
    status: JobStatus
    error: str | None
    submitted_at: str
    finished_at: str | None


@runtime_checkable
class JobRunner(Protocol):
    async def submit(self, job: Job) -> str: ...
    def bind_trigger(
        self, trigger: JobTrigger,
        factory: Callable[[LifecycleEvent | None], Job | None],
    ) -> None: ...
    async def drain(self, *, timeout: float) -> None: ...
    async def status(self, job_id: str) -> JobStatus: ...
    async def list(self, *, filter: dict | None = None) -> list[JobRecord]: ...
    async def retry(self, job_id: str) -> None: ...
    async def cancel(self, job_id: str) -> None: ...


ep_job_runner = ExtensionPoint(
    name="ep_job_runner",
    description="Off-critical-path job runner implementations (BEP 11 §2).",
)
```

> Make sure `Callable` is already imported at the top of `contract.py` (it is, by P2). `LifecycleEvent` defined earlier in this file is available here.

- [ ] **Step 4: Re-export from `bos.core.__init__`**

Add `Job`, `JobRecord`, `JobRunner`, `JobStatus`, `JobTrigger`, `ep_job_runner` to the imports + `__all__`.

- [ ] **Step 5: Run the structural test**

Run: `uv run pytest -q tests/test_job_runner.py::TestStructural`
Expected: FAIL — `ModuleNotFoundError: bos.core.defaults.jobs` (the structural test imports `InProcJobRunner` at module top). That's the next task's deliverable; for now narrow the run:

Run: `uv run python -c "from bos.core.contract import Job, JobRecord, JobRunner, JobStatus, JobTrigger, ep_job_runner; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/bos/core/contract.py src/bos/core/__init__.py tests/test_job_runner.py
git commit -m "feat(harness): JobRunner types + ep_job_runner extension point"
```

### Task 4.2: `InProcJobRunner` core — submit, dedup, drain, loop

**Files:**
- Create: `src/bos/core/defaults/jobs.py`
- Test: `tests/test_job_runner.py`

**Interfaces:**
- Consumes: `LifecycleBus`, `JobTrigger`, `JobStatus`, `JobRecord`, `Job` from contract.
- Produces:
  - `InProcJobRunner(bus: LifecycleBus, *, max_concurrency: int = 2, idle_after: float = 300)` — default impl, registered `@ep_job_runner(name="_default")`
  - `start()` schedules the loop; `drain(timeout)` returns when in-flight finish or `timeout` elapses, then cancels the loop.
  - Per-key dedup: a submit with an in-flight `key` returns the existing `job_id` and does not enqueue a duplicate.

- [ ] **Step 1: Add the core tests**

Append to `tests/test_job_runner.py`:

```python
class TestSubmitAndDrain:
    @pytest.mark.asyncio
    async def test_submit_runs_job(self, tmp_path):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=2, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            jid = await runner.submit(_RecJob(key="k1", log=log))
            await runner.drain(timeout=1.0)
            assert log == ["k1"]
            assert await runner.status(jid) == "succeeded"
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_dedup_on_key_in_flight(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            id1 = await runner.submit(_RecJob(key="same", log=log, delay=0.05))
            id2 = await runner.submit(_RecJob(key="same", log=log, delay=0.05))
            assert id1 == id2  # dedup → same id
            await runner.drain(timeout=1.0)
            assert log == ["same"]  # ran exactly once
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_drain_does_not_start_queued_unstarted(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            await runner.submit(_RecJob(key="slow", log=log, delay=0.20))
            # quickly queue another with a different key
            await runner.submit(_RecJob(key="never", log=log))
            # Drain immediately with a tight timeout — only "slow" gets its window
            await runner.drain(timeout=0.30)
            assert "slow" in log
            assert "never" not in log
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_failed_job_records_error(self):
        class _Boom:
            key = "boom"
            async def run(self):
                raise RuntimeError("kaboom")

        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            jid = await runner.submit(_Boom())
            await runner.drain(timeout=1.0)
            assert await runner.status(jid) == "failed"
            records = await runner.list(filter={"status": "failed"})
            assert records and "kaboom" in (records[0].error or "")
        finally:
            await runner.drain(timeout=0.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_job_runner.py::TestSubmitAndDrain`
Expected: FAIL — `ModuleNotFoundError: bos.core.defaults.jobs`.

- [ ] **Step 3: Implement the runner core**

```python
"""Default in-process JobRunner — BEP 11 §2.

v1: in-process, reliable with graceful drain. No persistent JobStore;
no cron; bounded asyncio.Queue for queued work; one in-flight per key."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from bos.core.contract import (
    Job, JobRecord, JobStatus, JobTrigger, LifecycleBus, LifecycleEvent, ep_job_runner,
)

logger = logging.getLogger(__name__)


def _parse_duration(value: str | int | float) -> float:
    """Accept '5m' / '30s' / '1h' / int seconds. Return seconds as float."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh])?\s*", value)
    if not m:
        raise ValueError(f"unrecognized duration: {value!r}")
    n, unit = float(m.group(1)), (m.group(2) or "s")
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


@ep_job_runner(name="_default")
class InProcJobRunner:
    def __init__(
        self, bus: LifecycleBus | None = None, *,
        max_concurrency: int = 2, idle_after: str | int | float = 300,
    ) -> None:
        self._bus = bus
        self._max_concurrency = max(1, int(max_concurrency))
        self._idle_after = _parse_duration(idle_after)
        self._queue: asyncio.Queue[tuple[str, Job]] = asyncio.Queue()
        self._records: dict[str, JobRecord] = {}
        self._inflight_by_key: dict[str, str] = {}  # key -> job_id
        self._workers: list[asyncio.Task] = []
        self._idle_timers: dict[str, asyncio.TimerHandle] = {}
        self._idle_factories: dict[JobTrigger, Callable[[LifecycleEvent | None], Job | None]] = {}
        self._started = False
        self._draining = asyncio.Event()

    # ── lifecycle ──

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"job-runner-worker-{i}")
            for i in range(self._max_concurrency)
        ]

    async def drain(self, *, timeout: float) -> None:
        """Stop accepting new starts and wait up to `timeout` for in-flight to finish.
        Queued-but-unstarted jobs are dropped. Cancels worker tasks at the end."""
        self._draining.set()
        deadline = asyncio.get_event_loop().time() + max(0.0, float(timeout))
        # Wait until all workers are idle (no in-flight) or timeout
        while any(self._record_status_running(rec) for rec in self._records.values()):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.02, remaining))
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()
        for timer in self._idle_timers.values():
            timer.cancel()
        self._idle_timers.clear()
        self._started = False
        self._draining.clear()

    # ── public API ──

    async def submit(self, job: Job) -> str:
        # Dedup: if an in-flight job has this key, return its id and skip enqueue
        if job.key in self._inflight_by_key:
            return self._inflight_by_key[job.key]
        if self._draining.is_set():
            # Accept but log — runtime will drop on drain finalization
            logger.info("submit during drain; job %s will likely be dropped", job.key)
        job_id = uuid.uuid4().hex
        self._records[job_id] = JobRecord(
            id=job_id, key=job.key, status="queued", error=None,
            submitted_at=datetime.now().isoformat(), finished_at=None,
        )
        self._inflight_by_key[job.key] = job_id
        await self._queue.put((job_id, job))
        return job_id

    def bind_trigger(
        self, trigger: JobTrigger,
        factory: Callable[[LifecycleEvent | None], Job | None],
    ) -> None:
        self._idle_factories[trigger] = factory
        if self._bus is None:
            return
        if trigger == "session_close":
            self._bus.subscribe("session_close", self._on_session_close)
        elif trigger == "idle":
            # arm/refresh per-chat timer on each turn_complete
            self._bus.subscribe("turn_complete", self._on_turn_complete_for_idle)

    async def status(self, job_id: str) -> JobStatus:
        rec = self._records.get(job_id)
        return rec.status if rec else "cancelled"

    async def list(self, *, filter: dict | None = None) -> list[JobRecord]:
        records = list(self._records.values())
        if filter:
            for k, v in filter.items():
                records = [r for r in records if getattr(r, k, None) == v]
        return records

    async def retry(self, job_id: str) -> None:
        # Out of scope for v1 (no JobStore retain); a no-op stub kept for protocol parity
        logger.info("retry not implemented in v1 (job_id=%s)", job_id)

    async def cancel(self, job_id: str) -> None:
        rec = self._records.get(job_id)
        if rec and rec.status == "queued":
            self._records[job_id] = self._with(rec, status="cancelled", finished_at=datetime.now().isoformat())
            self._inflight_by_key.pop(rec.key, None)

    # ── internals ──

    @staticmethod
    def _record_status_running(rec: JobRecord) -> bool:
        return rec.status == "running"

    @staticmethod
    def _with(rec: JobRecord, **changes: Any) -> JobRecord:
        return JobRecord(
            id=rec.id, key=changes.get("key", rec.key),
            status=changes.get("status", rec.status),
            error=changes.get("error", rec.error),
            submitted_at=rec.submitted_at,
            finished_at=changes.get("finished_at", rec.finished_at),
        )

    async def _worker(self, n: int) -> None:
        while True:
            job_id, job = await self._queue.get()
            if self._draining.is_set():
                # Drop queued-but-unstarted
                self._records[job_id] = self._with(self._records[job_id], status="cancelled",
                                                   finished_at=datetime.now().isoformat())
                self._inflight_by_key.pop(job.key, None)
                continue
            self._records[job_id] = self._with(self._records[job_id], status="running")
            try:
                await job.run()
                self._records[job_id] = self._with(self._records[job_id], status="succeeded",
                                                   finished_at=datetime.now().isoformat())
            except Exception as e:
                logger.exception("job %s (%s) failed", job_id, job.key)
                self._records[job_id] = self._with(self._records[job_id], status="failed",
                                                   error=str(e), finished_at=datetime.now().isoformat())
            finally:
                self._inflight_by_key.pop(job.key, None)

    async def _on_session_close(self, event: LifecycleEvent) -> None:
        factory = self._idle_factories.get("session_close")
        if factory is None:
            return
        job = factory(event)
        if job is not None:
            await self.submit(job)

    async def _on_turn_complete_for_idle(self, event: LifecycleEvent) -> None:
        if "idle" not in self._idle_factories:
            return
        existing = self._idle_timers.pop(event.chat_id, None)
        if existing is not None:
            existing.cancel()
        loop = asyncio.get_event_loop()
        self._idle_timers[event.chat_id] = loop.call_later(
            self._idle_after, lambda: asyncio.create_task(self._fire_idle(event)),
        )

    async def _fire_idle(self, event: LifecycleEvent) -> None:
        self._idle_timers.pop(event.chat_id, None)
        factory = self._idle_factories.get("idle")
        if factory is None:
            return
        job = factory(event)
        if job is not None:
            await self.submit(job)
```

- [ ] **Step 4: Run to verify the core tests pass**

Run: `uv run pytest -q tests/test_job_runner.py::TestStructural tests/test_job_runner.py::TestSubmitAndDrain`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bos/core/defaults/jobs.py tests/test_job_runner.py
git commit -m "feat(harness): InProcJobRunner — submit/dedup/drain (BEP 11 §2)"
```

### Task 4.3: `bind_trigger` + idle timer + `session_close` handling

**Files:**
- Test: `tests/test_job_runner.py`
- Modify: `src/bos/core/defaults/jobs.py` (no new code expected — earlier task already shipped these; this task locks the behavior)

**Interfaces:**
- Consumes: previously-shipped `bind_trigger`, idle-timer wiring.
- Produces: assertion of the trigger semantics under live `DefaultLifecycleBus`.

- [ ] **Step 1: Add trigger tests**

Append to `tests/test_job_runner.py`:

```python
class TestTriggers:
    @pytest.mark.asyncio
    async def test_session_close_factory_enqueues_a_job(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []

            def factory(event):
                return _RecJob(key=f"closed:{event.chat_id}:r{event.base_revision}", log=log)

            runner.bind_trigger("session_close", factory)
            await bus.emit(LifecycleEvent(
                kind="session_close", chat_id="c1", actor_name="A",
                base_revision=7, payload={},
            ))
            await runner.drain(timeout=1.0)
            assert log == ["closed:c1:r7"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_timer_fires_after_idle_after(self):
        bus = DefaultLifecycleBus()
        # idle_after = 0.05s for the test
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=0.05)
        await runner.start()
        try:
            log: list[str] = []

            def factory(event):
                return _RecJob(key=f"idle:{event.chat_id}", log=log)

            runner.bind_trigger("idle", factory)
            await bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id="c1", actor_name="A",
                base_revision=1, payload={},
            ))
            # wait long enough for the timer to elapse and the job to run
            await asyncio.sleep(0.20)
            await runner.drain(timeout=0.5)
            assert log == ["idle:c1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_idle_timer_resets_on_subsequent_turn(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=0.10)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("idle", lambda e: _RecJob(key="idle1", log=log))
            await bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id="c1", actor_name="A", base_revision=1, payload={},
            ))
            await asyncio.sleep(0.05)  # before timer fires
            await bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id="c1", actor_name="A", base_revision=2, payload={},
            ))
            await asyncio.sleep(0.05)  # still before reset timer fires
            assert log == []
            await asyncio.sleep(0.15)  # now past the second timer
            await runner.drain(timeout=0.5)
            assert log == ["idle1"]
        finally:
            await runner.drain(timeout=0.0)

    @pytest.mark.asyncio
    async def test_factory_returning_none_is_skipped(self):
        bus = DefaultLifecycleBus()
        runner = InProcJobRunner(bus, max_concurrency=1, idle_after=300)
        await runner.start()
        try:
            log: list[str] = []
            runner.bind_trigger("session_close", lambda e: None)
            await bus.emit(LifecycleEvent(
                kind="session_close", chat_id="c1", actor_name="A", base_revision=1, payload={},
            ))
            await runner.drain(timeout=0.2)
            assert log == []
        finally:
            await runner.drain(timeout=0.0)
```

- [ ] **Step 2: Run**

Run: `uv run pytest -q tests/test_job_runner.py`
Expected: PASS (all 9 tests in this file).

- [ ] **Step 3: Commit**

```bash
git add tests/test_job_runner.py
git commit -m "test(harness): JobRunner triggers — session_close, idle reset, factory→None"
```

---

## Phase 5 — Harness Wiring

Three new owned services in `AgentHarness`, the new `HarnessConfig` field, drain ordering in `__aexit__`, and the lifecycle emit points in the gateway-side actor manager.

### Task 5.1: `HarnessConfig.job_runner` field

**Files:**
- Modify: `src/bos/config/schema.py`
- Modify: `src/bos/core/harness.py` (constructor accepts `job_runner` kwarg; store it)
- Test: `tests/test_harness_bep11_wiring.py` (create)

**Interfaces:**
- Consumes: existing `HarnessConfig` (pydantic, `extra="forbid"`).
- Produces:
  - `HarnessConfig(job_runner="...")` is accepted; missing key defaults to `"_default"`.
  - `AgentHarness(..., job_runner="...")` accepts and stores the impl name.

- [ ] **Step 1: Write failing tests**

```python
"""End-to-end wiring of BEP 11 services into the harness."""

import asyncio

import pytest

from bos.config.schema import HarnessConfig


class TestHarnessConfig:
    def test_default_job_runner_field(self):
        cfg = HarnessConfig()
        assert cfg.job_runner == "_default"

    def test_explicit_job_runner_field(self):
        cfg = HarnessConfig(job_runner="custom")
        assert cfg.job_runner == "custom"

    def test_unknown_field_still_rejected(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            HarnessConfig(unknown="x")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestHarnessConfig`
Expected: FAIL — `job_runner` field doesn't exist on `HarnessConfig`.

- [ ] **Step 3: Add the field**

In `src/bos/config/schema.py`, modify the `HarnessConfig` class:

```python
class HarnessConfig(BaseModel):
    """The ``[harness]`` section — selects which implementation to use per EP.

    ``extra='forbid'`` — every key here must be known to the harness.
    """

    model_config = ConfigDict(extra="forbid")

    consolidator: str = "_default"
    chat_store: str = "_default"
    mail_route: str = "_default"
    job_runner: str = "_default"
    interceptors: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Thread the kwarg through `AgentHarness.__init__`**

In `src/bos/core/harness.py`, find the `__init__` (around line 107). Add a `job_runner` kwarg with the same default and store it:

```python
        job_runner: str = "_default",
```

…and in the body:

```python
        self._job_runner_impl = job_runner
```

…alongside the existing `self._consolidator_impl`, etc.

> Also locate the call sites that construct `AgentHarness` from `HarnessConfig` (often in `bos.config.workspace` or similar — grep for `AgentHarness(` and `consolidator=` to find the chain). Add `job_runner=cfg.job_runner` next to the existing `consolidator=cfg.consolidator` mapping. If the constructor mapping is centralized, one edit suffices.

Run: `grep -rn "AgentHarness(" src/bos | head` to find call sites.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestHarnessConfig`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/bos/config/schema.py src/bos/core/harness.py
git commit -m "feat(harness): HarnessConfig.job_runner field"
```

### Task 5.2: Construct LifecycleBus, JobRunner, BackgroundLLM in `__aenter__`

**Files:**
- Modify: `src/bos/core/harness.py`
- Modify: `src/bos/core/contract.py` (add three fields to `PluginServices`)
- Test: `tests/test_harness_bep11_wiring.py`

**Interfaces:**
- Consumes: `DefaultLifecycleBus`, `ep_job_runner.invoke("_default", ...)`, `DefaultBackgroundLLM`.
- Produces:
  - `harness.events` is a `LifecycleBus` instance
  - `harness.jobs` is a `JobRunner` instance, started before the harness returns from `__aenter__`
  - `harness.background_llm` is a `BackgroundLLM` instance
  - `harness._plugin_services` exposes all three to plugins

- [ ] **Step 1: Write failing tests**

Append to `tests/test_harness_bep11_wiring.py`:

```python
class TestHarnessServices:
    @pytest.mark.asyncio
    async def test_services_exposed_on_plugin_services(self, tmp_path):
        from bos.core.harness import AgentHarness

        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            svc = h._plugin_services
            assert svc.events is not None
            assert svc.jobs is not None
            assert svc.background_llm is not None
            # JobRunner started — its workers exist
            assert h.jobs is svc.jobs

    @pytest.mark.asyncio
    async def test_drain_called_before_other_teardown(self, tmp_path):
        from bos.core.harness import AgentHarness

        order: list[str] = []

        class _SpyJobRunner:
            async def start(self): pass
            async def drain(self, *, timeout): order.append("drain")
            def bind_trigger(self, *a, **kw): pass
            async def submit(self, job): return "x"
            async def status(self, jid): return "queued"
            async def list(self, *, filter=None): return []
            async def retry(self, jid): pass
            async def cancel(self, jid): pass
            async def aclose(self): order.append("aclose")

        # We do not actually wire _SpyJobRunner via the EP here — instead we
        # patch the harness instance after construction to swap in the spy
        # before __aexit__ runs. The intent is to assert that drain() is
        # awaited before any aclose() in the teardown sequence.
        h = AgentHarness(bos_dir=tmp_path, workspace=tmp_path)
        await h.__aenter__()
        try:
            spy = _SpyJobRunner()
            h.jobs = spy
            # also register as owned so aclose path runs
            h._owned.append(spy)
        finally:
            await h.__aexit__(None, None, None)
        assert order.index("drain") < order.index("aclose")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestHarnessServices`
Expected: FAIL — `PluginServices` has no `events`/`jobs`/`background_llm` fields.

- [ ] **Step 3: Extend `PluginServices`**

In `src/bos/core/contract.py`, modify `PluginServices` (currently around line 363):

```python
@dataclass(frozen=True)
class PluginServices:
    bos_dir: Path
    workspace: Path
    llm: Any  # LLMClient
    consolidator: Consolidator
    subagents: SubagentRuntime
    chat_store: ChatStore | None = None
    events: LifecycleBus | None = None
    jobs: JobRunner | None = None
    background_llm: BackgroundLLM | None = None
```

- [ ] **Step 4: Construct the three services in `__aenter__`**

In `src/bos/core/harness.py` `__aenter__` (currently around line 140), modify the construction sequence. After `self.llm = LLMClient()` and before the `PluginServices(...)` literal, add:

```python
        from bos.core.contract import ep_job_runner
        from bos.core.defaults.background_llm import DefaultBackgroundLLM
        from bos.core.defaults.lifecycle import DefaultLifecycleBus

        self.events = DefaultLifecycleBus()
        self.jobs = await ep_job_runner.invoke(
            self._job_runner_impl, {"bus": self.events},
        )
        await self.jobs.start()
        self._owned.append(self.jobs)
        self.background_llm = DefaultBackgroundLLM(self.llm)
```

Then update the `PluginServices(...)` literal to include the three new fields:

```python
        self._plugin_services = PluginServices(
            bos_dir=self._bos_root,
            workspace=self._workspace,
            llm=self.llm,
            chat_store=self.chat_store,
            consolidator=self.consolidator,
            subagents=_HarnessSubagentRuntime(self),
            events=self.events,
            jobs=self.jobs,
            background_llm=self.background_llm,
        )
```

> Add the three new attributes to `AgentHarness.__init__` defaults too: `self.events = None`, `self.jobs = None`, `self.background_llm = None`. Mirror how `self.chat_store` etc. are pre-declared (around line 128–135).

- [ ] **Step 5: Drain `jobs` first in `__aexit__`**

In `__aexit__` (currently around line 166), modify the top of the function (before `_aclose(self.interceptor)`) to drain the job runner:

```python
    async def __aexit__(self, *exc) -> None:
        if self.jobs is not None:
            try:
                await self.jobs.drain(timeout=5.0)
            except Exception:
                logger.exception("Error draining JobRunner")
        await _aclose(self.interceptor)
        # … existing teardown …
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestHarnessServices`
Expected: PASS (2 tests).

- [ ] **Step 7: Run the full suite to surface fallout**

Run: `uv run pytest -q`
Expected: green. If any pre-existing test constructs `PluginServices(...)` positionally, the new defaulted-`None` fields will not break it. If a test relied on the field order, fix it by switching to keyword args.

- [ ] **Step 8: Commit**

```bash
git add src/bos/core/contract.py src/bos/core/harness.py tests/test_harness_bep11_wiring.py
git commit -m "feat(harness): wire LifecycleBus/JobRunner/BackgroundLLM into PluginServices (BEP 11 §4)"
```

### Task 5.3: Emit `turn_complete` from `ActorManager._on_turn_finished`

**Files:**
- Modify: `src/bos/gateway/actor_manager.py`
- Test: `tests/test_harness_bep11_wiring.py`

**Interfaces:**
- Consumes: `ActorTurnContext` (has `chat_id`, `actor_name`), `ActorTurnResult` (has `status`, `committed_revision`), `LifecycleBus.emit`.
- Produces: a `turn_complete` `LifecycleEvent` with `base_revision = result.committed_revision` is emitted on the bus after every successful turn commit.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_harness_bep11_wiring.py`:

```python
class TestTurnCompleteEmission:
    @pytest.mark.asyncio
    async def test_turn_complete_emits_with_base_revision(self, tmp_path):
        """Override _on_turn_finished's emit path through ActorManager: when
        the manager's hook is called with a 'completed' result, an event must
        fire on the bus with kind='turn_complete' and the committed revision."""
        from bos.core.actor import ActorTurnContext, ActorTurnResult
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.actor_manager import ActorRecord, ActorManager  # adapt if class names differ

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe("turn_complete", handler)

        # Construct just enough to invoke the override directly — we are unit-
        # testing the hook, not the full gateway loop.
        mgr = ActorManager.__new__(ActorManager)  # bypass __init__
        mgr._lifecycle_bus = bus  # the attribute the override reads

        ctx = ActorTurnContext(
            chat_id="c1", actor_name="A", actor_address="A@local",
            turn_id="t1", reply_recipient="user",
        )
        result = ActorTurnResult(status="completed", committed_revision=4)
        await mgr._on_turn_finished(ctx, result)
        assert len(seen) == 1
        assert seen[0].kind == "turn_complete"
        assert seen[0].chat_id == "c1"
        assert seen[0].actor_name == "A"
        assert seen[0].base_revision == 4

    @pytest.mark.asyncio
    async def test_turn_complete_skipped_when_not_completed(self):
        from bos.core.actor import ActorTurnContext, ActorTurnResult
        from bos.core.defaults.lifecycle import DefaultLifecycleBus
        from bos.gateway.actor_manager import ActorManager

        seen = []
        bus = DefaultLifecycleBus()

        async def handler(e):
            seen.append(e)

        bus.subscribe("turn_complete", handler)
        mgr = ActorManager.__new__(ActorManager)
        mgr._lifecycle_bus = bus
        ctx = ActorTurnContext(
            chat_id="c1", actor_name="A", actor_address="A@local",
            turn_id="t1", reply_recipient="user",
        )
        await mgr._on_turn_finished(ctx, ActorTurnResult(status="aborted"))
        await mgr._on_turn_finished(ctx, ActorTurnResult(status="error"))
        await mgr._on_turn_finished(ctx, ActorTurnResult(status="stale"))
        assert seen == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestTurnCompleteEmission`
Expected: FAIL — `AttributeError: 'ActorManager' object has no attribute '_lifecycle_bus'`.

- [ ] **Step 3: Inject the bus + emit in the override**

In `src/bos/gateway/actor_manager.py`, read the existing `_on_turn_finished` (around line 44) and add an emit. Two edits:

(a) Make `_lifecycle_bus` an attribute the manager reads. Find `ActorManager.__init__` (or wherever it's constructed from harness) and add:

```python
    def __init__(self, ..., lifecycle_bus=None):
        ...
        self._lifecycle_bus = lifecycle_bus
```

…and at the construction site (the gateway creates an `ActorManager`), pass `lifecycle_bus=harness.events`. Grep for `ActorManager(` to find the site.

(b) Update `_on_turn_finished` to emit on the bus when the result is `completed`:

```python
    async def _on_turn_finished(self, ctx, result) -> None:
        # … existing logic …
        if (
            getattr(self, "_lifecycle_bus", None) is not None
            and result.status == "completed"
        ):
            from bos.core.contract import LifecycleEvent
            await self._lifecycle_bus.emit(LifecycleEvent(
                kind="turn_complete", chat_id=ctx.chat_id, actor_name=ctx.actor_name,
                base_revision=result.committed_revision, payload={},
            ))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestTurnCompleteEmission`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bos/gateway/actor_manager.py tests/test_harness_bep11_wiring.py
git commit -m "feat(harness): emit turn_complete on LifecycleBus from ActorManager (BEP 11 §1)"
```

### Task 5.4: Emit `session_close` from `AgentActor.retire_session`

**Files:**
- Modify: `src/bos/core/actor.py`
- Modify: `src/bos/gateway/actor_manager.py` (inject the emitter into the actor it constructs)
- Test: `tests/test_harness_bep11_wiring.py`

**Interfaces:**
- Consumes: `LifecycleBus.emit`.
- Produces: when `AgentActor.retire_session(chat_id)` is called, a `session_close` `LifecycleEvent` is emitted *if* an optional `lifecycle_emitter` callback was injected at construction. `AgentActor` stays runnable without it (unit tests, isolated actor instances).

- [ ] **Step 1: Write failing test**

Append to `tests/test_harness_bep11_wiring.py`:

```python
class TestSessionCloseEmission:
    @pytest.mark.asyncio
    async def test_retire_session_emits_session_close(self):
        from bos.core.actor import AgentActor

        seen = []

        async def emitter(*, chat_id, actor_name):
            seen.append((chat_id, actor_name))

        # Construct just enough to call retire_session; rest left as None.
        actor = AgentActor.__new__(AgentActor)
        actor._sessions = {"c1": object()}  # any truthy session
        actor._lifecycle_emitter = emitter
        actor._actor_name = "A"
        await actor.retire_session("c1")
        assert seen == [("c1", "A")]

    @pytest.mark.asyncio
    async def test_retire_session_no_emitter_is_silent(self):
        from bos.core.actor import AgentActor

        actor = AgentActor.__new__(AgentActor)
        actor._sessions = {"c1": object()}
        # no _lifecycle_emitter attribute at all
        # must not raise
        await actor.retire_session("c1")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestSessionCloseEmission`
Expected: FAIL — `retire_session` does not consult `_lifecycle_emitter`.

- [ ] **Step 3: Inject + emit in `AgentActor`**

In `src/bos/core/actor.py`, modify `AgentActor.__init__` to accept an optional emitter and store it:

```python
    def __init__(
        self, agent, mailbox, chat_state=None, *,
        lifecycle_emitter=None, actor_name=None,
    ):
        ...
        self._lifecycle_emitter = lifecycle_emitter
        self._actor_name = actor_name
```

…and modify `retire_session` (around line 190):

```python
    async def retire_session(self, chat_id: str | None) -> None:
        ...  # existing pop logic
        emitter = getattr(self, "_lifecycle_emitter", None)
        if emitter is not None and chat_id is not None:
            try:
                await emitter(chat_id=chat_id, actor_name=getattr(self, "_actor_name", None))
            except Exception:
                logger.exception("session_close emitter raised")
```

> Keep `lifecycle_emitter` and `actor_name` keyword-only with `None` defaults so existing constructors (and unit tests) keep working.

- [ ] **Step 4: Wire injection from `ActorManager`**

In `src/bos/gateway/actor_manager.py`, where `AgentActor(...)` is constructed (grep for it), add:

```python
        async def _emit_close(*, chat_id, actor_name):
            if self._lifecycle_bus is not None:
                from bos.core.contract import LifecycleEvent
                await self._lifecycle_bus.emit(LifecycleEvent(
                    kind="session_close", chat_id=chat_id, actor_name=actor_name,
                    base_revision=None, payload={},
                ))

        actor = AgentActor(
            agent, mailbox, chat_state,
            lifecycle_emitter=_emit_close,
            actor_name=record.name,  # or whatever holds the actor's name
        )
```

> Adapt to the exact construction signature — find the existing `AgentActor(` call in `actor_manager.py`, add `lifecycle_emitter=_emit_close` and `actor_name=<the local var>` keyword args.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestSessionCloseEmission`
Expected: PASS (2 tests).

- [ ] **Step 6: Run full suite — surface fallout from the new actor kwargs**

Run: `uv run pytest -q`
Expected: green. If existing `AgentActor(...)` test constructions fail because the kwargs are positional somewhere, fix call sites by promoting the new args to keyword.

- [ ] **Step 7: Commit**

```bash
git add src/bos/core/actor.py src/bos/gateway/actor_manager.py tests/test_harness_bep11_wiring.py
git commit -m "feat(harness): emit session_close from retire_session (BEP 11 §1)"
```

---

## Phase 6 — End-to-End Smoke + Final Verification

A plugin binds a trigger, a `turn_complete` event fires, the bound factory returns a `Job`, the runner runs it, and we observe the side effect — all under the real harness.

### Task 6.1: End-to-end smoke

**Files:**
- Test: `tests/test_harness_bep11_wiring.py`

**Interfaces:**
- Consumes: full BEP 11 stack assembled in P2/P3/P4/P5.
- Produces: integration confidence that the contracts compose end-to-end.

- [ ] **Step 1: Write the smoke test**

Append to `tests/test_harness_bep11_wiring.py`:

```python
class TestE2E:
    @pytest.mark.asyncio
    async def test_plugin_can_bind_trigger_and_receive_event(self, tmp_path):
        """A consumer binds 'turn_complete' on the JobRunner, emits an event
        via the bus, the bound factory builds a Job, the runner runs it,
        the side effect is observable. This is the BEP 10 consolidation flow's
        contract."""
        from bos.core.contract import LifecycleEvent
        from bos.core.harness import AgentHarness

        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            log: list[int] = []

            class _RecJob:
                def __init__(self, key, rev):
                    self.key = key
                    self._rev = rev
                async def run(self):
                    log.append(self._rev)

            def factory(event):
                if event is None:
                    return None
                return _RecJob(key=f"{event.chat_id}:{event.base_revision}", rev=event.base_revision or 0)

            # Bind on session_close because turn_complete is wired to refresh
            # the idle timer in v1; session_close fires the factory directly.
            h.jobs.bind_trigger("session_close", factory)
            await h.events.emit(LifecycleEvent(
                kind="session_close", chat_id="c1", actor_name="A",
                base_revision=42, payload={},
            ))
            # let the runner pick it up + finish
            await h.jobs.drain(timeout=1.0)
            # restart workers so subsequent code in the harness exit can drain cleanly
            await h.jobs.start()
            assert log == [42]
```

- [ ] **Step 2: Run**

Run: `uv run pytest -q tests/test_harness_bep11_wiring.py::TestE2E`
Expected: PASS.

- [ ] **Step 3: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src/bos/core/defaults/lifecycle.py src/bos/core/defaults/jobs.py src/bos/core/defaults/background_llm.py src/bos/core/contract.py src/bos/core/harness.py src/bos/core/actor.py src/bos/gateway/actor_manager.py src/bos/config/schema.py tests/test_chat_store_revision_window.py tests/test_lifecycle_bus.py tests/test_background_llm.py tests/test_job_runner.py tests/test_harness_bep11_wiring.py`
Expected: tests green; ruff clean on every touched file. Fix any new findings before committing.

- [ ] **Step 4: CLI smoke**

Run: `uv run boscli --help`
Expected: exits 0 (confirms imports across the touched modules resolve).

- [ ] **Step 5: Commit**

```bash
git add tests/test_harness_bep11_wiring.py
git commit -m "test(harness): end-to-end smoke for BEP 11 bind_trigger + emit"
```

---

## Deferred / Out of Scope (BEP 11 §2/§5)

These are explicitly out of scope and must NOT be attempted in this plan. When a follow-up plan addresses BEP 10 phases 4–10, it may also unlock the items below:

- **Persistent `JobStore`** — sqlite-backed crash-safe queue. Needed for: always-on/multi-process deployments and auto-consolidation on one-shot CLI (persist-on-enqueue + drain-on-next-startup).
- **`"cron"` trigger** + APScheduler evaluation.
- **Backpressure policies** beyond the bounded `asyncio.Queue`.
- **`BackgroundLLM` multi-step / tool-using** flows.
- **`every_n_turns` / `session_open` events** — BEP 11 explicitly excludes them.

---

## Self-Review (performed against BEP 11 §1–§5)

- **§1 `LifecycleBus`:** types + protocol + default impl (Task 2.1); only two event kinds (`turn_complete`, `session_close`); subscriber-failure isolation tested; sole producer is the actor side (ActorManager + retire_session — Tasks 5.3, 5.4). ✓
- **§2 `JobRunner`:** all protocol methods covered (Tasks 4.1, 4.2); idempotency on `key`; `drain(timeout)` does not start queued-but-unstarted; runner owns the per-chat idle timer driven by `turn_complete` (Task 4.3); `ep_job_runner` declared and `_default` registered. ✓
- **§3 `BackgroundLLM`:** protocol + default (Task 3.2); side-effect-free (grep test in 3.2 Step 7); local `response_schema` validation + bounded retry; jsonschema declared (Task 3.1); no chat persistence path. ✓
- **§4 Harness wiring:** `PluginServices` gains three fields (Task 5.2); `HarnessConfig.job_runner` added (Task 5.1); `__aenter__` constructs and starts jobs; `__aexit__` drains before owned-aclose (Task 5.2 Step 5). ✓
- **§5 ChatStore revision-window read:** `get_revision` + `get_messages_since` on protocol + both backends (Phase 1). ✓
- **Type consistency:** `LifecycleKind/LifecycleEvent/LifecycleBus`, `JobTrigger/JobStatus/Job/JobRecord/JobRunner`, `BackgroundLLM`, `ep_job_runner` referenced consistently across contract, defaults, and tests; field names (`chat_id`, `actor_name`, `base_revision`, `payload`) match the contract dataclass everywhere they appear in tests. ✓
- **Placeholder scan:** every code step shows full code; no "TBD"/"similar to"/"add error handling" patterns. ✓
- **Spec coverage gaps:** none — every BEP 11 v1 section maps to at least one task. The two items that are explicitly noted as "later" (persistent durability, cron) are in Deferred and not attempted.
