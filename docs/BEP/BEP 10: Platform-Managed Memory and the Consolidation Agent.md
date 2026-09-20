# BEP 10: Platform-Managed Memory and the Consolidation Agent

Status: **partly implemented.** The storage/capture/read track (§11 phases 0–3: L0 backend, L1
operation service, L6 capture+read enhancements) is built. Consolidation (§4) is built as an
**on-demand run** invoked by `boscli memory consolidate`; the automatic triggers this BEP originally
specified are **withdrawn** — see the note below.

> **2026-09-19 — off-turn automation withdrawn.** This BEP was written against BEP 11 (Async Tasks &
> Scheduling), which supplied a `JobRunner`, a `LifecycleBus` and a `BackgroundLLM`. Of those, only
> the event bus survives (as the harness `EventBus`, which the recall-log flush of §6 uses). The
> `JobRunner` and its `session_close`/`idle` triggers have been **deleted**: consolidation was their
> only consumer, it was off by default, and the queue, its trigger bindings and its
> `status`/`list`/`retry`/`cancel` surface never earned their complexity. `BackgroundLLM` was never
> built — BEP 12's `AgentRunner` took its place. Consolidation now runs **in-line, on demand**, from
> the CLI (§4). Every "trigger", "job", and "durability" claim below has been rewritten to match;
> nothing about §1–§3, §5–§10 storage/capture/read semantics changed.

Background: a verified survey of MemGPT/Letta, Mem0, Zep/Graphiti, Generative Agents, A-MEM, plus Hermes
and Claude Code informed the decisions below.

Extends/supersedes BEP 1 (Memory Enhancement Design): BEP 1's two-tier maxim/memory model,
`pep_memory_backend`, and markdown default are retained. Its `Remember` and `ReviseMaxim` tools are
**kept as append-only capture tools** (§3); its `Forget` tool is **removed** in favor of off-turn,
reversible invalidation (§1.3, §6).

Depends on: the harness `EventBus` (for the §6 recall-log flush on `turn_complete`), `ChatStore`
revision-window reads (BEP 5 amendment, §4), and `AgentRunner` (BEP 12) for the consolidation agent.
BEP 10 defines no scheduler of its own — and, after the withdrawal above, consumes none either.

---

## Core Insight

Memory writes split into two kinds of work:

- **Capture** — appending what is worth keeping. Cheap, intent-rich, and best done *in-turn*: the
  conversational agent has the live context to decide whether something matters, can `Recall` to check
  for conflicts, and can clarify with the user before committing.
