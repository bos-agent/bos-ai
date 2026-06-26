# BEP 12: Structured Agent Output & the AgentResult Primitive

Status: **in progress** (updated 2026-06-26)

---

## Core Insight

`Agent.ask` (`src/bos/core/agent/agent.py:243`) is already the system's core agentic primitive — it runs the tool/interceptor loop and persists the turn — but it exposes itself poorly to *programmatic* callers: it returns a bare `str` (`ctx.final_response`) and pushes everything else (token usage, tool activity, per-iteration state) out through the `event_sink` side channel. A caller that wants "the answer, plus how many round-trips and tokens it cost" has to wire an event sink to reconstruct it. And it can only return free text — there is no way to ask for a **validated, typed** result.

That gap is filled today by **two** parallel lanes that both spin up a throwaway agent-like thing and bypass `Agent.ask`:

- `BackgroundLLM` (`src/bos/core/defaults/background_llm.py:49`, contract at `src/bos/core/contract.py:206`) — a stateless one-shot that adds JSON-Schema validation + bounded retry over `LLM.complete`. Its **only** caller is the BEP 10 memory-curation consolidator (`DefaultMemoryConsolidator`, `src/bos/plugins/memory/consolidator.py:123`, which calls `self._llm.ask(..., response_schema=_RESPONSE_SCHEMA)`). Because that path bypasses the Agent, memory curation gets *none* of the agent machinery — no configurable model resolution, no improvable system prompt, no prompt provider, no turn events/observability.
- `SubagentRuntime` (contract at `src/bos/core/contract.py:312`, adapter `_HarnessSubagentRuntime` at `src/bos/core/harness.py:220`) — spins up a real disposable agent via `create_agent` and returns its **text**. Its **only** caller is the subagent plugin's `AskSubagent` tool (`src/bos/plugins/subagent.py:168`).

These are the same capability at heart — *spin up a disposable agent and return its result* — split only by "do I want validated structure or free text," and that split is exactly what `Agent.run(schema=…)` now erases.

This BEP closes the gaps in two moves:

1. **The primitive (tracks 1–4, shipped):** give the Agent a **first-class result type** (`AgentResult`) and **optional structured output**, exposed through a new `Agent.run(...)` primitive. `ask` becomes a thin text-returning wrapper over it.
2. **The unification (this update):** collapse `SubagentRuntime` **and** `BackgroundLLM` into one port — **`AgentRunner`** — implemented once by a harness adapter over `create_agent` + `Agent.run`. The subagent tool reads `result.output` (text); the consolidator passes `schema=…` and reads `result.output` (parsed object). Both `PluginServices` fields fold into a single `agent_runner`.

The design metric: **a programmatic caller gets `result.output` + `result.usage` + `result.iterations` from one call; asking for typed output is one `schema=` argument away; "improving consolidation" becomes "improving an agent"; and there is exactly one way — `AgentRunner.run` — for a plugin/tool to spawn a disposable agent, on-turn or off-turn.**

---

## Goals

1. **`AgentResult`** — a result dataclass carrying the final response *and* run metadata (output, whether it is structured, iteration count, aggregated token usage, turn id, finish reason).
2. **`Agent.run(...) -> AgentResult`** — the full-fidelity primitive. The existing `ask(...) -> str` is preserved as a thin wrapper (`(await self.run(...)).output`), so current callers do not churn.
3. **Optional structured output** — `run(..., schema=…)` validates the **final** answer against a JSON Schema (provider hint + local validation + bounded retry) and returns the parsed object; `result.structured` discriminates.
4. **`AgentRunner` — one disposable-agent port.** A single capability that subsumes both spawning a subagent (returns text) and a structured one-shot (returns a validated object), implemented once by a harness adapter over `create_agent` + `Agent.run`. Replaces `SubagentRuntime` and `BackgroundLLM`.
5. **Migrate both callers to `agent_runner`** — the `AskSubagent` tool (text path, on-turn, passes `context`) and memory curation (structured path, off-turn, omits `context`) — and **delete** `SubagentRuntime`, `BackgroundLLM`, `DefaultBackgroundLLM`, and `_HarnessSubagentRuntime`. `BackgroundLLM`'s validate-hint-retry is already gone — it folded into the shared structured validator that `Agent.run` uses (tracks 1–4).

## Non-Goals

