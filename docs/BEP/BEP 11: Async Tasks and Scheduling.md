# BEP 11: Async Tasks and Scheduling

Status: **design** — graduated from stub (2026-06-14) into a contract BEP after the BEP 10 review.
Defines the generic, memory-agnostic platform layer that off-turn work depends on.

Motivation: BEP 10 (Platform-Managed Memory) needs to run LLM-driven work **off the conversational
turn** — triggered by session lifecycle, executed reliably, reasoning via a side-effect-free LLM call.
Those are not memory concerns; they are platform infra. If left undefined, each consumer (memory first,
others later) grows a private scheduler, private LLM calls, and a private write loop. BEP 11 names the
shared layer once.

First consumer: **BEP 10** (consolidation jobs). Designed to also serve future off-turn work —
scheduled/cron agents, batch extraction, async tool fan-out, telemetry rollups.

Depends on: a small **BEP 5 amendment** (ChatStore revision-window read API, §5) so jobs can load a
committed transcript window by revision.

---

## Scope

Three harness-owned services, exposed to plugins via `PluginServices`:

1. **`LifecycleBus`** — an in-process pub/sub for session/turn lifecycle events.
2. **`JobRunner`** — reliable, idempotent, off-critical-path task execution.
3. **`BackgroundLLM`** — one-off, provider-level LLM reasoning with no chat/session side effects.

**Non-goals (initial):** distributed/multi-process workers; a message broker (Redis/RabbitMQ);
crash-safe cross-restart durability; cron (deferred to the durable layer); a general workflow/DAG engine.

These three are deliberately **separate primitives**, not one thing. An event bus (ephemeral fan-out), a
task queue (reliable work execution), and a scheduler (time-based trigger source) have different
durability and cardinality semantics; conflating them couples the hot path to queue mechanics. The clean
shape is **triggers → enqueue → execute**, where lifecycle events and (later) cron are trigger sources
that feed the queue.

---

## 1. `LifecycleBus` — event source

The `AgentActor` is the **sole producer in v1** — it is the only component with the in-memory session
map (`actor.py:31`) and `ChatStore` access. The bus is a thin, in-process, **ephemeral** fan-out: no
durability, no retry (a missed lifecycle event is meaningless to replay).

```python
LifecycleKind = Literal["turn_complete", "session_close"]

@dataclass(frozen=True)
class LifecycleEvent:
    kind: LifecycleKind
    chat_id: str
    actor_name: str | None
    base_revision: int | None       # ChatStore revision at emit time — for idempotency keys
    payload: dict[str, Any]

class LifecycleBus(Protocol):
    def subscribe(self, kind: LifecycleKind, handler: Callable[[LifecycleEvent], Awaitable[None]]) -> None: ...
    async def emit(self, event: LifecycleEvent) -> None: ...
```

- **v1 event kinds are minimal:** `turn_complete` (every committed turn) and `session_close`. There is
  **no** `session_open`/`session_start` event — `session_phase` (new/resumed/continuing) lives on
  `TurnContext.metadata` (BEP 10 §6) and is read at turn time; nothing needs a session-open *event*.
  There is **no** `every_n_turns` (removed: tool-call iterations are not semantic checkpoints).
- **Subscriber-failure isolation (decided):** a subscriber exception is caught and logged; fan-out
  continues. One plugin cannot break another's delivery.
- **Single-producer for now, generalizable later.** When a domain event with a non-actor producer
  actually lands (e.g. a future mid-session maxim refresh on a memory-changed signal — deferred in
  BEP 10 §5), this generalizes to a multi-producer `EventBus`. Not built speculatively.

## 2. `JobRunner` — off-critical-path task execution

The general async-task infra. Consumers `submit` jobs or `bind_trigger` to enqueue on a lifecycle event.

```python
JobTrigger = Literal["session_close", "idle", "manual"]              # + "cron" (deferred)
JobStatus  = Literal["queued", "running", "succeeded", "failed", "cancelled"]

class Job(Protocol):
    key: str                       # dedup/idempotency key; one in-flight per key
    async def run(self) -> None     # idempotent

class JobRunner(Protocol):
    async def submit(self, job: Job) -> str: ...                        # returns job id; async so a
                                                                        # persistent store can enqueue
    def bind_trigger(self, trigger: JobTrigger,
                     factory: Callable[[LifecycleEvent | None], Job | None]) -> None: ...
    async def drain(self, *, timeout: float) -> None: ...               # graceful-shutdown flush
    async def status(self, job_id: str) -> JobStatus: ...
    async def list(self, *, filter: dict | None = None) -> list[JobRecord]: ...
    async def retry(self, job_id: str) -> None: ...
    async def cancel(self, job_id: str) -> None: ...
```

