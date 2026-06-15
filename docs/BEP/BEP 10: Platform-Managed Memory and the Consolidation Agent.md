# BEP 10: Platform-Managed Memory and the Consolidation Agent

Status: **design** — core settled in the 2026-06-13 review; flows, layers, and the bottom-up plan
reconciled 2026-06-14 (see `docs/debates/`). **Readiness is per-track:** the storage/read track
(§12 phases 0–3: L0 backend, L1 operation service, L6a read enhancements) depends on **nothing** in
BEP 11 and is ready to implement now; the off-turn consolidation track (phases 4–8) is gated on **BEP 11**
graduating its `LifecycleBus`/`JobRunner`/`BackgroundLLM` contracts.

Background: a verified survey of MemGPT/Letta, Mem0, Zep/Graphiti, Generative Agents, A-MEM, plus Hermes
and Claude Code informed the decisions below.

Supersedes: the **agent tool surface** of BEP 1 (Memory Enhancement Design). BEP 1's two-tier
maxim/memory model, `pep_memory_backend`, and markdown default are retained and extended; its
`Remember`/`Forget`/`ReviseMaxim` *agent* tools are replaced by the read-only surface in §3.

Depends on: **BEP 11 (Async Tasks & Scheduling)** for the generic, memory-agnostic platform services
this design consumes — `JobRunner`, `LifecycleBus`, and `BackgroundLLM`. BEP 10 defines no scheduler of
its own; it is BEP 11's first consumer.

---

## Core Insight