1. **Mid-loop tool-argument schemas.** Validation applies to the agent's *final* answer only, not to individual tool-call arguments (those already have per-tool JSON schemas).
2. **Changing `ask`'s contract.** `ask` keeps returning `str`; this BEP is additive for its ~5 call sites (gateway actor `src/bos/gateway/actors/agent_actor.py:308`, the subagent runtime, the skills test runtime, CLI, tests).
3. **Streaming.** `run` returns a completed `AgentResult`; incremental streaming stays on the `event_sink`.
4. **Migrating the plain-text compaction `Consolidator`.** The `Consolidator` port (`src/bos/core/agent/contract.py:247`, default `LLMConsolidator`) produces a free-text summary and is *not* a `BackgroundLLM` caller; converting it to an agent is possible later but out of scope here.
5. **A typed/generic result.** `output` is `Any` discriminated by `structured`; no schema-to-Python-type codegen.
6. **Folding `LLM.complete` into `AgentRunner`.** `LLM.complete` is the raw, stateless, loop-free substrate that `Agent.run` (and therefore `AgentRunner`) is *built on*; it stays as the bottom primitive. `AgentRunner` is the disposable-agent layer above it, not a replacement for it.

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

Tools may still run before the final answer — so an agent can *research, then propose a typed result* (the capability BEP 15 §A4 assumes).

> **As-built amendment (2026-06-26).** The original sketch above ("extract into one shared *helper* in `bos.core.defaults`") could not stand as written: the agent ring (`src/bos/core/agent/`) is enforced **stdlib-pure** (`tests/test_agent_ring_isolation.py`), so it cannot import `jsonschema`. Validation therefore shipped as a **port + adapter**, not a flat shared function:
> - **Agent ring (stdlib only)** — `src/bos/core/agent/_structured.py`: `provider_hint_schema`, `UNUSABLE_FINISH_REASONS`, `StructuredOutputError`, a `StructuredValidator` Protocol, and a `parse_json` fallback.
> - **Assembly ring** — `src/bos/core/defaults/structured_validator.py`: `JsonSchemaValidator` (the `jsonschema` adapter), injected into agents by `create_agent` via `_default_structured_validator()` in `harness.py`.
>
> So there is a single validation implementation, but it lives behind an injected port rather than being imported directly by the agent. `BackgroundLLM`'s old duplicate validate/retry logic is therefore obsolete (it is removed wholesale in track 6 below, not merely "delegated").

### D. `AgentRunner` — the unified disposable-agent port

One port — *spin up a disposable agent and return its result* — that subsumes both
`SubagentRuntime` (text) and `BackgroundLLM` (validated object). It lives in the
contract ring (`src/bos/core/contract.py`) and is implemented **once** by a harness
adapter over `create_agent` + `Agent.run`:

```python
class AgentRunner(Protocol):
    async def run(
        self,
        message: MessageContent,
        *,
        kind: str | None = None,                  # a registered agent kind, OR…
        agent_cfg: dict[str, Any] | None = None,  # …an ad-hoc disposable agent
        schema: dict[str, Any] | None = None,     # structured output → result.output is the parsed obj
        context: ToolContext | None = None,       # OPTIONAL parent turn (see below)
        model: str | None = None,
    ) -> AgentResult: ...
```

**Why `context` is optional — and the key insight that makes the unification work.**
`ToolContext` is a *per-invocation parameter, not a service binding*. `AskSubagent` is
invoked *by the model as a tool*, so it already holds a `ToolContext` and passes it; the
adapter uses it only to (a) nest the child chat-id under the parent
(`make_subagent_chat_id(context.chat_id, kind)`) and (b) parent the event sink
(`derive_event_sink(context.event_sink, …)`). Off-turn callers — the memory consolidator,
which runs inside a job with no invoking tool — simply **omit `context`**: the adapter
falls back to a standalone **internal** chat-id (the `INTERNAL_CHAT_SEPARATOR` convention
in `src/bos/core/_chat_store_utils.py`, so the turn stays out of the user's chat
list/recall) and a null parent sink. Same capability, on-turn and off-turn — this is why
the consolidator does *not* need a separate non-agent lane.

The adapter is essentially today's `_HarnessSubagentRuntime.ask` (`src/bos/core/harness.py:220`)
generalized: derive the chat-id (nested if `context`, internal-standalone if not), call
`create_agent(kind, agent_cfg)`, derive the child event sink only when `context` is present,
then `await agent.run(child_chat_id, message, schema=schema, model=…, event_sink=…)` and
return the `AgentResult` whole (callers read `.output`). The disposable agent has a fresh
chat-id every run, so there is no chat history and no compaction-recursion risk.

### E. Migrating the two callers

- **`AskSubagent`** (`src/bos/plugins/subagent.py:168`) — on-turn, text. Today it calls
  `runtime.ask(role, task, context=context)`; it becomes
  `(await agent_runner.run(task, kind=role, context=context)).output`. Behavior-preserving.
