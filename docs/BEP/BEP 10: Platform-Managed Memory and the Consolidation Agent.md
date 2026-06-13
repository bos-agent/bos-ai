# BEP 10: Platform-Managed Memory and the Consolidation Agent

Status: **design** — decisions settled in the 2026-06-13 memory design review; open questions listed below; ready for implementation planning.

Background: a verified survey of MemGPT/Letta, Mem0, Zep/Graphiti, Generative Agents, A-MEM, plus Hermes and Claude Code informed the decisions below.
Supersedes: the **agent tool surface** of BEP 1 (Memory Enhancement Design). BEP 1's two-tier maxim/memory model, `pep_memory_backend`, and markdown default are retained and extended; its `Remember`/`Forget`/`ReviseMaxim` *agent* tools are replaced by the read-only surface in §3.

---

## Core Insight

Memory work splits into **reads** (cheap, safe, needed at the point of reasoning) and **writes**
(routing, contradiction resolution, promotion, forgetting — quality-sensitive *judgment*). The
strongest agent-memory systems (Letta sleep-time, Mem0's update phase) do not make the conversational
agent perform writes mid-turn; they move all curation **off-turn**, where a focused process sees the
full transcript and existing memories.

BEP 10 adopts that inversion for BOS:

> The conversational agent **only reads** memory. Every write — episodic memory, maxims, forgetting,
> linking — is performed off-turn by a platform-owned **consolidation agent** driven by `optimize()`.
> The storage backend is the source of truth; the system prompt is a cache-friendly *projection* of it.

This removes the hardest decisions from the latency path, eliminates a class of prompt-cache
invalidations, and makes the memory policy a single, evaluable, optimizable component.

## Goals

1. Make the conversational agent's memory surface **read-only** (`Recall` + auto-recall + in-context index).
2. Turn `optimize()` from a no-op into a real **off-turn consolidation agent** (extract, route, resolve, promote, compact, reflect, link).
3. Define a **cache-aware maxim lifecycle**: maxims load once per session, never mutate the cached prefix mid-chat.
4. Add the minimal **protocol signal** the lifecycle needs: `session_phase` (new / resumed / continuing).
5. Replace hard delete with **soft, reversible forgetting**.
6. Upgrade retrieval from substring match to an **in-context index + lexical (FTS5) + scored ranking** — no vectors by default.
7. Establish an **evaluation + offline prompt-optimization** path (component eval → DSPy/GEPA), without runtime coupling.

## Non-Goals

- **Vector/embedding retrieval by default.** Reserved for a swap-in backend at scale only (§8, §11). The personal-memory regime uses lexical + in-context index.
- **Coupling to the Obsidian application** (REST/MCP). Obsidian is a transport/export concern handled by an external skill (§10), never a backend.
- **Runtime dependency on DSPy.** DSPy/GEPA is an *offline* optimizer; compiled artifacts are lifted into BOS prompt strings (§9).
- **Memory blocks / multi-agent shared memory.** The Letta memory-block generalization of maxims is deferred until BEP 2 (named actors) needs it.
- **A graph database.** Links are an adjacency list in entry metadata (§7), not a graph store.

## Source-of-Truth and Ownership

| Concern | Source of truth | Owner |
|---|---|---|
| Episodic memories & maxims (durable) | memory **backend** (`pep_memory_backend`) | platform |
| Within-session directives | **chat history + consolidated summary** (`ChatStore`) | platform / consolidator |
| System-prompt maxim block | a **projection** of the backend, snapshotted per session | agent runtime |
| All memory **writes** | — | **`optimize()` consolidation agent** |
| Memory **reads** | — | conversational **agent** (`Recall` / auto-recall) |
| `session_phase` | actor in-memory map + `ChatStore` | **`AgentActor`** |
| Directive preservation on compaction | — | **`ep_consolidator`** |

---

## 3. Agent Tool Surface (read-only) — supersedes BEP 1

The conversational agent is registered with **one** memory tool:

- **`Recall`** — `query` (lexical search → ranked snippets) or `entry_id` (fetch full entry). Unchanged from today's read semantics (`plugin.py:205`).

Removed from the agent surface:

- **`Remember`**, **`Forget`** — no longer agent tools. Their backend operations (`ingest_memory`, soft-`forget_memory`, `set_maxim`) survive **only as internal mechanisms** the consolidation agent calls.
- **`ReviseMaxim`** (`plugin.py:174`) — **deleted**. Its append-only "merge cycle" never existed; maxim writes are off-turn (§5).

Reads are additionally served *without* a tool call by:

- **Auto-recall** — a turn interceptor (`prepare`/`before_llm`) retrieves on the incoming message and injects top hits as ephemeral context (after the cache breakpoint, §5).
- **In-context index** — a compact catalog (`id` + tags + one-line description) of available memories, rendered in the system prompt via `get_system_prompt_section`; the agent fetches full content by `entry_id`.

**Rationale.** Writes are judgment best made off-turn with full context; reads cannot corrupt the
store and are needed mid-reasoning. Removing write tools also removes the mid-turn cache-bust hazard
(§5).

---

## 4. The Consolidation Agent (`optimize()`)

`optimize()` (today a no-op, `markdown_backend.py:123`) becomes an **off-turn reasoning agent**.
Given the chat transcript + existing memories, it performs:

1. **Extract** — derive candidate facts worth keeping from the conversation.
2. **Route** — maxim vs episodic memory (function, not content: behavior-shaping/always-visible → maxim).
3. **Resolve contradictions** — Mem0-style ADD / UPDATE / DELETE / NOOP over the top-`s` similar existing memories. **DELETE is the off-turn `Forget`.**
4. **Promote** — episodic → maxim when a durable pattern is observed.
5. **Compact** — fold maxim revision notes into clean prose when approaching the 2048-char cap (the real "merge cycle").
6. **Reflect** — synthesize higher-level memories when accumulated importance crosses a threshold (Generative Agents).
7. **Link** — attach related entries (§7).

### Engine layering (three distinct roles — do not conflate)

- **`optimize()`** = the **hook/trigger** (when consolidation runs).
- **`ep_consolidator`** (`contract.py:185`) = the **runtime engine** for summarize/reflect/compact sub-tasks.
- **`ep_provider`** = direct calls for the **structured ADD/UPDATE/DELETE** decision (not summarization).
- **DSPy/GEPA** (§9) = the **offline prompt compiler** that tunes the prompts the above run with. Never in the hot path.

### Scheduling and durability

- Triggered off the user's critical path: **end-of-chat**, and/or **every N turns** (Letta default 5), and/or **idle**. Configurable (§8).
- **Flush-on-session-close + periodic flush** so an abrupt end never loses a chat's learnings before the next cycle.
- Cost trade is explicit: latency moves to idle/off-turn compute. `optimize()` is **opt-in** and must be **idempotent**.

---

## 5. Maxim Lifecycle and Cache Discipline

### In-chat vs cross-chat

- **In-chat immediacy is free.** A directive the user states ("from now on answer in Portuguese") is already in the message history; the model honors it for the rest of the session with **no maxim write**.
- A maxim's unique job is **cross-session persistence**. Therefore maxim writes are **off-turn only** (§4), landing at the next session boundary.

### Loading cadence (decided)

- Maxims are **snapshotted at session start / resume** (keyed off `session_phase`, §6) and held **constant for the whole session**.
- **Never per-iteration, never mid-turn.** Today `_build_system_prompt()` runs once at `ask()` start (`agent.py:375`) **and again every loop iteration** (`agent.py:513`), each time re-reading maxims from storage. With off-turn writes, maxims cannot change within a turn — the per-iteration maxim read is removed via memoization. The per-iteration rebuild may remain for genuinely volatile sections (e.g. skills loaded mid-turn via `LoadSkill`).
- For long-lived actors, **one** refresh is permitted on an explicit "`optimize()` committed a maxim change" signal, applied at a turn boundary (accepting one cache miss then).

### Prompt layering (cache breakpoint after maxims)

```
[ base prompt + tool defs ]    stable / session   ┐ cached
[ maxims ]                     cross-session       ┘ prefix
─────────────── cache breakpoint ───────────────
[ skills loaded mid-turn, system info / time ]    volatile
[ ephemeral "active directives (this session)" ]  volatile, append-only
[ chat history … current message ]                append-only
```

- Stable maxims live **before** the breakpoint; anything volatile lives **after** it.
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

It is surfaced as **`TurnContext.metadata["session_phase"]`** (string literal `new`/`resumed`/`continuing`).
No new interceptor stage or core dataclass is required — `metadata` is the documented escape hatch and
flows to every plugin and interceptor.

**Consumers.** Memory plugin is first: `new`/`resumed` → (re)load maxim snapshot; `continuing` → reuse.
Broadly useful (e.g. a skills plugin re-announcing on resume).

---

## 7. Retrieval, Forgetting, and Links (default backend)

### Retrieval — in-context index + lexical + scored ranking (no vectors by default)

`search_memories` (`markdown_backend.py:81`, substring + ctime) is replaced by, in ascending order of N:

1. **In-context index** — catalog surfaced in the prompt; zero recall-miss at small N.
2. **Lexical search** — SQLite **FTS5** (BM25), agentic/iterative via `Recall`.
3. **Scored ranking** over lexical candidates (Generative Agents signals, orthogonal to vectors):
   ```
   score(m,q) = w_rec · 0.99^hours_since(m.last_used) + w_imp · importance(m)/10 + w_lex · bm25(q,m)
   ```
   `importance` (1–10) is assigned **by the consolidation agent at write time** and stored in `MemoryEntry.metadata`. Weights start at 1, terms normalized to [0,1].

### Forgetting — soft and reversible (Graphiti-style)

- Add `valid` / `invalidated_at` to `MemoryEntry.metadata`. The off-turn DELETE sets the flag; `search_memories` filters invalidated entries by default.
- `optimize()` hard-purges entries invalidated beyond a retention window (§8).

### Links — graph-lite (A-MEM / Graphiti BFS)

- Optional `links: list[str]` (entry-id refs) in `MemoryEntry.metadata`. On ingest, the consolidation agent finds candidate neighbors (shared tags / lexical overlap at small N) and the LLM decides which to link. `Recall` may expand one hop. No graph DB.

---

## 8. Configuration Shape

```toml
[exts.MemoryPlugin]
maxims  = ["user", "soul", "identity", "rules"]   # always-in-prompt keys (unchanged)
scope   = "workspace"                              # unchanged
backend = "_default"                               # markdown default; swappable (§11)

[exts.MemoryPlugin.consolidation]
enabled            = false        # opt-in; optimize() is a no-op when false
trigger            = "session_close"   # "session_close" | "every_n_turns" | "idle"
every_n_turns      = 5            # when trigger = "every_n_turns"
retention_days     = 30          # hard-purge soft-deleted entries older than this

[exts.MemoryPlugin.retrieval]
auto_recall        = true        # interceptor-injected retrieval
index_in_prompt    = true        # in-context catalog
top_k              = 5
```

New `MemoryEntry.metadata` fields: `importance: int (1-10)`, `valid: bool`, `invalidated_at: str|None`,
`last_used: str|None`, `links: list[str]`. (`metadata` already exists, `scoped_memory.py:15`.)

---

## 9. Evaluation and Offline Optimization

**Finding to act on:** BEP 1's rich "USING YOUR MEMORY" prompt is **not wired in** — the running code
uses the terse `_MEMORY_PROMPT_SECTION` (`plugin.py:81`). Do not swap it in blind; measure it.

### Evaluation ladder (build cheapest first)

1. **Component eval (highest ROI)** — labeled set: routing `{transcript → action: maxim:key | memory | nothing}` (now targeting the *consolidation agent*) + retrieval `{query → relevant ids}` (recall@k / MRR). ~50 cases; runs in seconds; unblocks all prompt iteration.
2. **Scenario eval + LLM-judge** — scripted multi-turn conversations; `ep_provider` is stubbable; hang on `tests/test_harness.py`.
3. **Standard benchmarks** — LOCOMO / LongMemEval for headline numbers (conflate retrieval + generation).
4. **Telemetry** — dead-memory ratio (stored-but-never-recalled), recall-hit-rate, maxim churn.

### DSPy / GEPA (offline only)

Memory decisions map to DSPy Signatures; an optimizer (MIPROv2 / BootstrapFewShot / **GEPA**) compiles
instructions + few-shot demos against the §9 metric + dataset. Precedent:
[`NousResearch/hermes-agent-self-evolution`](https://github.com/NousResearch/hermes-agent-self-evolution)
(DSPy + GEPA). **Rules:** (a) DSPy *consumes* the eval — build it first; (b) run offline and **lift the
compiled instructions/demos into BOS prompt strings** (`_MEMORY_TOOL_USAGE`, the prompt section). No
runtime dependency (DSPy wants the LM call; that fights `ep_provider`).

---

## 10. Obsidian Interop (transport, not backend)

The backend is swappable; Obsidian is **not** a storage tech to swap to (it is a GUI app). Keep BOS
memory as source of truth and move memory to/from Obsidian via an **external skill**: emit YAML
frontmatter (`importance`/`valid`/`invalidated_at`/`tags`/`links`) and `[[wikilinks]]`. If the default
backend adopts frontmatter + slugged filenames, the vault is already Obsidian-compatible and export is
near-trivial. The runtime never depends on Obsidian.

---

## 11. Backend Swappability

`pep_memory_backend` makes storage tech a pluggable choice. **Markdown is the default** (right for the
small personal regime). FTS5 and, **only at scale**, a vector DB are alternative backends behind the
same protocol — chosen by config, not bolted onto the default. The decisions above upgrade the default
backend and do not constrain what a deployment swaps in.

---

## 12. Implementation Plan (sequenced)

1. **In-context index** (`R1.1`) — render catalog in `get_system_prompt_section`.
2. **Read-only surface (§3)** — `register_tools` registers only `Recall`; delete `ReviseMaxim`; demote `Remember`/`Forget` to internal calls.
3. **Component eval harness (§9.1)** — ~50 labeled cases; the unblocker.
4. **FTS5 lexical search (§7)** — new backend or upgrade default search.
5. **Soft delete (§7)** — `valid`/`invalidated_at`; filter in `search_memories`.
6. **`session_phase` (§6)** — compute in `AgentActor`, expose via `TurnContext.metadata`.
7. **Per-session maxim snapshot + prompt layering (§5)** — memoize maxims; breakpoint after them.
8. **Importance + scored ranking (§7).**
9. **`optimize()` consolidation agent (§4)** + **consolidator preserves directives (§5)**.
10. **DSPy/GEPA offline optimization (§9)** against the eval.
11. **Links (§7).**
12. **Memory blocks / vector backend** — deferred (Non-Goals).

## 13. Acceptance Criteria

- The agent has no memory write tool; `Recall` is the only memory tool registered.
- A maxim is never written during a turn; all maxim/memory writes originate from `optimize()`.
- Within a session with no `optimize()` write, the system-prompt maxim block is byte-identical across all turns and iterations (cache holds); maxims are read from storage at most once per session.
- A directive stated mid-chat is honored for the rest of the session without any maxim write, and persists into a *new* session after `optimize()` runs.
- `Forget` (off-turn) marks entries invalid and reversibly; hard purge only after `retention_days`.
- `TurnContext.metadata["session_phase"]` is present and correct for new / resumed / continuing turns.
- The component eval exists and reports routing accuracy + retrieval recall@k.

## 14. Open Questions

1. **`session_phase` vs interceptor stage.** Is `metadata["session_phase"]` sufficient, or do we also want a first-class `session_start` interceptor stage for once-per-session hooks?
2. **Consolidation trigger default.** `session_close` vs `every_n_turns` — what is the right default for the CLI/TUI single-user case vs always-on channels?
3. **Directive preservation contract.** How does `ep_consolidator` *identify* a "durable directive" to preserve on compaction — heuristic, a tagged message, or an LLM pass?
4. **Resume reload window.** For long-lived actors, what exactly signals "`optimize()` committed a change" to trigger the one permitted mid-session maxim refresh?
5. **BEP 1 reconciliation.** BEP 1 should be marked superseded-in-part (tool surface) and its "USING YOUR MEMORY" prompt either wired in or retired pending the eval.

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-13 | Initial draft | Capture the platform-managed memory design from the 2026-06-13 review; supersede BEP 1's agent tool surface |