### Triggers

- **`session_close`** (primary): emitted by the actor when a **chat ends** — *not* a synonym for
  process exit. In a **living runtime** (TUI, server channel) a chat can end while the process keeps
  running, so the enqueued consolidation job runs on the background loop. **Consolidation never blocks
  exit** (decided — Option 1).
- **One-shot CLI** (`boscli ask "…"` → answer → exit): the chat end and process exit coincide, so there
  is no living loop to run the enqueued in-memory job — it would simply evaporate. v1 therefore does
  **not** auto-consolidate here; use the **`manual`** trigger (`boscli memory consolidate`).
  Auto-consolidation for one-shot CLI (persist-on-enqueue + drain-on-next-startup) is a post-persistence
  upgrade (see durability).
- **`idle`**: a runner-owned timer. The runner arms a per-`chat_id` timer on each `turn_complete`; if it
  elapses without another turn, the bound job fires. **Timer ownership lives in the runner** (the
  scheduling layer), driven by actor `turn_complete` events — not in the actor, not in the plugin.
- **`manual`**: admin "run now" (BEP 10 §1.5).

### Durability vocabulary (precise, to avoid over-reading)

- **v1 = reliable in-process, graceful-shutdown drain.** An in-memory queue. At shutdown, `drain(timeout)`
  gives **already-running** jobs a bounded window to finish; it does **not** start queued-but-unstarted
  jobs and **never blocks exit on a fresh `BackgroundLLM` call**. Consequently a `session_close` that
  coincides with process exit (one-shot CLI) is enqueued-but-never-started → dropped (no learning; use
  `manual`). Not crash-safe (a `SIGKILL`/power loss drops queued work).
- **Later = crash-safe persistent durability.** A persistent `JobStore` (records, statuses, retries) for
  always-on/multi-process deployments. Also unlocks **auto-consolidation for one-shot CLI** via
  persist-on-enqueue + drain-on-next-startup. Deferred until that regime exists.

### Idempotency

One in-flight job per `key`; a retry must not duplicate effects. BEP 10 uses
`(scope, chat_id, actor_name, base_revision, trigger)`. `base_revision` comes from the lifecycle event
and pins the transcript window (§5).

### Pluggability and config

A new extension point **`ep_job_runner`** selects the implementation; `_default` is the in-process
runner.

```toml
[harness]
job_runner = "_default"           # NEW key — requires HarnessConfig schema change (§4)
[exts.ep_job_runner._default]
max_concurrency = 2
persistence     = "memory"        # "memory" (v1) | "store" (crash-safe, later)
idle_after      = "5m"            # idle-trigger timer
```

## 3. `BackgroundLLM` — provider-level off-turn reasoning

A one-off, **side-effect-free** LLM call for background jobs. It does **not** persist chat, mutate actor
sessions, or send mailbox events — distinguishing it from a full `Agent.ask` (cf.
`_HarnessSubagentRuntime.ask`, `harness.py:84`, which creates a child chat and *does* persist).

```python
class BackgroundLLM(Protocol):
    async def ask(self, *, messages: list[dict[str, Any]], model: str | None = None,
                  reasoning_effort: ReasoningEffort | None = None,
                  tools: list[dict[str, Any]] | None = None,
                  response_schema: dict[str, Any] | None = None,
                  metadata: dict[str, Any] | None = None) -> LLMResponse: ...
```