- **`DefaultMemoryConsolidator`** (`src/bos/plugins/memory/consolidator.py:123`) — off-turn,
  structured. Today it calls `self._llm.ask(messages=…, response_schema=_RESPONSE_SCHEMA, model=…)`
  then `json.loads(resp.content)`. It becomes
  `agent_runner.run(_render_user_prompt(request), agent_cfg={"system_prompt": _SYSTEM_PROMPT}, schema=_RESPONSE_SCHEMA, model=self._model)`
  and reads `result.output` (already the parsed, validated object — no manual `json.loads`).
  The memory plugin's `MemoryConsolidator` protocol (`src/bos/plugins/memory/consolidator.py:48`)
  is unchanged; only the default implementation's dependency swaps from `background_llm` to
  `agent_runner`. The payoff stands: consolidation now has a model, a prompt, and turn events
  like any agent.

### F. Collapsing `PluginServices` and removing the old lanes

`PluginServices` (`src/bos/core/contract.py:326`) today carries `subagents: SubagentRuntime`
and `background_llm: BackgroundLLM | None`. Both fold into a single
`agent_runner: AgentRunner`. With both callers migrated, these are **deleted**:
`SubagentRuntime` and `BackgroundLLM` (protocols, `contract.py`), `DefaultBackgroundLLM`
(`src/bos/core/defaults/background_llm.py`), `_HarnessSubagentRuntime` (`harness.py`), the
two `PluginServices` fields, and the harness wiring that constructs them — plus
`tests/test_background_llm.py`.

The skills plugin's `_SkillTestRuntime` still uses `PluginServices.llm` and `.consolidator`
(it builds an agent by hand for skill tests). Those two fields are **out of scope for this
update**: fold them into `agent_runner` only *after* verifying the skills runtime's exact
needs (test-only tool/loader wiring), as a later, separate step. Until then `llm` and
`consolidator` stay on `PluginServices`.

> **Layering (the clean end state).** `LLM.complete` (raw, stateless) → `Agent.run`
> (one agentic turn, returns `AgentResult`) → `AgentRunner.run` (spawn a *disposable* agent
> turn — the single capability plugins/tools use). Do **not** fold `LLM.complete` into
> `AgentRunner`; it is the substrate `AgentRunner` is built on (Non-Goal 6).

---

## Layered plan (dependency-ordered, bottom-up)

**Tracks 1–4 — SHIPPED** (commit `70793d6`):

1. ✅ **Structured-output validator as a port + adapter** — `StructuredValidator` Protocol + `provider_hint_schema` / `UNUSABLE_FINISH_REASONS` / `StructuredOutputError` / `parse_json` in the stdlib-pure agent ring (`src/bos/core/agent/_structured.py`); `JsonSchemaValidator` adapter in `src/bos/core/defaults/structured_validator.py`, injected by `create_agent`. (Replaces the original "flat shared helper" sketch — see §C amendment.)
2. ✅ **`AgentResult`** — dataclass in `agent/contract.py`; exported from `bos.core`.
3. ✅ **`Agent.run` + `ask` wrapper** — loop moved into `run`; `iterations`/`usage` aggregated; `ask` delegates and returns `.output`.
4. ✅ **Structured path in `run`** — `schema` → provider hint + injected validator + bounded `max_schema_retries`; raises `StructuredOutputError` on exhaustion / unusable finish reason.

**Tracks 5–7 — the `AgentRunner` unification (this update):**

5. **`AgentRunner` port + adapter** — add the `AgentRunner` Protocol to `contract.py`; implement the single harness adapter over `create_agent` + `Agent.run` (generalizing `_HarnessSubagentRuntime`, with optional `context`).
6. **Swap `PluginServices`** — replace `subagents` + `background_llm` with `agent_runner`; update harness wiring to construct and inject the adapter.
7. **Migrate callers + delete old lanes** — `AskSubagent` → `agent_runner.run(kind=…, context=ctx)`; `DefaultMemoryConsolidator` → `agent_runner.run(agent_cfg=…, schema=…)`; delete `SubagentRuntime`, `BackgroundLLM`, `DefaultBackgroundLLM`, `_HarnessSubagentRuntime`, the two `PluginServices` fields, harness wiring, and `tests/test_background_llm.py`.

Tracks 5–7 depend on 1–4 (shipped) and on `create_agent`/`Agent.run` being reachable from the adapter (they are, inside an active harness). **Out of scope (later, verify first):** folding `PluginServices.llm`/`consolidator` into `agent_runner` once the skills `_SkillTestRuntime` migrates.

---

## Affected Code

