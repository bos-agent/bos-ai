# BEP 12: Structured Agent Output & the AgentResult Primitive

Status: **draft** (2026-06-25)

---

## Core Insight

`Agent.ask` (`src/bos/core/agent/agent.py:243`) is already the system's core agentic primitive — it runs the tool/interceptor loop and persists the turn — but it exposes itself poorly to *programmatic* callers: it returns a bare `str` (`ctx.final_response`) and pushes everything else (token usage, tool activity, per-iteration state) out through the `event_sink` side channel. A caller that wants "the answer, plus how many round-trips and tokens it cost" has to wire an event sink to reconstruct it. And it can only return free text — there is no way to ask for a **validated, typed** result.

That gap is currently filled by a *parallel* lane: `BackgroundLLM` (`src/bos/core/defaults/background_llm.py:49`, contract at `src/bos/core/contract.py:206`), a stateless one-shot that adds JSON-Schema validation + bounded retry over `LLM.complete`. Its **only** caller is the BEP 10 memory-curation consolidator (`DefaultMemoryConsolidator`, `src/bos/plugins/memory/consolidator.py:123`, which calls `self._llm.ask(..., response_schema=_RESPONSE_SCHEMA)`). Because that path bypasses the Agent, memory curation gets *none* of the agent machinery — no configurable model resolution, no improvable system prompt, no prompt provider, no turn events/observability. You cannot improve consolidation the way you improve any other agent.

This BEP closes both gaps with one move: give the Agent a **first-class result type** (`AgentResult`) and **optional structured output**, exposed through a new `Agent.run(...)` primitive. `ask` becomes a thin text-returning wrapper over it. Once the Agent can return a validated typed result, memory curation becomes a real (disposable) agent that calls `run(schema=…)`, and `BackgroundLLM`'s reason to exist folds into a shared validator.

The design metric: **a programmatic caller gets `result.output` + `result.usage` + `result.iterations` from one call; asking for typed output is one `schema=` argument away; and "improving consolidation" becomes "improving an agent" — same prompt, model, and observability knobs as everything else.**

---

## Goals

1. **`AgentResult`** — a result dataclass carrying the final response *and* run metadata (output, whether it is structured, iteration count, aggregated token usage, turn id, finish reason).
2. **`Agent.run(...) -> AgentResult`** — the full-fidelity primitive. The existing `ask(...) -> str` is preserved as a thin wrapper (`(await self.run(...)).output`), so current callers do not churn.
3. **Optional structured output** — `run(..., schema=…)` validates the **final** answer against a JSON Schema (provider hint + local validation + bounded retry) and returns the parsed object; `result.structured` discriminates.
4. **Migrate the one structured `BackgroundLLM` caller** (memory curation) to a disposable agent calling `run(schema=…)`, so it inherits agent prompt/model/observability.
5. **Retire `BackgroundLLM`** — extract its validate-hint-retry into a single shared validator reused by `run`; remove the now-unused protocol/adapter (or demote it to a thin wrapper).

## Non-Goals

1. **Mid-loop tool-argument schemas.** Validation applies to the agent's *final* answer only, not to individual tool-call arguments (those already have per-tool JSON schemas).
2. **Changing `ask`'s contract.** `ask` keeps returning `str`; this BEP is additive for its ~5 call sites (gateway actor `src/bos/gateway/actors/agent_actor.py:308`, the subagent runtime, the skills test runtime, CLI, tests).
3. **Streaming.** `run` returns a completed `AgentResult`; incremental streaming stays on the `event_sink`.
4. **Migrating the plain-text compaction `Consolidator`.** The `Consolidator` port (`src/bos/core/agent/contract.py:247`, default `LLMConsolidator`) produces a free-text summary and is *not* a `BackgroundLLM` caller; converting it to an agent is possible later but out of scope here.
5. **A typed/generic result.** `output` is `Any` discriminated by `structured`; no schema-to-Python-type codegen.

---

## Design

### A. `AgentResult` (agent ring)

A frozen dataclass beside `LLMResponse`/`TurnContext` in `src/bos/core/agent/contract.py` (the agent ring owns its result shape):

```python
@dataclass
class AgentResult:
    output: Any                 # final response: str, or the schema-validated object
    structured: bool            # True ⇒ output is the validated object (schema was supplied)
    iterations: int             # LLM round-trips taken this run
    usage: dict[str, int]       # summed prompt/completion/total tokens across iterations
    turn_id: str
    finish_reason: str | None = None
```

`usage` aggregates the per-iteration `LLMResponse.usage` dicts the loop already produces (today emitted only via `_emit_event` metadata, never summed). `structured` is the discriminator the caller reads to know how to interpret `output`.

### B. `Agent.run` + loop refactor

The ~300-line loop currently inside `ask` moves into `run`; `ask` delegates:

```python
async def run(self, chat_id, content, *, schema: dict | None = None,
              max_schema_retries: int = 1, <existing ask kwargs>) -> AgentResult: ...

async def ask(self, chat_id, content, <existing kwargs>) -> str:
    return (await self.run(chat_id, content, **kwargs)).output  # schema=None ⇒ output is str
```

`run` tracks `iterations` and accumulates `usage` as it goes, and returns the `AgentResult` instead of the bare `ctx.final_response`. Behavior with `schema=None` is byte-for-byte the current `ask` behavior (same persistence, events, interceptors), so the wrapper is a pure refactor for existing callers.

### C. Structured output