- Reuses provider/model resolution from the existing `LLMClient` (`harness.py:149`); config stays explicit.
- `response_schema` supports structured-output decisions (BEP 10's ADD/UPDATE/INVALIDATE operations).
- **Schema validation (decided): a local validator always runs** on the response; provider-native
  structured-output is used as a hint when the provider supports it, but BEP never trusts the provider to
  enforce the schema. Validation failure → bounded retry, then surfaced to the job.
- Full dynamic, multi-step, tool-using `agent.ask` is **deferred** as a separate later capability.

---

## 4. Harness wiring (explicit, because current config rejects it)

These are required implementation steps, not implied:

- **`PluginServices`** (`contract.py:363`) gains three fields: **`background_llm`**, **`events`**
  (the `LifecycleBus`), **`jobs`** (the `JobRunner`).
- **`HarnessConfig`** (`schema.py:91`, currently `extra="forbid"` with `consolidator`/`chat_store`/
  `mail_route`/`interceptors`) gains **`job_runner: str = "_default"`** — without this, the proposed
  config is rejected at validation.
- **`AgentHarness.__aenter__`** (`harness.py:140`) constructs the `LifecycleBus`, the `JobRunner` (via
  `ep_job_runner`), and the `BackgroundLLM`, and adds them to `PluginServices` (`harness.py:154`).
- **`AgentActor`** emits `turn_complete` / `session_close` on the bus; otherwise unchanged.
- **Loop ownership & shutdown (resolves review item C).** The `JobRunner` exposes a `run()` coroutine
  (its processing loop) that the runtime adds to the **same `asyncio.TaskGroup` that hosts the actor and
  channels** (`runner.py`), so it lives and is cancelled with them. On shutdown, `AgentHarness.__aexit__`
  (`harness.py:166`) calls `await job_runner.drain(timeout=…)` **before** `_aclose`-ing owned services —
  giving in-flight jobs their bounded window while `BackgroundLLM`/`ChatStore` are still alive — then
  cancels the loop. The drain ordering is the only shutdown subtlety; everything else is a normal owned
  service.

## 5. Dependency: ChatStore revision-window read (BEP 5 amendment)

Consolidation processes only **unprocessed turns**, bounded by a per-chat **watermark** (last-handled
revision, owned by the memory plugin — BEP 10 §4). A run reads turns *after* the watermark up to the
current head, then advances the watermark. `ChatStore` today only exposes
`get_messages(chat_id, active_only=True)` (`contract.py:179`); a small BEP 5 amendment adds:

```python
async def get_revision(self, chat_id: str) -> int: ...                              # current head
async def get_messages_since(self, chat_id: str, *, revision: int) -> list[Message]: ...  # (revision, head]
```

`commit_turn` already returns a `ChatCommit` (with a revision), so the write side exists; this adds the
incremental read. Owned by BEP 5 (ChatStore domain); BEP 10/11 depend on it.

### Scan / scheduled consolidation

BEP 10's `manual` trigger and the **scheduled scan** share one core: *for each chat where
`get_revision() > watermark`, enqueue a job for `get_messages_since(watermark)`.* In v1 the **scheduler
is external** (cron/launchd/systemd, or a future BOS scheduled-task feature) invoking
`boscli memory consolidate --all` — each invocation is its own short-lived runtime, so it needs no daemon
and respects Option 1. An **internal** interval trigger (a BOS-owned 15-min timer) is deferred to the
persistent/daemon job runner.

---

## Open questions

1. **Persistent `JobStore` timing** — when does always-on/multi-process force crash-safe durability beyond in-memory + drain? Storage choice (sqlite vs an existing store)?
2. **Cron/interval triggers** — add `"cron"` as a trigger source feeding the same queue; evaluate **APScheduler** (`AsyncIOScheduler` + sqlite jobstore) as a thin wrap vs homegrown. (Broker-based queues — Celery/arq/taskiq/Procrastinate — assume Redis/Postgres; out of scope until that regime.)
3. **Backpressure** — behavior when the queue is saturated (drop oldest, block, spill to store).
4. **`idle_after` default** — what timer fits the CLI/TUI vs always-on channel cases.

## Implementation note (BEP 10 path)

BEP 10 §12 sequences these as phases 4–6 (L3 `LifecycleBus`, L2 `BackgroundLLM`, L4 `JobRunner`),
parallelizable with the memory read-path work and landing before the consolidation handler (L5). v1 is
deliberately minimal: in-process, no cron, no persistent store, two event kinds, three triggers.

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-14 | Initial stub | Extract the generic async-task/scheduling layer from BEP 10 |
| 2026-06-14 | Graduate to contract BEP | Concrete event kinds + sole-producer model; async `submit` + durability vocabulary; runner-owned idle timer; removed `every_n_turns`; named `PluginServices`/`HarnessConfig`/`ep_job_runner` wiring; local schema validation; ChatStore revision-window dependency (BEP 5) |