| Area | File(s) | Change | Status |
|---|---|---|---|
| Result type | `src/bos/core/agent/contract.py`; `src/bos/core/__init__.py` | add + export `AgentResult` | ✅ shipped |
| Core primitive | `src/bos/core/agent/agent.py` (`ask`→`run` refactor) | `run` returns `AgentResult`; `ask` delegates; aggregate usage/iterations; structured final-answer path | ✅ shipped |
| Structured validator (port+adapter) | `src/bos/core/agent/_structured.py`; `src/bos/core/defaults/structured_validator.py`; `harness.py` (`_default_structured_validator`) | stdlib-pure port + `jsonschema` adapter, injected by `create_agent` | ✅ shipped |
| `AgentRunner` port | `src/bos/core/contract.py`; `src/bos/core/__init__.py` | add `AgentRunner` Protocol; export | track 5 |
| `AgentRunner` adapter | `src/bos/core/harness.py` | single impl over `create_agent` + `Agent.run` (optional `context`); wire + inject | tracks 5–6 |
| `PluginServices` | `src/bos/core/contract.py` (field), `harness.py` (construction) | replace `subagents`+`background_llm` with `agent_runner` | track 6 |
| Subagent tool | `src/bos/plugins/subagent.py:168` | `AskSubagent` → `agent_runner.run(kind=…, context=ctx)`, read `.output` | track 7 |
| Memory curation | `src/bos/plugins/memory/consolidator.py`, `.../memory/plugin.py` | `DefaultMemoryConsolidator` → `agent_runner.run(agent_cfg=…, schema=…)`, read `.output` | track 7 |
| Delete old lanes | `contract.py` (`SubagentRuntime`, `BackgroundLLM`), `defaults/background_llm.py`, `harness.py` (`_HarnessSubagentRuntime`), `tests/test_background_llm.py` | remove protocols/adapters/wiring/tests | track 7 |
| Consumer | BEP 15 §A4 | gains the structured-output primitive it presumes | n/a |

---

## Open Issues

1. ✅ **Method name.** `run` chosen; `ask` is the thin text wrapper. (Shipped.)
2. ✅ **Retry exhaustion behavior.** Raises `StructuredOutputError`. (Shipped.)
3. ✅ **`BackgroundLLM` removal vs demotion** — **delete outright.** Its validate/retry already folded into the injected structured validator; no remaining caller wants a non-agent lane (the consolidator uses `AgentRunner` with `context` omitted). No thin-wrapper demotion.
4. **Consolidation agent registration** — does the disposable consolidation agent get a real registered `kind` (configurable/promptable via config), or stay an ad-hoc `agent_cfg={"system_prompt": …}` passed to `agent_runner.run`? Track 7 ships the ad-hoc form; promoting it to a registered `kind` is a follow-up if config-tunability is wanted.
5. ✅ **`usage` shape** — summed totals on `AgentResult.usage`; per-iteration breakdown stays in events. (Shipped.)
6. **`AgentRunner` chat-id ownership.** The adapter derives the child chat-id (nested vs internal-standalone). Confirm the off-turn standalone id uses the `INTERNAL_CHAT_SEPARATOR` convention so it is filtered from list/recall exactly like subagent chats — and that nothing downstream depends on the consolidator's old non-chat path.

---

## References

- BEP 11: Async Tasks and Scheduling (defines `BackgroundLLM`, §3 — **superseded**: `BackgroundLLM` is removed and folded into `AgentRunner` by this BEP).
- BEP 13: Ring isolation (the `agent` ring is stdlib-pure — why structured validation is a port+adapter, and why the `AgentRunner` adapter lives in the harness/assembly ring).
- BEP 15: Agent-Backed Command Workflow (§A4 structured-output contract — primary consumer; depends on this BEP).
- Chat-id internal-convention helpers (`INTERNAL_CHAT_SEPARATOR`/`make_subagent_chat_id`/`is_internal_chat`/`filter_internal_chats`, `src/bos/core/_chat_store_utils.py`) used to keep disposable-agent turns out of the user's chat list/recall.

---

## Revision History

- **2026-06-25** — Initial draft. Claims BEP number 12 (renumbered the former "BEP 12: Agent-Backed Command Workflow" to BEP 15, since this structured-output primitive is the lower-level dependency it builds on).
- **2026-06-26** — Tracks 1–4 shipped (`70793d6`). Recorded the as-built amendment: structured validation landed as a **port + adapter** (forced by the stdlib-pure agent ring), not a flat shared helper (§C). Replaced original tracks 5–6 ("migrate consolidator, retire `BackgroundLLM`") with the broader **`AgentRunner` unification**: a single disposable-agent port subsuming both `SubagentRuntime` and `BackgroundLLM`, with `ToolContext` as an optional per-call param (Goals 4–5; Design §§D–F; tracks 5–7; resolved Open Issues 1–3, 5).