- **Curation** — routing maxim-vs-episodic, resolving contradictions, promotion, compaction, linking,
  and forgetting. This is quality-sensitive *judgment*, and the strongest agent-memory systems
  (Letta sleep-time, Mem0's update phase) move it **off-turn**, where a focused process sees the full
  transcript and all existing memories.

BEP 10 keeps these on separate clocks:

> The conversational agent **captures** by appending — `Remember` (episodic) and `ReviseMaxim` (a maxim
> note) — and **reads** via `Recall`/auto-recall. It never *curates*. Routing, contradiction resolution,
> promotion, compaction, and forgetting are performed off-turn by a platform-scheduled **consolidation
> handler** owned by the memory plugin. The storage backend is the source of truth; the system prompt is
> a per-turn **projection** of it.

This keeps the hard decisions off the latency path, removes per-iteration prompt-cache churn, and makes
the curation policy a single, evaluable, optimizable component — without giving up the agent's ability
to capture with intent.

## Goals

1. Keep the conversational agent's memory surface **append + read** (`Remember`, `ReviseMaxim`, `Recall`, plus auto-recall and an in-context index); move all **curation** off-turn.
2. Replace the no-op `optimize()` with a real **off-turn consolidation handler** (extract, route, resolve, promote, compact, reflect, link) run as a background job.
3. Define a **cache-aware maxim lifecycle**: the maxim block is rebuilt per turn (stable within a turn), with cross-turn efficiency from content-addressed prompt caching — no per-session snapshot, no mid-turn mutation.
4. Replace hard delete with **soft, reversible forgetting**, performed off-turn, with operation provenance.
5. Upgrade retrieval from substring match to an **in-context index + lexical + scored ranking** — no vectors by default.
6. Establish an **evaluation + offline prompt-optimization** path (component eval → DSPy/GEPA), without runtime coupling.

## Non-Goals

- **Vector/embedding retrieval by default.** Reserved for a swap-in backend at scale only (§10). The personal-memory regime uses lexical + in-context index.
- **Defining a scheduler, queue, or job runner.** Consolidation is invoked; repeat runs are an
  external scheduler's concern (cron/systemd/launchd calling the CLI). If BOS ever needs an
  in-process execution layer again, it gets its own BEP.
- **Coupling to the Obsidian application** (REST/MCP). Obsidian is a transport/export concern handled by an external skill (§9), never a backend.
- **Runtime dependency on DSPy.** DSPy/GEPA is an *offline* optimizer; compiled artifacts are lifted into BOS prompt strings (§8).
- **Secure/irreversible deletion.** Soft-invalidate + retention purge only; privacy/GDPR-style hard erasure is a separate dedicated service.
- **Memory blocks / multi-agent shared memory.** Deferred until BEP 2 (named actors) needs it.
- **A graph database.** Links are an adjacency list in entry metadata (§6), not a graph store.

## Source-of-Truth and Ownership

| Concern | Source of truth | Owner |
|---|---|---|
| Episodic memories & maxims (durable) | memory **backend** (`pep_memory_backend`) | memory plugin |
| Within-session directives | **chat history + compaction summary** (`ChatStore`) | platform |
| System-prompt maxim block | a **projection** of the backend, rebuilt per turn | agent runtime |
| Memory **capture** (raw append) | — | conversational **agent** (`Remember` / `ReviseMaxim`) |
| Memory **curation** (route/resolve/promote/compact/forget) | — | **memory consolidation handler** (off-turn), via the operation service |
| Memory **reads** | — | conversational **agent** (`Recall` / auto-recall) |
| Operation provenance & audit | memory operation service | memory plugin |
| When consolidation runs | — | **the invoker** — `boscli memory consolidate`, or an external scheduler calling it |
| Consolidation watermark (last-handled revision per chat) | memory plugin store | memory plugin |
| Recall log (surfaced entries) + `last_used` recency | memory plugin store | memory plugin |

---

## 1. Flows

Five flows the system must support. Layer tags `[Ln]` refer to §2.

### 1.1 Recall (foreground, read-only)

1. The maxim block + in-context index are rebuilt once at turn start and held constant for the turn `[L6]`.
2. Auto-recall retrieves on the incoming message, injects top-k as ephemeral context **after** the cache breakpoint `[L6]`.
3. The in-context index (`id` + tags + one-line summary) sits in the cached prefix; the agent may `Recall(entry_id)` for full content or `Recall(query)` to search `[L6]`.
4. The agent answers, treating recalled memory as context, not proof. `last_used` is updated off-turn from a recall log (§6) — never by a mid-turn write `[L1]`.

### 1.2 Durable preference/fact (agent appends, off-turn curation)

1. The user states a durable fact. The agent may `Recall` to check for conflicts and clarify intent with the user in-turn.
2. The agent **appends** it — `Remember` (episodic) or `ReviseMaxim` (maxim note). This is a raw, uncurated write to the backend; the agent does not route, dedup, or resolve contradictions. Persistence is silent (no turn-event); the tool result confirms to the agent `[L6/L0]`.
3. The append is visible at the **next turn** (projection rebuild, §5) — same chat, and other chats on their next turn.
4. At a trigger boundary, the off-turn consolidation handler receives the committed transcript window + existing memories (including the raw append) and curates: route maxim/episodic, resolve (ADD/UPDATE/INVALIDATE/NOOP), promote, compact maxim notes, link `[L4/L5]`.
5. Curated results persist cleanly across sessions; the raw append is deduped/folded in place.

The audit log and admin tooling (§1.5) record what was captured and how it was curated.

### 1.3 Forget / stop using

There is no destructive agent tool. "Stop using X" is captured and curated, not deleted in-turn:

1. The agent appends a negation — `Remember("X is no longer true / do not reference X")` — confirming intent with the user where ambiguous `[L6]`.
2. The negation is in chat history, so the agent does not re-surface X for the rest of the session.
3. Off-turn, the consolidation handler reconciles the negation against the contradicted entry and issues an **INVALIDATE** via the operation service, tagged `requested_by="user"` — distinct from model-inferred contradiction invalidation `[L1/L5]`.
4. Invalid entries drop out of recall/index on commit; the contradicted fact stops surfacing in new sessions `[L0]`.
5. Admin may restore within retention (§1.5).

`requested_by ∈ {user, consolidator, admin, retention}` is preserved on every invalidation so user
intent is never silently overridden by a later model inference, and vice-versa. (A dedicated in-session
suppress-list for the window before consolidation runs is out of scope for v1; immediate suppression
rests on conversation context + the negation memory.)

### 1.4 Off-turn curation (on demand)

1. `boscli memory consolidate --chat <id>` (or `--all`) is invoked — by an operator, or by an external
   scheduler on a timer `[L7]`.
2. The run reads the chat's current revision; if it is at or behind the chat's **watermark** there are
   no new turns and it returns without calling a model. The watermark — not a queue — is what stops a
   window being consolidated twice `[L5]`.
3. It loads the committed transcript window past the watermark (`ChatStore.get_messages_since`) +
   existing memories (including raw agent appends) + active maxims `[L0]`.
4. The consolidation handler runs a disposable agent (`AgentRunner`, BEP 12) with a `response_schema` to
   emit **structured operations** — ADD / UPDATE / INVALIDATE / PROMOTE / LINK / NOOP, each with
   `reason` + `source_turn_ids` `[L5]`.
5. The operation service validates + applies, writes an audit record `[L1]`.
6. The watermark advances **only** on a trustworthy proposal. A failed or unparseable proposal raises
   and leaves the watermark in place, so those turns are retried on the next run rather than burned.
7. Applied changes surface on the **next turn** that rebuilds the projection (§5).

### 1.5 Admin / operator (platform-owned, not routed through the agent)

```
configure   consolidation: model                                                  [config]
inspect     boscli memory list | show <id> | index   # importance, valid, links, source turns  [L7→L0]
search      boscli memory recall "<query>"            # what the agent would retrieve            [L7→L0]
run now     boscli memory consolidate --chat <id>             # propose + apply (unprocessed turns) [L7→L5]
scan        boscli memory consolidate --all                   # every chat past its watermark; cron-able [L7→L5]
dry-run     boscli memory consolidate --chat <id> --dry-run   # NOT BUILT — review ops before apply [L7→L5]
recover     boscli memory restore <id>                # reverse a soft delete within retention    [L7→L1]
audit       boscli memory audit <filter>              # reason / requested_by / result            [L7→L1]
observe     telemetry: recall-hit-rate, dead-memory ratio, maxim churn, cost, failure-rate        [L7→L1]
```

Audit is first-class and built: an operator can trace *why* any entry exists or was invalidated.
Dry-run is **not built** — the surviving `consolidate` command always applies. It stays listed here
because reviewing proposals before trusting them is still the intent; it is a real gap, not a
description of today's CLI.

---

## 2. Layers and Ownership

A layer may depend only on layers below it. **Generic infra is harness-owned and memory-agnostic;
the memory domain is plugin-owned and swappable; config travels with the owner.** Per the **Model B**
decision, the memory plugin owns the consolidation *body* — the harness provides only generic
mechanism (today: the `EventBus` and `AgentRunner`).

| Layer | Owns | Memory-aware? | Config home |
|---|---|---|---|
| **L0** Storage backend | durable primitives, metadata, soft delete | yes | `[exts.pep_memory_backend.<impl>]` |
| **L1** Memory operation service | validated/audited/dry-run **curation** writes; `requested_by`; `last_used` | yes | (code) + plugin policy |
| **L2** `AgentRunner` *(BEP 12)* | one disposable, history-less agent run with structured output | **no** | (infra) |
| **L3** `EventBus` | `turn_complete`/`session_close` fan-out (actor-produced); used by the §6 recall flush | **no** | (infra) |
| ~~**L4** `JobRunner`~~ | ~~queue, triggers, idempotency, retry~~ — **withdrawn** (see header). Repeat runs are an external scheduler's job. | — | — |
| **L5** Consolidation handler | `propose()` → structured operations | yes | `[exts.ep_plugin.MemoryPlugin.consolidation]` |
| **L6** Capture + read path | index, append/read tools, auto-recall, per-turn maxim rebuild | yes | `[exts.ep_plugin.MemoryPlugin]` (+ per-agent `plugin-bindings`) |
| **L7** Admin surface | `boscli memory …`, audit, telemetry | yes | — |
| **L8** Eval & offline opt | component eval, DSPy/GEPA | — | — |

**Capture vs curation writes.** Agent capture (`Remember`/`ReviseMaxim`) is a raw append directly to L0.
The L1 operation service governs **curation** writes — every off-turn ADD/UPDATE/INVALIDATE/PROMOTE/LINK
— so they are validated, audited, and provenance-tagged.

**The contract.** The harness exposes L2/L3 to plugins via `PluginServices` (`agent_runner`, `events`,
alongside `llm` and `chat_store`). The memory plugin is a *consumer*: in `setup(services)` it subscribes
to `services.events` for the recall flush, and its consolidation run uses `services.agent_runner` plus
its own L1 operation service over its L0 backend. Neither harness service names "memory" — so
consolidation *policy* (which model) lives **with the plugin**. Swap the memory plugin → its config
swaps with it; the harness infra config stays valid.

```
┌──────────────────────────────────────────────────────────────┐
│ L7  Admin surface  (boscli memory consolidate — the invoker)  │
│ L5  Consolidation run (window → propose → apply → watermark)  │
├──────────────────────────────────────────────────────────────┤  harness (generic infra)
│ L2  AgentRunner (BEP 12)     L3  EventBus                     │
├──────────────────────────────────────────────────────────────┤
│ L1  Memory operation service (validated/audited)              │
│ L0  Storage backend (primitives, metadata, soft delete)       │  ← L6 capture+read depends only on L0
└──────────────────────────────────────────────────────────────┘
(L8 evaluation & offline optimization sits above L7.)
```

---

## 3. Agent Tool Surface — extends BEP 1

The conversational agent is registered with **three** memory tools. **Capture is append-only; curation
is off-turn.**

- **`Recall`** — read. `query` (lexical search → ranked snippets) or `entry_id` (fetch full entry). Unchanged read semantics (`plugin.py:205`).
- **`Remember`** — append a raw episodic entry (`plugin.py:152`). The agent uses live context to decide *whether* to capture and to clarify intent with the user; it does **not** route, dedup, or resolve contradictions — that is the consolidation handler (§4). A "stop using X" request is captured as a negation append (§1.3).
- **`ReviseMaxim`** — append a timestamped note to a maxim key (`plugin.py:174`). The maxim analog of `Remember`: in-turn capture of a behavior-shaping directive, bounded by the 2048-char cap. Appends accumulate notes; the consolidation **Compact** step (§4) folds them into clean prose off-turn — this is the "merge cycle" the cap message has always promised (`plugin.py:198`) and which now actually exists.

Removed:

- **`Forget`** — destructive deletion is removed from the agent surface. Forgetting is soft, reversible, and off-turn (§1.3, §6): the agent appends a negation; the consolidation handler INVALIDATEs via the operation service with `requested_by="user"`.

Reads are additionally served *without* a tool call by:

- **Auto-recall** — a turn interceptor (`prepare`/`before_llm`) retrieves on the incoming message and injects top hits as ephemeral context (after the cache breakpoint, §5).
- **In-context index** — a compact catalog (`id` + tags + one-line description) of available memories, rendered in the system prompt via `get_system_prompt_section`; the agent fetches full content by `entry_id`.

The read enhancements (index, auto-recall, ranking) and the off-turn curation ship independently (§11).

---

## 4. Consolidation: handler + trigger

`optimize()` (today a no-op, `markdown_backend.py:123`) is **removed**, not renamed. Storage must not
own consolidation orchestration. Consolidation is a **real handler** owned by the memory plugin,
invoked by generic infra triggers — *not* a method on any storage contract.

### Contract shape (a handler, not a trigger function)

```python
class MemoryConsolidator(Protocol):                  # final name per §13; e.g. MemoryCurator
    async def propose(self, request: MemoryConsolidationRequest) -> list[MemoryOperation]: ...
```

- `propose()` is the **decision** (structured operations); the operation service `apply()` is the
  **write**. Manual/admin "run now" = `propose` + `apply`; dry-run = `propose` + `apply(dry_run=True)`;
  triggered runs = the same, wrapped in a job.
- The request carries the transcript window, existing memories (including raw agent appends since the
  watermark), active maxims, policy, and trigger metadata. The response is **structured operations** —
  string summaries are insufficient for writes.

### Data contracts (operations, request, operation service)

The handler's output and the operation service's input are the same type — a list of `MemoryOperation`.
The LLM emits these as structured output (`AgentRunner`'s `schema`, BEP 12); the operation
service validates and applies them. Each op is a flat object so it maps cleanly to a JSON schema.

```python
MemoryOpKind = Literal["ADD", "UPDATE", "INVALIDATE", "PROMOTE", "LINK", "NOOP"]
RequestedBy  = Literal["user", "consolidator", "admin", "retention"]

@dataclass(frozen=True)
class MemoryOperation:
    op: MemoryOpKind
    reason: str                          # audited rationale (required for every op, incl. NOOP)
    source_turn_ids: list[str] = ()      # provenance into the transcript window
    target_id: str | None = None         # UPDATE / INVALIDATE / PROMOTE / LINK — existing entry
    content: str | None = None           # ADD / UPDATE — episodic text
    summary: str | None = None           # ADD / UPDATE — one-line for the in-context index (§6)
    tags: list[str] | None = None        # ADD / UPDATE
    importance: int | None = None        # ADD / UPDATE — 1..10
    maxim_key: str | None = None         # PROMOTE — target maxim (user|self|rules)
    links: list[str] | None = None       # LINK — entry-ids to attach
    requested_by: RequestedBy = "consolidator"   # INVALIDATE — "user" for an explicit forget
```

Per-op required fields (validated at L1):

| op | requires | effect |
|---|---|---|
| ADD | content | new episodic entry (`valid`, `importance`, `source_turn_ids` stamped) |
| UPDATE | target_id + (content / tags / importance) | revise an existing entry in place |
| INVALIDATE | target_id (+ requested_by) | soft-delete; the off-turn forget (§6) |
| PROMOTE | target_id + maxim_key | append the entry's gist as a note on the named maxim; **Compact** folds it later |
| LINK | target_id + links | attach adjacency refs (§6) |
| NOOP | — | record that the handler considered and declined (kept for audit + eval) |

The request the handler receives:

```python
@dataclass(frozen=True)
class MemoryConsolidationRequest:
    chat_id: str
    actor_name: str | None
    scope: str                              # memory scope being curated
    base_revision: int                      # ChatStore head at read — bounds the window, advances the watermark
    transcript_window: list[Message]        # committed turns (watermark, base_revision]
    candidate_memories: list[MemoryEntry]   # existing in-scope entries to reconcile against (working set)
    active_maxims: dict[str, str]           # maxim key -> current text
```

Implementation note: the shipped request carries no `trigger` (there is only one way in) and no
`policy`. A separate `raw_appends` field was specified — the agent's uncurated `Remember` captures, to
be *folded* rather than *reconciled against* — but was never populated distinctly; captures reach the
handler inside `candidate_memories` like any other entry. Restoring the distinction is open work, not
a shipped behaviour.

The operation service (L1) is the single write door for curation:

```python
@dataclass(frozen=True)
class AuditRecord:
    op: MemoryOperation
    result: Literal["applied", "dry_run", "rejected", "noop"]
    entry_id: str | None                    # affected/created entry
    at: str                                 # ISO timestamp
    error: str | None = None                # set when result == "rejected"

class MemoryOperationService(Protocol):
    async def apply(self, ops: list[MemoryOperation], *, dry_run: bool = False) -> list[AuditRecord]: ...
    async def search_candidates(self, query: str, *, top_k: int) -> list[MemoryEntry]: ...
    async def touch_last_used(self, entry_ids: list[str]) -> None: ...   # off-turn, from the recall log
    async def restore(self, entry_id: str) -> None: ...
    async def audit(self, *, filter: dict | None = None) -> list[AuditRecord]: ...
```

`apply` validates each op (required fields, `maxim_key` in the allowed set, `target_id` exists and is in
scope), applies it via the L0 backend, appends an `AuditRecord`, and emits a memory-changed event. With
`dry_run=True` it validates and returns records with `result="dry_run"`, mutating nothing — backing the
admin dry-run (§1.5). Raw agent appends bypass this service (they write straight to L0, §2); the service
governs **curation** writes only.

The handler performs, over the request:

1. **Extract** — candidate facts worth keeping that the agent did **not** already capture (complementary to the raw appends).
2. **Route** — maxim vs episodic (function, not content: behavior-shaping/always-visible → maxim). When routing (or PROMOTE-ing) to a maxim, the handler picks the target key from the configured `maxims` set by **function**, using each key's scope description (`_MAXIM_DESCRIPTIONS`, `plugin.py:33`) — e.g. a hard constraint → `rules`, a durable user preference → `user`; if no key clearly fits, the fact stays episodic.
3. **Resolve** — Mem0-style ADD / UPDATE / INVALIDATE / NOOP over the top-`s` similar existing memories, reconciling both extracted candidates and the agent's raw appends. **INVALIDATE is the off-turn forget.**
4. **Promote** — episodic → maxim when a durable pattern is observed.
5. **Compact** — fold maxim revision notes (from `ReviseMaxim` and prior UPDATEs) into clean prose near the 2048-char cap (the real "merge cycle").
6. **Reflect** — synthesize higher-level memories when accumulated importance crosses a threshold (Generative Agents).
7. **Link** — attach related entries (§6).

v1 scope is **Resolve (ADD/UPDATE/INVALIDATE/NOOP)** over episodic memories — reconciling raw appends and
extracted candidates — plus **Compact** of maxim notes; promote / reflect / link are independent,
config-gated increments (§11).

### Engine layering (distinct roles — do not conflate)

- **Invoker** = `boscli memory consolidate` (or an external scheduler calling it). Not a memory method.
- **Run** = `run_consolidation()` (memory plugin, L5): window → propose → apply → advance watermark.
- **Handler** = `MemoryConsolidator.propose()` (memory plugin, L5).
- **Write** = the memory **operation service** (L1) — validated, audited.
- **LLM** = **`AgentRunner`** (L2, BEP 12): a disposable, history-less agent run with structured output
  — a fresh internal chat-id, no parent turn, no session mutation. The memory plugin owns the prompts.
- **DSPy/GEPA** (§8) = the **offline** prompt compiler. Never in the hot path.

Beware the look-alike: `ep_consolidator` (`LLMConsolidator`) is the **chat-history summarizer** used
for context compaction and turn handoff. Memory consolidation is a different thing and does **not**
use it (Model B). See §13.

### Invocation and concurrency

- **Consolidation is never triggered by the runtime.** It runs when `boscli memory consolidate` is
  invoked — never inside an agent turn, never at session close, never on a timer BOS owns. Nothing in
  the response path waits on it, and no process holds a queue of pending consolidations.
- **Nothing is lost by not auto-running.** Raw agent appends are persisted immediately, so they are
  recalled in later runs regardless; the watermark keeps every un-consolidated turn eligible until a
  run processes it. Deferring consolidation delays curation, it does not drop input.
- **Re-running the same window is prevented by the watermark**, not by a dedup key: a run whose
  `base_revision` is at or behind the watermark returns before calling a model.
- **Concurrency is the invoker's responsibility.** A single `--all` invocation walks chats
  sequentially. Two *concurrent* invocations over the same chat can both read the same window and both
  consolidate it — the watermark file is written atomically, so nothing corrupts, but duplicate near-
  facts can result; the *next* run's Resolve step folds them (UPDATE/INVALIDATE). Dedup is eventual,
  never corrupting. Do not run two scheduled scans that overlap.

### Incremental processing (watermark) and the scan trigger

The handler processes only **unprocessed turns**, tracked by a per-chat **watermark = last-handled
revision**, owned by the memory plugin (the consumer's checkpoint — not chat content, not generic
`ChatStore` state). A run for chat *C* reads turns `(watermark, current]` (via the ChatStore
revision-window read, BEP 5 amendment), runs the handler, and on success advances the watermark to
`current`.

This makes a **scan** mode possible: *for every chat where `current_revision > watermark`, consolidate
the unprocessed window.* The scan is the shared core behind both delivery mechanisms:

- **Manual** — `boscli memory consolidate [--chat C | --all]` (admin "run now").
- **Scheduled** — the *same* scan invoked by an **external scheduler** (cron/launchd/systemd) on an
  interval (e.g. every 15 min). Each scheduled invocation is its own short-lived process that exists
  *to do* the work — no daemon, nothing to drain, nothing blocking an exit.

The watermark is what makes the scan incremental and dedup correct at the input boundary; the handler's
resolve step still reconciles each new turn against existing memories. BOS deliberately owns **no**
internal interval trigger: a timer that only ever calls a CLI command is the scheduler's job, not the
agent runtime's.

---

## 5. Maxim Lifecycle and Cache Discipline

Three independent clocks. Earlier thinking conflated them; keeping them separate is what makes the
design simple.

| Clock | Controls | Cadence |
|---|---|---|
| **Capture** | agent append to the backend (`Remember` / `ReviseMaxim`) | in-turn, immediate |
| **Curation** | off-turn consolidation writes (route/resolve/promote/compact/forget) | off-turn (`session_close` / `idle` / `manual`) |
| **Projection (load)** | when the system prompt reflects the backend | **per turn** |

### In-chat vs cross-chat

- **In-chat immediacy is free.** A directive the user states ("from now on answer in Portuguese") is already in the message history; the model honors it for the rest of the session with **no write needed**.
- A maxim's unique job is **cross-session persistence**. A maxim-worthy directive is captured in-turn via `ReviseMaxim` (append) and curated off-turn.

### Loading cadence (decided)

- The maxim block (and the in-context index) is rebuilt **once at `ask()` start** and held constant for **all iterations of that turn**.
- **Never per-iteration.** Today `_build_system_prompt()` runs at `ask()` start (`agent.py:375`) **and again every loop iteration** (`agent.py:513`), re-reading maxims each time — a live mid-turn cache bust. Remove the per-iteration maxim/index read; keep per-iteration rebuild only for genuinely volatile sections (e.g. skills loaded mid-turn via `LoadSkill`).
- **No cross-turn memoization, no per-session snapshot.** The prefix is rebuilt each turn, and the provider's **content-addressed** prompt cache makes that free when nothing changed: a byte-identical prefix re-hits the cache. A turn boundary busts the cache only when the backend actually changed since the previous turn — a single prefix reprocessing, amortized across the turn's iterations and generation. With multi-iteration turns this cost is negligible, and learning lands at the **next turn** rather than the next process.
- Cross-*session* cache reuse is not a goal — the provider cache TTL is short (≈5 min), so a per-session snapshot would buy almost nothing while costing real complexity. Within-turn byte-stability is the property that matters, and per-turn rebuild preserves it.

### Prompt layering (cache breakpoint after maxims)

```
[ base prompt + tool defs ]    stable                ┐ cached prefix
[ in-context index ]           rebuilt per turn       │ (stable within a turn;
[ maxims ]                     rebuilt per turn       ┘  re-hits across turns when unchanged)
─────────────── cache breakpoint ───────────────
[ skills loaded mid-turn, system info / time ]    volatile
[ ephemeral "active directives (this session)" ]  volatile, append-only
[ auto-recall hits (this message) ]               volatile
[ chat history … current message ]                append-only
```

- Stable-within-turn maxims and the index live **before** the breakpoint; anything that changes within a turn lives **after** it.
- **"Active now" mid-session, beyond what the messages carry → ephemeral block after the breakpoint** (or a message), never a maxim mutation. `TurnContext` already supports this: `ephemeral` (`agent.py:71`) + `set_ephemeral_message` (`agent.py:98`).

### History and incremental caching

- History is already loaded **once per turn** (`agent.py:368`), not per iteration — no frequency change needed.
- `get_context()` must return an **append-only** serialization (prior turns byte-identical, only new messages at the tail) so the provider caches the growing message prefix; cache busts only at deliberate **compaction boundaries**. Verify `_load_and_compact_history` (`agent.py:707`) does not perturb earlier bytes.

---

## 6. Retrieval, Forgetting, and Links (default backend)

### Retrieval — in-context index + lexical + scored ranking (no vectors by default)

`search_memories` (`markdown_backend.py:81`, substring + ctime) is replaced by, in ascending order of N:

1. **In-context index** — catalog surfaced in the prompt; zero recall-miss at small N. It is **bounded** by a configurable size budget (`index_max`, §7): when memories exceed it the index holds the top entries by `importance` (a stable key — not recency, see Recall log above) and the overflow stays reachable via `Recall(query)`. Large N thus degrades from zero-miss index to search-only, never to data loss.
2. **Lexical search** — agentic/iterative via `Recall` (ranked substring at small N; **FTS5/BM25** is a deferred alternate backend at scale, §10).
3. **Scored ranking** over lexical candidates (Generative Agents signals, orthogonal to vectors):
   ```
   score(m,q) = w_rec · 0.99^hours_since(m.last_used) + w_imp · importance(m)/10 + w_lex · lexical(q,m)
   ```
   `importance` (1–10) is assigned **by the consolidation handler at write time** and stored in `MemoryEntry.metadata`. Weights start at 1, terms normalized to [0,1]. Ranking is **eval-gated** — it must beat plain match on the §8 eval before `importance` is trusted.

### Recall log and `last_used` (off-turn recency)

The recency term `last_used` (above) and the read telemetry (§8) both need to know **which entries were
surfaced, and when** — without a mid-turn entry write (§1.1). A small **recall log** owned by the memory
plugin provides this:

1. **During the turn (zero IO, zero entry writes):** the read path records surfaced entry-ids on `TurnContext.metadata["recalled"]` — auto-recall appends its injected hits, the `Recall` tool appends what it returned. In-memory only; it touches neither the backend nor the cached prefix.
2. **At `turn_complete` (off-turn, harness `EventBus`):** a memory-plugin subscriber (a) appends the surfaced events to the durable recall log and (b) calls `operation_service.touch_last_used(entry_ids)` to refresh recency. Both run after the response is committed — off the user's latency path. In a one-shot CLI run `turn_complete` fires before exit, so recency is still recorded; a hard kill loses only soft ranking signal, never durable memory.

Record shape (append-only JSONL in the memory store dir):

```python
@dataclass(frozen=True)
class RecallEvent:
    at: str                 # ISO timestamp
    chat_id: str
    turn_id: str
    entry_id: str
    source: Literal["auto_recall", "recall_tool"]
    query: str | None = None
```

- **`last_used` never enters the cached prefix.** The in-context index renders `id` + tags + `summary` only and is ordered by a **stable** key (creation order, or `importance` — which changes only at a consolidation boundary), **never** by `last_used`. So a recency touch reorders neither the index nor the maxims — the §5 cache discipline holds. `last_used` affects only the ranked `search` results, which are injected *after* the cache breakpoint.
- **Telemetry (§8)** is derived from the recall log + backend index: *dead-memory ratio* = valid entries with no recall event; *recall-hit-rate* = recall events per surfaced entry over a window. The admin `observe` surface (§1.5) reads these.
- **Write cost.** `touch_last_used` is a per-turn metadata write off the critical path; the default backend rewrites a few entry files, fine at personal scale. A higher-scale backend may batch touches or fold them into the next consolidation run.
- **Retention.** The recall log is rolled up / trimmed by the retention job alongside the invalidated-entry purge (below); only aggregate counters and a recent-events window need to persist.

### Forgetting — soft, reversible, with provenance (Graphiti-style)

- Add `valid` / `invalidated_at` and `source_turn_ids` to `MemoryEntry.metadata`. The off-turn INVALIDATE (via the operation service) sets the flag; `search` filters invalidated entries by default.
- Every invalidation carries `requested_by ∈ {user, consolidator, admin, retention}` — user-requested forget is never silently overridden by model-inferred contradiction.
- A retention job hard-purges entries invalidated beyond `retention_days` (§7). Secure/irreversible erasure is out of scope (a separate service).

### Links — graph-lite (A-MEM / Graphiti BFS)

- Optional `links: list[str]` (entry-id refs) in `MemoryEntry.metadata`. On ingest, the consolidation handler finds candidate neighbors (shared tags / lexical overlap at small N) and the LLM decides which to link. `Recall` may expand one hop. No graph DB.

### Backend protocol (L0) delta

The `MemoryBackend` protocol (`scoped_memory.py:18`) changes to support metadata, ranked retrieval, the
in-context index, soft delete, and the curation operations. Hard `forget_memory` and the no-op
`optimize()` are **removed** (§4, §11 P1).

```python
@dataclass
class MemoryEntry:                          # existing id/content/tags/created_at kept; metadata extended
    id: str
    content: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    metadata: dict = field(default_factory=dict)   # importance, valid, invalidated_at, last_used,
                                                    # links, source_turn_ids, summary (§7)

@dataclass(frozen=True)
class MemoryIndexEntry:                      # the in-context index projection
    id: str
    tags: list[str]
    summary: str                             # stored summary, else truncated content

class MemoryBackend(Protocol):
    # maxims — unchanged
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...

    # capture (raw append) + read
    async def ingest_memory(self, content: str, *, tags=None, importance: int = 5,
                            summary: str | None = None, source_turn_ids: list[str] | None = None) -> str: ...
    async def get_memory(self, entry_id: str, *, include_invalid: bool = False) -> MemoryEntry | None: ...
    async def search_memories(self, query: str, *, top_k: int = 5,
                             include_invalid: bool = False) -> list[MemoryEntry]: ...   # ranked (§6)
    async def list_index(self) -> list[MemoryIndexEntry]: ...                           # valid-only

    # curation writes (driven by the L1 operation service)
    async def update_memory(self, entry_id: str, *, content=None, tags=None,
                            importance=None, summary=None, links=None) -> None: ...
    async def invalidate_memory(self, entry_id: str, *, requested_by: RequestedBy) -> None: ...
    async def restore_memory(self, entry_id: str) -> None: ...
    async def purge_invalidated(self, *, older_than_days: int) -> int: ...               # retention

    # REMOVED: forget_memory (hard delete) ; optimize() (no-op)
```

- `search_memories` / `list_index` / `get_memory` filter `valid=false` by default; `restore_memory` and the admin audit path pass `include_invalid=True`.
- `ScopedMemory` (`scoped_memory.py:28`) wraps the new methods the same way it wraps the old, composing scope-visibility with valid-filtering.
- **Migration:** existing entries lack the new metadata; they are defaulted on read (`valid=true`, `importance=5`, empty `links`/`source_turn_ids`), so no batch migration is required.

---

## 7. Configuration Shape

Config splits by owner (§2): generic infra is harness-level and memory-agnostic; memory policy
travels with the plugin. **Config keys are `[exts.<ep_name>.<impl>]`** — a bare `[exts.MemoryPlugin]`
does **not** resolve (it hits `does not match any registered extension point; ignored`,
`workspace.py:592`); `MemoryPlugin` is an implementation of the `ep_plugin` EP.

```toml
# ── memory domain — travels with the swappable plugin ──
[exts.ep_plugin.MemoryPlugin]
maxims  = ["user", "self", "rules"]   # always-in-prompt keys
scope   = "workspace"
backend = "_default"                              # markdown default; swappable (§10)

[exts.ep_plugin.MemoryPlugin.consolidation]
model = ""                          # consolidation agent's model; else BOS_CONSOLIDATOR_MODEL
# No `enabled` / `trigger` key: consolidation runs only when invoked (§4).
# `retention_days` was specified here for purging soft-deleted entries; the purge
# is NOT built (`purge_invalidated` has no caller), so the key is not accepted.
# `auto_apply` is likewise gone: a run always applies, because the operator chose to run it.

[exts.ep_plugin.MemoryPlugin.retrieval]
auto_recall     = true
index_in_prompt = true
index_max       = 50                # cap entries in the in-context index; overflow falls back to Recall search
top_k           = 5
```

New `MemoryEntry.metadata` fields: `importance: int (1-10)`, `valid: bool`, `invalidated_at: str|None`,
`last_used: str|None`, `links: list[str]`, `source_turn_ids: list[str]`. Persisted via **YAML
frontmatter** in the default backend (Obsidian-compatible, §9). (`metadata` already exists,
`scoped_memory.py:15`, but the markdown backend has no metadata round-trip today — adding it is the first
implementation step, §11.)

---

## 8. Evaluation and Offline Optimization

**Finding to act on:** BEP 1's rich "USING YOUR MEMORY" prompt is **not wired in** — the running code
uses the terse `_MEMORY_PROMPT_SECTION` (`plugin.py:81`). Do not swap it in blind; measure it.

### Evaluation ladder (build cheapest first)

1. **Component eval (highest ROI)** — labeled set: routing `{transcript → action: maxim:key | memory | nothing}` (targeting the *consolidation handler*) + retrieval `{query → relevant ids}` (recall@k / MRR). ~50 cases; runs in seconds; unblocks all prompt iteration. May run in stub mode before the handler exists.
2. **Scenario eval + LLM-judge** — scripted multi-turn conversations; `AgentRunner`/`ep_provider` is stubbable; hang on `tests/test_harness.py`.
3. **Standard benchmarks** — LOCOMO / LongMemEval for headline numbers (conflate retrieval + generation).
4. **Telemetry** — dead-memory ratio (stored-but-never-recalled), recall-hit-rate, maxim churn, consolidation cost, failure rate (surfaced via the L1 audit log).

### DSPy / GEPA (offline only)

Memory decisions map to DSPy Signatures; an optimizer (MIPROv2 / BootstrapFewShot / **GEPA**) compiles
instructions + few-shot demos against the §8 metric + dataset. Precedent:
[`NousResearch/hermes-agent-self-evolution`](https://github.com/NousResearch/hermes-agent-self-evolution)
(DSPy + GEPA). **Rules:** (a) DSPy *consumes* the eval — build it first; (b) run offline and **lift the
compiled instructions/demos into BOS prompt strings**. No runtime dependency.

---

## 9. Obsidian Interop (transport, not backend)

The backend is swappable; Obsidian is **not** a storage tech to swap to (it is a GUI app). Keep BOS
memory as source of truth and move memory to/from Obsidian via an **external skill**: emit YAML
frontmatter (`importance`/`valid`/`invalidated_at`/`tags`/`links`/`source_turn_ids`) and `[[wikilinks]]`.
Because the default backend adopts frontmatter + slugged filenames (§7), the vault is already
Obsidian-compatible and export is near-trivial. The runtime never depends on Obsidian.

---

## 10. Backend Swappability

`pep_memory_backend` makes storage tech a pluggable choice. **Markdown is the default** (right for the
small personal regime). FTS5 and, **only at scale**, a vector DB are alternative backends behind the
same protocol — chosen by config, not bolted onto the default. The decisions above upgrade the default
backend and do not constrain what a deployment swaps in.

---

## 11. Implementation Plan (bottom-up; dependency-ordered)

Each phase rests on layers already built (§2). `[L*]` marks the layer. Phases sharing a row group are
parallelizable.

| # | Phase | Deps | Deliverable | Acceptance |
|---|---|---|---|---|
| 0 | **Lock + seed** | — | Regression tests for current `Remember`/`Recall`/`ReviseMaxim` / maxim injection / backend CRUD / chat persistence; component-eval skeleton (stub mode) | Tests green on current behavior; eval runs against stubs |
| 1 | **L0 storage** | P0 | YAML frontmatter + metadata incl. `source_turn_ids`; soft delete + default filtering; `list_index`; ranked `search`; **clean-remove `optimize()`** from the `MemoryBackend` protocol (`scoped_memory.py`), the `ScopedMemory` forwarder (`:71`), the in-memory backend, and the no-op test — it has no third-party users, so no compat shim (contract: §6 Backend protocol delta) | Metadata round-trips; invalid hidden; restore works; no `optimize()` on the backend |
| 2 | **L1 operation service** | L0 | `apply(ops, dry_run)` + audit + `requested_by`; `touch_last_used`; `search_candidates` (contract: §4 Data contracts) | Dry-run mutates nothing; audit records reason/source/requested_by |
| 3 | **L6 capture+read path** *(∥)* | L0 | Index in cached prefix; auto-recall interceptor; **per-turn maxim rebuild** (remove per-iteration re-read); keep `Remember`+`ReviseMaxim` as appends; **remove `Forget`** | Maxim block byte-identical across a turn's iterations; changes across turns only on backend change; no destructive tool registered |
| 4 | **L3 `EventBus`** *(∥)* | — | `turn_complete`/`session_close` events carrying `base_revision` | Events fire with correct `base_revision` for the chat |
| 5 | **L2 `AgentRunner`** *(∥)* *(BEP 12)* | harness | disposable-agent structured-output call, no parent chat persistence | Schema-valid output, zero session side effects |
| ~~6~~ | ~~**L4 JobRunner**~~ | — | **withdrawn** (see header) — no queue, no triggers, no retry | — |
| 7 | **L5 consolidation v1** | L0,L1,L2 | `run_consolidation()`: window → `propose()` → ADD/UPDATE/INVALIDATE/NOOP + maxim Compact, applied via L1, watermark advanced | A mid-chat fact and a `ReviseMaxim` note are curated by a `boscli memory consolidate` run and persist cleanly into a *new* session; all curation off-turn + audited |
| 8 | **L7 admin** | L1,L5 | `boscli memory list/show/index/recall/consolidate/restore/audit`; still open: `--dry-run`, telemetry | Each works live; restore reverses a soft delete |
| 9 | **Ranking + increments** | L5,L8 | `importance` + scored ranking (eval-gated); then promote → reflect → link | Ranking beats plain match on eval before trusted |
| 10 | **L8 DSPy/GEPA** | L8,L5 | offline compile; lift prompts into BOS strings | No runtime DSPy dependency |
| — | **Deferred** | — | FTS5 alt backend, vector backend, memory blocks, full dynamic `agent.ask` | — |

---

## 12. Acceptance Criteria

- The agent's memory surface is **append + read**: `Remember` (episodic append), `ReviseMaxim` (maxim-note append), `Recall` (read). No destructive `Forget` tool is registered.
- All **curation** — routing, contradiction resolution, promotion, compaction, forgetting — originates from the off-turn handler via the operation service, audited with `reason` + `requested_by` + `source_turn_ids`.
- Within a turn, the system-prompt maxim block + index are byte-identical across all iterations (cache holds within the turn); across turns the block changes only when the backend changed since the prior turn; maxims are read from storage at most once per turn.
- A directive stated mid-chat is honored for the rest of the session; once captured (agent append) it surfaces at the next turn, and once curated off-turn it persists cleanly into a *new* session.
- A user "stop using X" is captured as a negation append and invalidated off-turn, tagged `requested_by="user"`; user intent is never silently overridden.
- Soft delete is reversible (`restore`); hard purge only after `retention_days`.
- The component eval exists and reports routing accuracy + retrieval recall@k.

## 13. Decisions and Open Questions

**Resolved:**

- **Capture stays on-turn; curation moves off-turn.** The agent keeps `Remember` + `ReviseMaxim` (append-only) + `Recall`; `Forget` is removed (negation via `Remember` + off-turn INVALIDATE). Raw appends are persisted immediately and curated off-turn.
- **Projection = per-turn rebuild + content-addressed caching.** The maxim block/index are rebuilt at each turn start; within-turn byte-stability is preserved and the cache re-hits across turns when unchanged.
- **Orchestration = Model B.** The memory plugin owns the consolidation body; the harness provides generic infra only (L2/L3).
- **Off-turn LLM = `AgentRunner`** (BEP 12): a disposable, history-less agent, not a faked `Agent.ask`. (A provider-level `BackgroundLLM` was specified here and never built.)
- **Consolidation contract = a real handler** (`propose` + operation-service `apply`); the backend `optimize()` is **removed**. Invocation is the operator's, not the runtime's.
- **Memory operation service (L1)** is the single validated/audited write door for **curation**; it is memory-domain (swaps with the plugin).
- **Lifecycle transport = the harness `EventBus`** (`turn_complete`/`session_close`), not an interceptor stage or envelope. Only the §6 recall flush consumes it.
- **Config ownership** — generic infra harness-level; memory policy on the plugin under `[exts.ep_plugin.MemoryPlugin]`.
- **Persistence is silent** (no turn-event); observability via the audit log.
- **Hard/secure deletion is out of scope** (separate service); BEP 10 does soft-invalidate + retention purge.

**Open:**

1. **Dry-run and retention purge** — both specified above, neither built. `consolidate --dry-run` has no flag and `purge_invalidated()` has no caller, so `retention_days` does nothing. Build or drop them; do not leave them half-documented.
2. **Writer naming** (cosmetic) — name the memory writer to avoid the `ep_consolidator` collision (e.g. `MemoryCurator`/`MemoryWriter`). Shape is decided; only the identifier is open.
3. **Compaction fold (BEP 5 follow-up)** — fold chat compaction into `ChatStore` (delegating the LLM transform to `AgentRunner`), retiring the standalone `ep_consolidator` and with it the `consolidator` name collision (open issue 2). Memory consolidation never uses it (Model B).
4. **BEP 1 reconciliation** — wire in or retire the "USING YOUR MEMORY" prompt pending the eval (§8).

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-13 | Initial draft | Platform-managed memory: off-turn consolidation handler, operation service, provenance/audit, soft forgetting, BEP 11 dependency |
| 2026-06-17 | Capture/curation split + per-turn projection | Settle the agent surface as append + read (`Remember`/`ReviseMaxim`/`Recall`, no `Forget`); move all curation off-turn; set the maxim projection cadence to per-turn rebuild + content-addressed caching |
| 2026-09-19 | Withdraw off-turn automation | BEP 11 was withdrawn and its `JobRunner` deleted — consolidation was its only consumer, was off by default, and the queue/triggers/`retry`/`cancel` surface never earned their cost. Consolidation is now an on-demand run (`boscli memory consolidate`), repeat runs are an external scheduler's concern, and the `EventBus` survives for the §6 recall flush. Also corrected: `--dry-run`, `auto_apply`, `retention_days` purge and the distinct `raw_appends` input are specified here but were never built — each is now marked as a gap rather than described as shipped. |