Memory work splits into **reads** (cheap, safe, needed at the point of reasoning) and **writes**
(routing, contradiction resolution, promotion, forgetting — quality-sensitive *judgment*). The
strongest agent-memory systems (Letta sleep-time, Mem0's update phase) do not make the conversational
agent perform writes mid-turn; they move all curation **off-turn**, where a focused process sees the
full transcript and existing memories.

BEP 10 adopts that inversion for BOS:

> The conversational agent **only reads** memory. Every write — episodic memory, maxims, forgetting,
> linking — is performed off-turn by a platform-scheduled **consolidation handler** owned by the memory
> plugin. The storage backend is the source of truth; the system prompt is a cache-friendly *projection*
> of it.

This removes the hardest decisions from the latency path, eliminates a class of prompt-cache
invalidations, and makes the memory policy a single, evaluable, optimizable component.

## Goals

1. Make the conversational agent's memory surface **read-only** (`Recall` + auto-recall + in-context index).
2. Replace the no-op `optimize()` with a real **off-turn consolidation handler** (extract, route, resolve, promote, compact, reflect, link) run as a background job.
3. Define a **cache-aware maxim lifecycle**: maxims load once per session, never mutate the cached prefix mid-chat.
4. Add the minimal **protocol signal** the lifecycle needs: `session_phase` (new / resumed / continuing).
5. Replace hard delete with **soft, reversible forgetting**, with operation provenance.
6. Upgrade retrieval from substring match to an **in-context index + lexical + scored ranking** — no vectors by default.
7. Establish an **evaluation + offline prompt-optimization** path (component eval → DSPy/GEPA), without runtime coupling.

## Non-Goals

- **Vector/embedding retrieval by default.** Reserved for a swap-in backend at scale only (§11). The personal-memory regime uses lexical + in-context index.
- **Defining the scheduler/job runner.** That is BEP 11; BEP 10 consumes it.
- **Coupling to the Obsidian application** (REST/MCP). Obsidian is a transport/export concern handled by an external skill (§10), never a backend.
- **Runtime dependency on DSPy.** DSPy/GEPA is an *offline* optimizer; compiled artifacts are lifted into BOS prompt strings (§9).
- **Secure/irreversible deletion.** Soft-invalidate + retention purge only; privacy/GDPR-style hard erasure is a separate dedicated service.
- **Memory blocks / multi-agent shared memory.** Deferred until BEP 2 (named actors) needs it.
- **A graph database.** Links are an adjacency list in entry metadata (§7), not a graph store.

## Source-of-Truth and Ownership

| Concern | Source of truth | Owner |
|---|---|---|
| Episodic memories & maxims (durable) | memory **backend** (`pep_memory_backend`) | memory plugin |
| Within-session directives | **chat history + compaction summary** (`ChatStore`) | platform |
| System-prompt maxim block | a **projection** of the backend, snapshotted per session | agent runtime |
| All memory **writes** | — | **memory consolidation handler** (off-turn), via the memory operation service |
| Memory **reads** | — | conversational **agent** (`Recall` / auto-recall) |
| Operation provenance & audit | memory operation service | memory plugin |
| When consolidation runs (triggers, jobs) | — | **BEP 11** `JobRunner` + `LifecycleBus` (generic) |
| Consolidation watermark (last-handled revision per chat) | memory plugin store | memory plugin |
| `session_phase` | actor in-memory map + `ChatStore` | **`AgentActor`** |

---

## 1. Flows

Five flows the system must support. Layer tags `[Ln]` refer to §2.

### 1.1 Recall (foreground, read-only)

1. `session_phase` computed (`new`/`resumed`/`continuing`) `[L3]`.
2. Maxims snapshotted once per session → cached prompt prefix `[L6]`.
3. Auto-recall retrieves on the incoming message, injects top-k as ephemeral context **after** the cache breakpoint `[L6]`.
4. In-context index (`id` + tags + one-line summary) sits in the cached prefix; the agent may `Recall(entry_id)` for full content or `Recall(query)` to search `[L6]`.
5. The agent answers, treating recalled memory as context, not proof. `last_used` is updated off-turn from a recall log — never by a mid-turn write `[L1]`.

### 1.2 Durable preference/fact (no agent write)

1. The user states a durable fact; the agent does **not** call a write tool.
2. The live session honors it from chat history / an ephemeral directive.
3. At a trigger boundary, a background job receives the committed transcript window + candidate memories `[L4/L5]`.
4. The consolidation handler proposes: maxim update | episodic add | update existing | contradiction-invalidate | NOOP.
5. If applied, the next new/resumed session surfaces it via maxim snapshot / index / auto-recall / `Recall`.

Persistence is **silent** within the turn (no turn-event); what was learned is visible via the audit log
and admin tooling (§1.5).

### 1.3 Forget / stop using

A write-free read path alone is insufficient — the user would keep getting a fact recalled until the
next consolidation runs. So forget is **two-part**:

1. **Immediate:** record an ephemeral "do-not-use" directive for the live session `[L6]`.
2. Enqueue a background invalidation job, `requested_by="user"` `[L4]`.
3. The handler invalidates matching entries via the operation service, tagged **user-requested** — distinct from model-inferred contradiction invalidation `[L1/L5]`.
4. Invalid entries drop out of recall/index immediately on commit `[L0]`.
5. Admin may restore within retention (§1.5).

`requested_by ∈ {user, consolidator, admin, retention}` is preserved on every invalidation so user
intent is never silently overridden by a later model inference, and vice-versa.

### 1.4 Background workflow (off-turn writes)

1. Trigger fires: `session_close` | `idle` | `manual` `[L3/L7]`.
2. `JobRunner` creates a durable job `[L4]`: idempotency key `(scope, chat_id, actor_name, base_revision, trigger)`; one in-flight per key; flush-on-session-close before process exit.
3. The job (memory plugin, Model B — owns its body) loads the committed transcript window (`ChatStore` @ `base_revision`) + candidate memories + active maxims `[L0]`.
4. The consolidation handler calls `BackgroundLLM` with a `response_schema` to emit **structured operations** — ADD / UPDATE / INVALIDATE / PROMOTE / LINK / NOOP, each with `reason` + `source_turn_ids` `[L2/L5]`.
5. The operation service validates + applies (or dry-runs), writes an audit record, emits a memory-changed event `[L1]`.
6. Applied changes surface on the **next** new/resumed session's maxim snapshot. (In-process mid-session refresh is deferred — §5.)

### 1.5 Admin / operator (platform-owned, not routed through the agent)

```
configure   consolidation: enabled / trigger / retention_days / auto_apply       [config]
inspect     boscli memory list | show <id> | index   # importance, valid, links, source turns  [L7→L0]
search      boscli memory recall "<query>"            # what the agent would retrieve            [L7→L0]
dry-run     boscli memory consolidate --chat <id> --dry-run   # review proposed ops first        [L7→L5]
run now     boscli memory consolidate --chat <id>             # propose + apply (unprocessed turns) [L7→L5]
scan        boscli memory consolidate --all                   # every chat past its watermark; cron-able [L7→L5]
recover     boscli memory restore <id>                # reverse a soft delete within retention    [L7→L1]
audit       boscli memory audit <filter>              # reason / requested_by / result            [L7→L1]
jobs        boscli memory jobs list|get|retry|cancel                                              [L7→L4]
observe     telemetry: recall-hit-rate, dead-memory ratio, maxim churn, cost, failure-rate        [L7→L1]
```

Dry-run and audit are first-class: for a feature whose point is the agent *silently* writing, operators
must review proposed operations before trusting auto-apply and trace *why* any entry exists or was
invalidated.

---

## 2. Layers and Ownership

A layer may depend only on layers below it. **Generic infra is harness-owned (BEP 11) and
memory-agnostic; the memory domain is plugin-owned and swappable; config travels with the owner.**
Per the **Model B** decision, the memory plugin owns its job *body* — the harness provides only the
generic mechanism.

| Layer | Owns | Memory-aware? | Config home |
|---|---|---|---|
| **L0** Storage backend | durable primitives, metadata, soft delete | yes | `[exts.pep_memory_backend.<impl>]` |
| **L1** Memory operation service | validated/audited/dry-run writes; `requested_by`; `last_used` | yes | (code) + plugin policy |
| **L2** `BackgroundLLM` *(BEP 11)* | one-off provider-level structured calls (no chat/session side effects) | **no** | `[harness]` + `[exts.ep_provider.<impl>]` |
| **L3** `LifecycleBus` *(BEP 11)* | `turn_complete`/`session_close` events (actor-produced); `session_phase` is `TurnContext.metadata`; `idle` is a runner timer | **no** | (infra) |
| **L4** `JobRunner` *(BEP 11)* | durable jobs, triggers, idempotency, retry, `drain()` | **no** | `[harness] job_runner` + `[exts.ep_job_runner.<impl>]` |
| **L5** Consolidation handler | `propose()` → structured operations | yes | `[exts.ep_plugin.MemoryPlugin.consolidation]` |
| **L6** Read path | index, read-only tools, auto-recall, maxim snapshot | yes | `[exts.ep_plugin.MemoryPlugin]` (+ per-agent `plugin-bindings`) |
| **L7** Admin surface | `boscli memory …`, dry-run, audit, jobs, telemetry | yes | — |
| **L8** Eval & offline opt | component eval, DSPy/GEPA | — | — |

**The contract.** The harness exposes L2/L3/L4 to plugins via `PluginServices` (alongside today's `llm`,
`subagents`). The memory plugin is a *consumer*: in `setup(services)` it registers a trigger binding +
job factory with `services.jobs`; the job's `run()` uses `services.background_llm` + its own L1
operation service over its L0 backend. The harness job layer never names "memory" — so the
consolidation *policy* (trigger / `retention_days` / `auto_apply`) lives **with the
plugin**, and only the generic job *mechanism* config (concurrency, persistence, retry) is harness-level.
Swap the memory plugin → its policy config swaps with it; the harness infra config stays valid.

```
┌──────────────────────────────────────────────────────────────┐
│ L8  Evaluation & offline optimization                         │
│ L7  Admin surface                                             │
│ L5  Consolidation handler (propose → ops)                     │
├──────────────────────────────────────────────────────────────┤  BEP 11 (generic infra)
│ L4  JobRunner   ◄── L2 BackgroundLLM, L3 LifecycleBus         │
├──────────────────────────────────────────────────────────────┤
│ L1  Memory operation service (validated/audited/dry-run)      │
│ L0  Storage backend (primitives, metadata, soft delete)      │  ← L6 read path depends only on L0
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Agent Tool Surface (read-only) — supersedes BEP 1

The conversational agent is registered with **one** memory tool:

- **`Recall`** — `query` (lexical search → ranked snippets) or `entry_id` (fetch full entry). Unchanged from today's read semantics (`plugin.py:205`).

Removed from the agent surface:

- **`Remember`**, **`Forget`** — no longer agent tools. Their effect is achieved off-turn by the consolidation handler via the operation service (§4). A user "forget" request is handled per §1.3 (immediate ephemeral suppression + background invalidation).
- **`ReviseMaxim`** (`plugin.py:174`) — **deleted**. Its append-only "merge cycle" never existed; maxim writes are off-turn (§5).

Reads are additionally served *without* a tool call by:

- **Auto-recall** — a turn interceptor (`prepare`/`before_llm`) retrieves on the incoming message and injects top hits as ephemeral context (after the cache breakpoint, §5).
- **In-context index** — a compact catalog (`id` + tags + one-line description) of available memories, rendered in the system prompt via `get_system_prompt_section`; the agent fetches full content by `entry_id`.

**Migration safety.** The write tools are removed **only after** the consolidation handler (§4) exists,
so memory is never stranded in a no-write/no-learning window. The read enhancements (index, auto-recall,
ranking) ship first and independently (§12).

---

## 4. Consolidation: handler + trigger

`optimize()` (today a no-op, `markdown_backend.py:123`) is **removed**, not renamed. Storage must not
own consolidation orchestration. Consolidation is a **real handler** owned by the memory plugin,
invoked by generic infra triggers — *not* a method on any storage contract.

### Contract shape (a handler, not a trigger function)

```python
class MemoryConsolidator(Protocol):                  # final name per §14; e.g. MemoryCurator
    async def propose(self, request: MemoryConsolidationRequest) -> list[MemoryOperation]: ...
```

- `propose()` is the **decision** (structured operations); the operation service `apply()` is the
  **write**. Manual/admin "run now" = `propose` + `apply`; dry-run = `propose` + `apply(dry_run=True)`;
  triggered runs = the same, wrapped in a job.
- The request carries the transcript window, candidate memories, active maxims, policy, and trigger
  metadata. The response is **structured operations** — string summaries are insufficient for writes.

The handler performs, over the request:

1. **Extract** — candidate facts worth keeping.
2. **Route** — maxim vs episodic (function, not content: behavior-shaping/always-visible → maxim).
3. **Resolve** — Mem0-style ADD / UPDATE / INVALIDATE / NOOP over the top-`s` similar existing memories. **INVALIDATE is the off-turn forget.**
4. **Promote** — episodic → maxim when a durable pattern is observed.
5. **Compact** — fold maxim revision notes into clean prose near the 2048-char cap (the real "merge cycle").
6. **Reflect** — synthesize higher-level memories when accumulated importance crosses a threshold (Generative Agents).
7. **Link** — attach related entries (§7).

v1 scope is **Extract + Route + Resolve (ADD/UPDATE/INVALIDATE/NOOP)** over episodic memories; promote /
compact / reflect / link are independent, config-gated increments (§12).

### Engine layering (distinct roles — do not conflate)

- **Trigger** = generic infra: `LifecycleBus` event → `JobRunner` binding (BEP 11). Not a memory method.
- **Handler** = `MemoryConsolidator.propose()` (memory plugin, L5).
- **Write** = the memory **operation service** (L1) — validated, audited, dry-run-capable.
- **LLM** = **`BackgroundLLM`** (L2, BEP 11): a provider-level, side-effect-free structured call. *Not* a faked conversational `Agent.ask` (no chat persistence, no session mutation). The memory plugin owns the prompts.
- **DSPy/GEPA** (§9) = the **offline** prompt compiler. Never in the hot path.

Note: chat-history compaction uses `ep_consolidator` today; memory consolidation does **not** — it uses
`BackgroundLLM` directly. (`ep_consolidator` is slated to fold into `ChatStore` once `BackgroundLLM`
exists; see §14.)

### Scheduling and durability (mechanism owned by BEP 11)

- Triggered off the user's critical path: **session_close** (primary), **idle**, or **manual**. Configurable (§8). (No turn-count trigger: tool-call iterations are not semantic checkpoints.)
- **Consolidation never blocks exit (Option 1).** `session_close` here means "a chat ended," not "the process is exiting." Auto-consolidation runs only in a **living runtime** (TUI/server) where the job drains on the background loop. A **one-shot CLI** invocation (`boscli ask` → exit) does not auto-consolidate in v1 — it uses the `manual` trigger (`boscli memory consolidate`); auto-on-next-startup arrives with the persistent job store (BEP 11).
- Jobs are **idempotent** against `(scope, chat_id, actor_name, base_revision, trigger)`; a retry must not duplicate entries. Consolidation is **opt-in**.

### Incremental processing (watermark) and the scan trigger

The handler processes only **unprocessed turns**, tracked by a per-chat **watermark = last-handled
revision**, owned by the memory plugin (the consumer's checkpoint — not chat content, not generic
`ChatStore` state). A run for chat *C* reads turns `(watermark, current]` (via the ChatStore
revision-window read, BEP 11 §5), runs the handler, and on success advances the watermark to `current`.

This makes a **scan** mode possible: *for every chat where `current_revision > watermark`, enqueue a
job for the unprocessed window.* The scan is the shared core behind both delivery mechanisms:

- **Manual** — `boscli memory consolidate [--chat C | --all]` (admin "run now").
- **Scheduled** — the *same* scan invoked by an **external scheduler** (cron/launchd/systemd, or a future
  BOS scheduled-task feature) on an interval (e.g. every 15 min). This fits Option 1: each scheduled
  invocation is its own short-lived runtime that exists *to do* the work — no daemon, no exit-blocking.

The watermark is what makes the scan incremental and dedup correct at the input boundary; the handler's
resolve step still reconciles each new turn against existing memories. An *internal* interval trigger
(BOS firing its own timer) is deferred to the persistent/daemon job runner (BEP 11).

---

## 5. Maxim Lifecycle and Cache Discipline

### In-chat vs cross-chat

- **In-chat immediacy is free.** A directive the user states ("from now on answer in Portuguese") is already in the message history; the model honors it for the rest of the session with **no maxim write**.
- A maxim's unique job is **cross-session persistence**. Therefore maxim writes are **off-turn only** (§4), landing at the next session boundary.

### Loading cadence (decided)

- Maxims are **snapshotted at session start / resume** (keyed off `session_phase`, §6) and held **constant for the whole session**.
- **Never per-iteration, never mid-turn.** Today `_build_system_prompt()` runs once at `ask()` start (`agent.py:375`) **and again every loop iteration** (`agent.py:513`), each time re-reading maxims from storage. With off-turn writes, maxims cannot change within a turn — the per-iteration maxim read is removed via memoization. The per-iteration rebuild may remain for genuinely volatile sections (e.g. skills loaded mid-turn via `LoadSkill`).
- **Mid-session maxim refresh is deferred (not in v1).** A long-lived actor whose maxims change
  underneath it would, in principle, refresh once at a turn boundary on a memory-changed signal. The
  CLI/TUI-first v1 does not need this — a new process re-snapshots anyway — and it is the piece that would
  force a non-actor producer on the `LifecycleBus`. Revisit only when an always-on actor must see a
  same-process maxim change.

### Prompt layering (cache breakpoint after maxims)

```
[ base prompt + tool defs ]    stable / session   ┐ cached
[ in-context index ]           per-session snapshot │ prefix
[ maxims ]                     cross-session       ┘
─────────────── cache breakpoint ───────────────
[ skills loaded mid-turn, system info / time ]    volatile
[ ephemeral "active directives (this session)" ]  volatile, append-only
[ auto-recall hits (this message) ]               volatile
[ chat history … current message ]                append-only
```

- Stable maxims and the per-session index live **before** the breakpoint; anything volatile lives **after** it.
- **"Active now" mid-session, beyond what the messages carry → ephemeral block after the breakpoint** (or a message), never a maxim mutation. `TurnContext` already supports this: `ephemeral` (`agent.py:71`) + `set_ephemeral_message` (`agent.py:98`).

### History and incremental caching

- History is already loaded **once per turn** (`agent.py:368`), not per iteration — no frequency change needed.
- `get_context()` must return an **append-only** serialization (prior turns byte-identical, only new messages at the tail) so the provider caches the growing message prefix; cache busts only at deliberate **compaction boundaries**. Verify `_load_and_compact_history` (`agent.py:707`) does not perturb earlier bytes.

---

## 6. Protocol Addition: `session_phase`

The maxim lifecycle needs to know whether a turn opens a **new**, **resumed**, or **continuing**
session. No first-class signal exists today (interceptor stages are turn-scoped; `TurnContext` has no
such flag; the actor's `SessionState` is process-local and unexposed; the protocol has no "session"
concept — only chat + turn).

**Decision.** Define *session* = a span of turns for a `chat_id` within one process lifetime. The
`AgentActor` — the only component with both the in-memory session map (`actor.py:31`,
`_get_or_create_session` `actor.py:267`) and `ChatStore` access — computes the phase when it
first handles a `chat_id`:

| In-memory session this process? | `ChatStore` has history? | `session_phase` |
|---|---|---|
| no | no | `new` |
| no | yes | `resumed` |
| yes | — | `continuing` |

It is surfaced as **`TurnContext.metadata["session_phase"]`** (string literal `new`/`resumed`/`continuing`),
**`session_phase` is metadata-only** — it is read at turn time and needs no event. (The actor separately
emits `turn_complete`/`session_close` on the `LifecycleBus` for the consolidation triggers, but there is
no `session_open` event; nothing requires one.) No new interceptor stage or core dataclass is required —
`metadata` is the documented escape hatch and flows to every plugin and interceptor.

**Consumers.** Memory plugin is first: `new`/`resumed` → (re)load maxim snapshot; `continuing` → reuse.
Broadly useful (e.g. a skills plugin re-announcing on resume).

---

## 7. Retrieval, Forgetting, and Links (default backend)

### Retrieval — in-context index + lexical + scored ranking (no vectors by default)

`search_memories` (`markdown_backend.py:81`, substring + ctime) is replaced by, in ascending order of N:

1. **In-context index** — catalog surfaced in the prompt; zero recall-miss at small N.
2. **Lexical search** — agentic/iterative via `Recall` (ranked substring at small N; **FTS5/BM25** is a deferred alternate backend at scale, §11).
3. **Scored ranking** over lexical candidates (Generative Agents signals, orthogonal to vectors):
   ```
   score(m,q) = w_rec · 0.99^hours_since(m.last_used) + w_imp · importance(m)/10 + w_lex · lexical(q,m)
   ```
   `importance` (1–10) is assigned **by the consolidation handler at write time** and stored in `MemoryEntry.metadata`. Weights start at 1, terms normalized to [0,1]. Ranking is **eval-gated** — it must beat plain match on the §9 eval before `importance` is trusted.

### Forgetting — soft, reversible, with provenance (Graphiti-style)

- Add `valid` / `invalidated_at` and `source_turn_ids` to `MemoryEntry.metadata`. The off-turn INVALIDATE (via the operation service) sets the flag; `search` filters invalidated entries by default.
- Every invalidation carries `requested_by ∈ {user, consolidator, admin, retention}` — user-requested forget is never silently overridden by model-inferred contradiction.
- A retention job hard-purges entries invalidated beyond `retention_days` (§8). Secure/irreversible erasure is out of scope (a separate service).

### Links — graph-lite (A-MEM / Graphiti BFS)

- Optional `links: list[str]` (entry-id refs) in `MemoryEntry.metadata`. On ingest, the consolidation handler finds candidate neighbors (shared tags / lexical overlap at small N) and the LLM decides which to link. `Recall` may expand one hop. No graph DB.

---

## 8. Configuration Shape

Config splits by owner (§2): generic job infra is harness-level and memory-agnostic; memory policy
travels with the plugin. **Config keys are `[exts.<ep_name>.<impl>]`** — a bare `[exts.MemoryPlugin]`
does **not** resolve (it hits `does not match any registered extension point; ignored`,
`workspace.py:592`); `MemoryPlugin` is an implementation of the `ep_plugin` EP.

```toml
# ── generic infra (BEP 11) — memory-agnostic, harness-level ──
[harness]
job_runner = "_default"
[exts.ep_job_runner._default]
max_concurrency = 2
persistence     = "memory"          # "memory" (v1) | "store" (durable, later)

# ── memory domain — travels with the swappable plugin ──
[exts.ep_plugin.MemoryPlugin]
maxims  = ["user", "soul", "identity", "rules"]   # always-in-prompt keys
scope   = "workspace"
backend = "_default"                              # markdown default; swappable (§11)

[exts.ep_plugin.MemoryPlugin.consolidation]
enabled        = false              # opt-in
trigger        = "session_close"    # "session_close" | "idle"  (+ "manual" via admin)
retention_days = 30                 # hard-purge soft-deleted entries older than this
auto_apply     = false              # v1: propose → audit → operator enables auto-write once trusted

[exts.ep_plugin.MemoryPlugin.retrieval]
auto_recall     = true
index_in_prompt = true
top_k           = 5
```

New `MemoryEntry.metadata` fields: `importance: int (1-10)`, `valid: bool`, `invalidated_at: str|None`,
`last_used: str|None`, `links: list[str]`, `source_turn_ids: list[str]`. Persisted via **YAML
frontmatter** in the default backend (Obsidian-compatible, §10). (`metadata` already exists,
`scoped_memory.py:15`, but the markdown backend has no metadata round-trip today — adding it is the first
implementation step, §12.)

---

## 9. Evaluation and Offline Optimization

**Finding to act on:** BEP 1's rich "USING YOUR MEMORY" prompt is **not wired in** — the running code
uses the terse `_MEMORY_PROMPT_SECTION` (`plugin.py:81`). Do not swap it in blind; measure it.

### Evaluation ladder (build cheapest first)

1. **Component eval (highest ROI)** — labeled set: routing `{transcript → action: maxim:key | memory | nothing}` (targeting the *consolidation handler*) + retrieval `{query → relevant ids}` (recall@k / MRR). ~50 cases; runs in seconds; unblocks all prompt iteration. May run in stub mode before the handler exists.
2. **Scenario eval + LLM-judge** — scripted multi-turn conversations; `BackgroundLLM`/`ep_provider` is stubbable; hang on `tests/test_harness.py`.
3. **Standard benchmarks** — LOCOMO / LongMemEval for headline numbers (conflate retrieval + generation).
4. **Telemetry** — dead-memory ratio (stored-but-never-recalled), recall-hit-rate, maxim churn, consolidation cost, failure rate (surfaced via the L1 audit log).

### DSPy / GEPA (offline only)

Memory decisions map to DSPy Signatures; an optimizer (MIPROv2 / BootstrapFewShot / **GEPA**) compiles
instructions + few-shot demos against the §9 metric + dataset. Precedent:
[`NousResearch/hermes-agent-self-evolution`](https://github.com/NousResearch/hermes-agent-self-evolution)
(DSPy + GEPA). **Rules:** (a) DSPy *consumes* the eval — build it first; (b) run offline and **lift the
compiled instructions/demos into BOS prompt strings**. No runtime dependency.

---

## 10. Obsidian Interop (transport, not backend)

The backend is swappable; Obsidian is **not** a storage tech to swap to (it is a GUI app). Keep BOS
memory as source of truth and move memory to/from Obsidian via an **external skill**: emit YAML
frontmatter (`importance`/`valid`/`invalidated_at`/`tags`/`links`/`source_turn_ids`) and `[[wikilinks]]`.
Because the default backend adopts frontmatter + slugged filenames (§8), the vault is already
Obsidian-compatible and export is near-trivial. The runtime never depends on Obsidian.

---

## 11. Backend Swappability

`pep_memory_backend` makes storage tech a pluggable choice. **Markdown is the default** (right for the
small personal regime). FTS5 and, **only at scale**, a vector DB are alternative backends behind the
same protocol — chosen by config, not bolted onto the default. The decisions above upgrade the default
backend and do not constrain what a deployment swaps in.

---

## 12. Implementation Plan (bottom-up; dependency-ordered)

Each phase rests on layers already built (§2). `[L*]` marks the layer. Phases sharing a row group are
parallelizable.

| # | Phase | Deps | Deliverable | Acceptance |
|---|---|---|---|---|
| 0 | **Lock + seed** | — | Regression tests for current `Recall` / maxim injection / backend CRUD / chat persistence; component-eval skeleton (stub mode) | Tests green on current behavior; eval runs against stubs |
| 1 | **L0 storage** | P0 | YAML frontmatter + metadata incl. `source_turn_ids`; soft delete + default filtering; `list_index`; ranked `search`; **clean-remove `optimize()`** from the `MemoryBackend` protocol (`scoped_memory.py`), the `ScopedMemory` forwarder (`:71`), the in-memory backend, and the no-op test — it has no third-party users, so no compat shim | Metadata round-trips; invalid hidden; restore works; no `optimize()` on the backend |
| 2 | **L1 operation service** | L0 | `apply(ops, dry_run)` + audit + `requested_by`; `touch_last_used`; `search_candidates` | Dry-run mutates nothing; audit records reason/source/requested_by |
| 3 | **L6a read enhancements** *(∥)* | L0 | Index in cached prefix; auto-recall interceptor; per-session maxim snapshot + breakpoint — **write tools kept** | Maxim block byte-identical across a session; no no-learning gap |
| 4 | **L3 lifecycle** *(∥)* *(BEP 11)* | — | `session_phase` in metadata; `LifecycleBus` events w/ `base_revision` | Phase correct for new/resumed/continuing |
| 5 | **L2 BackgroundLLM** *(∥)* *(BEP 11)* | harness | provider-level structured-output call, no chat persistence | Schema-valid output, zero session side effects |
| 6 | **L4 JobRunner** *(BEP 11)* | L2, L3 | durable jobs + idempotency + retry; **manual + dry-run first**, then triggers; `drain()` | Trigger→one run; flush-on-close loses nothing; retry no dupes |
| 7 | **L5 consolidation v1** | L0,L1,L2,L4 | `propose()` → ADD/UPDATE/INVALIDATE/NOOP applied via L1 | With `auto_apply=true` (or a manual/admin apply), a mid-chat fact persists into a *new* session; all writes off-turn + audited |
| 8 | **L6b write-tool removal** | L5 | only `Recall`; `ReviseMaxim` deleted; forget → ephemeral suppression + bg invalidation | No write tool registered; forget suppresses immediately *and* invalidates |
| 9 | **L7 admin** | L1,L4,L5 | `boscli memory list/show/index/recall/consolidate(--dry-run)/restore/audit/jobs/telemetry` | Each works live; restore reverses a soft delete; dry-run reviewable |
| 10 | **Ranking + increments** | L5,L8 | `importance` + scored ranking (eval-gated); then promote → compact → reflect → link | Ranking beats plain match on eval before trusted |
| 11 | **L8 DSPy/GEPA** | L8,L5 | offline compile; lift prompts into BOS strings | No runtime DSPy dependency |
| — | **Deferred** | — | FTS5 alt backend, vector backend, memory blocks, full dynamic `agent.ask` | — |

---

## 13. Acceptance Criteria

- The agent has no memory write tool; `Recall` is the only memory tool registered (after P8).
- A maxim is never written during a turn; all maxim/memory writes originate from the off-turn handler via the operation service, and are audited with `reason` + `requested_by` + `source_turn_ids`.
- Within a session with no consolidation write, the system-prompt maxim block is byte-identical across all turns and iterations (cache holds); maxims are read from storage at most once per session.
- A directive stated mid-chat is honored for the rest of the session without any maxim write, and — once consolidation **applies** it (`auto_apply=true` or a manual/admin apply; `auto_apply` defaults `false`) — persists into a *new* session.
- A user "forget" suppresses immediately in-session (ephemeral) **and** invalidates off-turn, tagged `requested_by="user"`.
- Soft delete is reversible (`restore`); hard purge only after `retention_days`.
- `TurnContext.metadata["session_phase"]` is present and correct for new / resumed / continuing turns.
- The write tools are removed only after the consolidation handler exists (no no-learning window).
- The component eval exists and reports routing accuracy + retrieval recall@k.

## 14. Decisions and Open Questions

**Resolved (2026-06-14 reconciliation):**

- **Orchestration = Model B.** The memory plugin owns the job body; the harness provides generic infra only (L2/L3/L4).
- **Background LLM = provider-level `BackgroundLLM`**, not a faked `Agent.ask`.
- **Consolidation contract = a real handler** (`propose` + operation-service `apply`); the backend `optimize()` is **removed**; the trigger is infra.
- **Memory operation service (L1)** is the single validated/audited/dry-run write door; it is memory-domain (swaps with the plugin).
- **Lifecycle transport = `LifecycleBus`** (not an interceptor stage or envelope).
- **Config ownership** — generic job infra harness-level; memory policy (incl. `auto_apply`, default `false`) on the plugin under `[exts.ep_plugin.MemoryPlugin]`.
- **Persistence is silent** (no turn-event); observability via the audit log + job logs.
- **Hard/secure deletion is out of scope** (separate service); BEP 10 does soft-invalidate + retention purge.

**Open:**

1. **Job storage / infra lib** — homegrown asyncio runner for v1 (zero external infra); evaluate APScheduler (`AsyncIOScheduler` + sqlite jobstore) for the durable + cron layer when it arrives. (BEP 11 owns this.)
2. **Writer naming** (cosmetic) — name the memory writer to avoid the `ep_consolidator` collision (e.g. `MemoryCurator`/`MemoryWriter`). Shape is decided; only the identifier is open.
3. **Compaction fold (BEP 5 follow-up)** — once `BackgroundLLM` lands, fold chat compaction into `ChatStore` (delegating the LLM transform to `BackgroundLLM`), retiring the standalone `ep_consolidator`. Not renaming it in the interim; it is retired by the fold. Memory consolidation never uses it (Model B).
4. **BEP 1 reconciliation** — mark superseded-in-part (tool surface); wire in or retire the "USING YOUR MEMORY" prompt pending the eval.

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-13 | Initial draft | Capture the platform-managed memory design; supersede BEP 1's agent tool surface |
| 2026-06-14 | Flows, layers, bottom-up plan (reconciled from `docs/debates/`) | Resolve runtime shape (Model B + provider-level `BackgroundLLM`), add the memory operation service + provenance/audit + forget flow, fix config keys (`[exts.ep_plugin.MemoryPlugin]`), depend on BEP 11 for generic infra, remove `optimize()` in favor of a handler + infra trigger, renumber sections cleanly |