When `schema` is supplied:
- It is passed as the provider hint into `self._llm.complete(...)` (the same `response_schema` kwarg `LLM.complete` already forwards — `BackgroundLLM` uses exactly this).
- When the model emits a **final** message (no tool calls), the answer is validated locally with `jsonschema` against the authoritative schema. On success, `output` = parsed object, `structured=True`. On failure, a corrective message is appended and the loop re-enters, bounded by `max_schema_retries`; exhaustion raises (reusing `BackgroundLLM`'s `_UNUSABLE_FINISH_REASONS` distinction so a truncation/filter/error isn't mislabeled a schema failure).

The validate-hint-retry logic is **extracted from `BackgroundLLM` into one shared validator** (a small helper in the assembly ring, e.g. `bos.core.defaults`) so there is a single implementation, used by `run`'s structured path. Tools may still run before the final answer — so an agent can *research, then propose a typed result* (the capability BEP 15 §A4 assumes).

### D. Memory curation as a disposable agent

`DefaultMemoryConsolidator` (the lone structured `BackgroundLLM` caller) is reworked to:
- create a **disposable agent** via the harness (fresh, **internal** chat-id so its turns never surface in the user's chat list or recall — reusing `is_internal_chat`/`make_subagent_chat_id` in `src/bos/core/_chat_store_utils.py`), with no tools/plugins and a **tunable consolidation system prompt**;
- call `run(schema=_RESPONSE_SCHEMA)` and read `result.output`.

The agent has no chat history (new chat-id every run), so there is no compaction-recursion risk. This is wired at the harness/assembly layer (where `create_agent` is reachable); the memory plugin's `MemoryConsolidator` protocol (`src/bos/plugins/memory/consolidator.py:48`) is unchanged, only its default implementation. The payoff: consolidation now has a model, a prompt, and turn events like any agent.

### E. Retire `BackgroundLLM`

With its only caller migrated, `BackgroundLLM` (protocol `src/bos/core/contract.py:206`; adapter `DefaultBackgroundLLM`; `PluginServices.background_llm` field at `src/bos/core/contract.py:336`; harness wiring at `src/bos/core/harness.py`) is removed, its validate-retry having moved to the shared validator (C). Alternatively it is demoted to a thin stateless wrapper over that validator for any future caller that genuinely wants zero agent overhead — decided when such a caller appears, not now.

---

## Layered plan (dependency-ordered, bottom-up)

1. **Shared structured-output validator** — extract `_provider_hint_schema` / `_UNUSABLE_FINISH_REASONS` / validate+retry from `background_llm.py` into a reusable helper. (No behavior change yet; `BackgroundLLM` delegates to it.)
2. **`AgentResult`** — add the dataclass to `agent/contract.py`; export from `bos.core`.
3. **`Agent.run` + `ask` wrapper** — refactor the loop; aggregate `iterations`/`usage`; `schema=None` path only. Verify all existing `ask` callers/tests unchanged.
4. **Structured path in `run`** — wire `schema` → provider hint + shared validator + bounded retry on the final answer.
5. **Memory curation → disposable agent** — rework `DefaultMemoryConsolidator` to `create_agent` + `run(schema=…)` over an internal chat-id.
6. **Remove `BackgroundLLM`** — delete protocol/adapter/`PluginServices` field/wiring; update memory plugin construction and tests.

Tracks 1–4 are implementable now. Tracks 5–6 depend on 1–4 and on `create_agent` being reachable from the consolidator wiring.

---

## Affected Code

| Area | File(s) | Change |
|---|---|---|
| Result type | `src/bos/core/agent/contract.py`; `src/bos/core/__init__.py` | add + export `AgentResult` |
| Core primitive | `src/bos/core/agent/agent.py` (`ask`→`run` refactor) | `run` returns `AgentResult`; `ask` delegates; aggregate usage/iterations; structured final-answer path |
| Shared validator | new helper in `src/bos/core/defaults/` (from `background_llm.py`) | one validate-hint-retry impl |
| Memory curation | `src/bos/plugins/memory/consolidator.py`, `.../memory/plugin.py` | `DefaultMemoryConsolidator` → disposable agent + `run(schema=…)` |
| Remove background lane | `src/bos/core/contract.py` (`BackgroundLLM`, `PluginServices.background_llm`), `src/bos/core/defaults/background_llm.py`, `src/bos/core/harness.py` | delete or demote; update wiring + `tests/test_background_llm.py` |
| Consumer | BEP 15 §A4 | gains the structured-output primitive it presumes |

---

## Open Issues

1. **Method name.** `run` (chosen here) vs `ask_structured`/`ask_typed`. `run` reads as the full-fidelity primitive with `ask` the text convenience; confirm.
2. **Retry exhaustion behavior.** Raise (like `BackgroundLLMError`) vs return an `AgentResult` with `structured=False` and the last raw text + a failure marker. Leaning raise, to match current `BackgroundLLM` semantics.
3. **`BackgroundLLM` removal vs demotion** — delete outright, or keep as a thin stateless wrapper over the shared validator.
4. **Consolidation agent registration** — does the disposable consolidation agent get a real registered `kind` (configurable/promptable via config) or stay an internal, code-defined agent?
5. **`usage` shape** — summed totals (chosen) vs also retaining a per-iteration breakdown on the result (kept in events for now).

---

## References

- BEP 11: Async Tasks and Scheduling (defines `BackgroundLLM`, §3 — superseded for the structured path here).
- BEP 15: Agent-Backed Command Workflow (§A4 structured-output contract — primary consumer; depends on this BEP).
- Chat-id internal-convention helpers (`is_internal_chat`/`make_subagent_chat_id`, `src/bos/core/_chat_store_utils.py`) used to keep the consolidation agent's turns out of the user's chat list/recall.

---

## Revision History

- **2026-06-25** — Initial draft. Claims BEP number 12 (renumbered the former "BEP 12: Agent-Backed Command Workflow" to BEP 15, since this structured-output primitive is the lower-level dependency it builds on).
