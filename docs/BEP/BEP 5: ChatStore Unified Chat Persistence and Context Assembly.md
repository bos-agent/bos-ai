# BEP 5: ChatStore — Unified Chat Persistence and Context Assembly

Status: **design** — revised after post-revision independent reviews; ready for implementation planning.

---

## Core Insight

Currently, [ReActAgent](../../src/bos/core/agent.py#L181) orchestrates chat history management across three separate abstractions:

1. **`MessageStore`** — a thin append/read protocol for individual LLM messages, keyed by `chat_id`.
2. **`Consolidator`** — an LLM-backed summarizer called when the history exceeds the token budget.
3. **`_get_chat_history`** — agent-internal logic that loads messages, estimates tokens, triggers consolidation, reloads, and projects messages into LLM format.

Additionally, [NamedAgent](../../src/bos/named_actors/actor.py#L42) overrides `_get_chat_history` to filter tool call noise and add speaker attribution. The tool noise filtering is generally useful and should not be embedded in a NamedAgent-only override.

BEP 5 introduces a **`ChatStore`** protocol that unifies persistence and context assembly:

- load stored chat messages,
- apply active summary boundaries,
- filter historical tool noise,
- project provider-ready context,
- estimate context token usage,
- persist completed turns and compaction summaries.

The `Consolidator` remains a separate harness-level service. ChatStore stores the summary boundary; it does not decide when to consolidate and does not call an LLM.

---

## Design Principles

1. **Persistence and context assembly are one service.** The same component that knows how messages are stored should know how active context is assembled from those messages.
2. **Consolidation orchestration stays outside ChatStore.** `get_context()` is a pure read. The agent or command decides whether to compact, calls the consolidator, then persists the summary through ChatStore.
3. **Historical tool calls must be provider-safe.** ChatStore must not emit invalid provider message sequences such as assistant `tool_calls` without corresponding tool messages.
4. **Filtering policy is per call, not only per store.** ChatStore is harness-scoped, but agents may need different historical tool-noise policies.
5. **Summary boundaries are explicit protocol semantics.** A compaction summary is not just an implementation detail; callers and tests must be able to observe that it exists and was applied.
6. **This is a dev-mode breaking migration.** The project has no production compatibility requirement for BEP 5. `MessageStore` and `ep_message_store` may be removed atomically in the implementation PR.

---

## The `ChatStore` Protocol

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

ToolNoiseFilter = Literal["keep_signatures", "strip_all", "keep_all"]
TokenEstimateSource = Literal["litellm", "fallback", "fallback-error"]
ToolResultStatus = Literal["success", "error", "unknown"]


@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    is_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

# Message is the existing dataclass from contract.py, restated here for reference.
# is_summary is the only new field added by BEP 5.


@dataclass(frozen=True)
class TokenEstimate:
    count: int
    tokenizer_model: str | None
    source: TokenEstimateSource


@dataclass(frozen=True)
class ContextResult:
    """Provider-ready context assembled by get_context()."""

    messages: list[dict[str, Any]]
    source_messages: list[Message]
    estimated_tokens: int
    tokenizer_model: str | None
    estimation_source: TokenEstimateSource
    filter_mode: ToolNoiseFilter
    summary_applied: bool
    summary_message_count_excluded: int
    latest_summary: Message | None = None


@dataclass(frozen=True)
class ChatMeta:
    chat_id: str
    message_count: int
    last_activity: datetime | None
    has_summary: bool
    latest_summary_at: datetime | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ChatStore(Protocol):
    # ── Turn persistence ──
    async def save_turn(
        self, chat_id: str, messages: Iterable[Message], *, turn_id: str | None = None
    ) -> None: ...

    # ── Context assembly: pure read, no consolidation ──
    async def get_context(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> ContextResult: ...

    # ── Compaction input: active + filtered but not provider-projected ──
    async def get_compaction_messages(
        self,
        chat_id: str,
        *,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> list[Message]: ...

    # ── Token estimation for diagnostics and commands ──
    async def estimate_tokens(
        self,
        chat_id: str,
        *,
        tokenizer_model: str | None = None,
        filter_mode: ToolNoiseFilter | None = None,
    ) -> TokenEstimate: ...

    # ── Compaction boundary persistence and inspection ──
    async def save_summary(self, chat_id: str, summary: str) -> None: ...
    async def get_summary(self, chat_id: str) -> Message | None: ...

    # ── Raw access ──
    async def get_messages(self, chat_id: str, *, active_only: bool = True) -> list[Message]: ...

    # ── Metadata ──
    async def list_chats(self) -> dict[str, ChatMeta]: ...
```

`@runtime_checkable` protocol checks may be used as a coarse smoke test that attributes exist. They are not a full signature-conformance test; integration tests must still exercise each method.

---

## Method Semantics

### `save_turn(chat_id, messages, *, turn_id=None)`

Persist one completed turn. This replaces `save_messages(chat_id, messages)`.

`turn_id` is optional. If omitted, implementations extract it from message metadata when possible. The API makes turns first-class without forcing all callers to manage turn IDs directly.

If a turn was aborted or interrupted, the caller may still persist sanitized messages through `save_turn()`. Aborted turn representation is specified in [Aborted Turn Semantics](#aborted-turn-semantics).

### `get_context(chat_id, *, tokenizer_model=None, filter_mode=None)`

Return provider-ready active conversation history. This is a **pure read**:

1. Load active messages: latest summary, if any, plus messages after that summary.
2. Apply historical tool-noise filtering according to `filter_mode`.
3. Project filtered messages to provider-ready `dict[str, Any]` messages.
4. Estimate tokens for the projected messages.
5. Return `ContextResult`.

`get_context()` does **not** accept `max_tokens` and does **not** compact, prune, or drop older active messages to fit a budget. The caller compares `ContextResult.estimated_tokens` to its own history budget and decides whether to consolidate.

`filter_mode=None` means “use the ChatStore default from harness configuration.” Agents can pass an explicit mode per call.

### `get_compaction_messages(chat_id, *, filter_mode=None)`

Return active messages after applying the same historical tool-noise policy used for context assembly, but before provider projection.

This method exists so consolidation does not summarize raw, tool-heavy history after `get_context()` has already determined that the effective context is too large. The consolidator receives filtered `Message` objects, preserving summary markers and metadata while avoiding verbose tool result bodies.

### `estimate_tokens(chat_id, *, tokenizer_model=None, filter_mode=None)`

Return a named `TokenEstimate` rather than a bare tuple. Token count is for the same projected messages `get_context()` would return — i.e., effective context tokens. Used by the `/tokens` command for diagnostics.

### `save_summary(chat_id, summary)`

Persist a compaction boundary. The summary string is produced by the harness-level consolidator; ChatStore stores it and treats it as the boundary for future active-context reads.

The protocol-level representation is:

```python
Message(
    llm_message={"role": "system", "content": f"Chat summary:\n{summary}"},
    is_summary=True,
)
```

Implementations may add metadata, but `is_summary=True` is the normative boundary marker.

### `get_summary(chat_id)`

Return the latest summary boundary message, or `None` if the chat has never been compacted. This makes summary boundaries observable for tests, debugging, `/compact`, and future UI surfaces.

### `get_messages(chat_id, *, active_only=True)`

Return raw, unfiltered `Message` objects.

- `active_only=True` returns the latest summary, if present, plus all messages after that summary.
- `active_only=False` returns the complete append log, including messages before the latest summary and all summary records.

Raw access never applies tool-noise filtering. Callers that need filtered active messages for compaction should use `get_compaction_messages()`.

### `list_chats()`

Return structured metadata for all chats as `dict[str, ChatMeta]`. Backend-specific data belongs in `ChatMeta.extra`, not in ad hoc top-level keys.

---

## Summary Boundary Semantics

Summary boundaries are part of the ChatStore contract.

A summary boundary is a `Message` with `is_summary=True`. The **latest** such message defines the active-context boundary.

For a stored log:

```text
m1
m2
summary_a  (is_summary=True)
m3
m4
summary_b  (is_summary=True)
m5
m6
```

- `get_summary(chat_id)` returns `summary_b`.
- `get_messages(chat_id, active_only=True)` returns `[summary_b, m5, m6]`.
- `get_messages(chat_id, active_only=False)` returns `[m1, m2, summary_a, m3, m4, summary_b, m5, m6]`.
- `get_context(chat_id)` assembles provider-ready messages from `[summary_b, m5, m6]` after filtering and projection.
- `ContextResult.summary_applied == True` and `summary_message_count_excluded == 5`.

This means the latest summary remains visible to the model as compressed prior context, while pre-summary raw messages are excluded from active context.

---

## Tool Noise Filtering

`get_context()` and `get_compaction_messages()` filter historical tool noise by default. The goal is to retain enough provenance for the model to avoid redundant tool calls while dropping verbose tool results.

### Filter Modes

- `"keep_signatures"` (default) — drop full tool result messages, and retain compact textual signatures of historical tool calls.
- `"strip_all"` — drop tool result messages and remove all historical tool-call signatures.
- `"keep_all"` — no filtering. Intended only for debugging, short-lived agents, or export-like flows; it may produce larger contexts and must still be provider-valid after projection.

### Provider-Safe Signature Encoding

Historical tool signatures MUST NOT be emitted as provider `tool_calls` unless the corresponding provider protocol also receives the required matching tool result messages. To avoid invalid sequences and provider-specific unknown-field handling, `keep_signatures` encodes historical tool calls as assistant content, not as live provider tool-call objects.

Example retained signature:

```text
[tool call: get_weather({"city":"Tokyo","units":"celsius"}) -> success]
```

This signature may be appended to the assistant message content or emitted as a compact assistant message adjacent to the original assistant content. The implementation should prefer preserving meaningful assistant text and adding signatures in a deterministic, compact format.

### Filtering Rules for `keep_signatures`

1. Drop historical `role: "tool"` messages from provider context.
2. For assistant messages with historical `tool_calls`, append compact textual signatures containing:
   - function name,
   - truncated arguments,
   - best-effort result status.
3. Remove provider-native `tool_calls` fields from historical messages before sending them to the LLM provider.
4. Skip assistant messages that have neither meaningful content nor retained signatures after filtering.

### Tool Result Status

`ToolResultStatus` is best-effort:

- `"success"` — a corresponding tool result is present and does not indicate an error using known conventions.
- `"error"` — a corresponding tool result is present and indicates an error using known conventions, such as an `error` field or explicit failed status.
- `"unknown"` — no corresponding tool result exists, the turn was interrupted, or the result schema is not recognized.

The BEP intentionally avoids placing non-standard fields such as `result_status` inside provider-native `tool_calls` entries.

---

## Consolidator Relationship

The `Consolidator` remains a harness-level service. It receives messages and returns a summary string. ChatStore persists that string as a summary boundary via `save_summary()`.

The agent receives both services and orchestrates the flow:

```python
# 1. Get provider-ready context.
ctx_result = await self._chat_store.get_context(
    chat_id,
    tokenizer_model=budget_model,
    filter_mode=self._tool_noise_filter,
)

# 2. If over budget, consolidate filtered active messages and persist summary.
if ctx_result.estimated_tokens > self._max_tokens:
    async with self._chat_compaction_lock(chat_id):
        # Re-check inside the lock because another actor may have compacted already.
        ctx_result = await self._chat_store.get_context(
            chat_id,
            tokenizer_model=budget_model,
            filter_mode=self._tool_noise_filter,
        )
        if ctx_result.estimated_tokens > self._max_tokens:
            messages = await self._chat_store.get_compaction_messages(
                chat_id,
                filter_mode=self._tool_noise_filter,
            )
            summary = await self._consolidator.consolidate(messages)
            await self._chat_store.save_summary(chat_id, summary)
            ctx_result = await self._chat_store.get_context(
                chat_id,
                tokenizer_model=budget_model,
                filter_mode=self._tool_noise_filter,
            )

# 3. Build turn context.
ctx = TurnContext(history=ctx_result.messages, ...)
```

The lock is intentionally outside ChatStore because the full sequence spans a read, an LLM call, a write, and a second read. ChatStore serializes individual mutations; the harness or actor runtime serializes the multi-step compaction decision for a given chat.

### Consolidation Budget Failure

Consolidation may fail if the filtered active messages exceed the consolidation model's context window. The consolidator should raise `ConsolidationError`. Initial BEP 5 implementation does not require recursive chunked summarization, but the error must be clean and must leave ChatStore unchanged.

A future BEP may add chunked or rolling compaction.

---

## First-Class Turns

Today, `turn_id` is a metadata field on `Message`, and the store is oblivious to turn boundaries. `save_turn()` makes this explicit at the API level while keeping `turn_id` optional.

### Immediate Benefits

- The API communicates intent: “this is a complete turn” vs. “here are some messages.”
- Implementations can use `turn_id` for indexing, grouping, or storage layout.
- Aborted turn handling becomes clearer because persisting a partial or sanitized turn is an explicit caller decision.

### Future Capabilities, Not Required by BEP 5

- `list_turns(chat_id) -> list[TurnMeta]`
- `get_turn(chat_id, turn_id) -> list[Message]`
- selective turn deletion or pruning
- turn-range compaction

---

## Aborted Turn Semantics

An aborted turn is a turn that did not complete normally because execution was interrupted, cancelled, or failed after producing partial messages.

Protocol requirements:

1. The caller decides whether to persist an aborted turn.
2. If persisted, messages from the aborted turn SHOULD include metadata such as:

   ```python
   metadata={"aborted": True, "abort_reason": "interrupted"}
   ```

3. `save_turn()` stores aborted messages like any other messages; it does not invent or infer abort state.
4. `get_context()` must produce provider-valid messages even when a historical turn was aborted mid-tool-call.
5. Missing tool results in aborted turns produce tool result status `"unknown"` under `keep_signatures`.
6. Provider-native dangling `tool_calls` from aborted historical turns must be stripped or converted to textual signatures before projection.

---

## Configuration Spec

`[harness.message_store]` is replaced by `[harness.chat_store]`:

```toml
# ── Before ──
[harness.message_store]
name = "_default"
store_dir = "./messages"

# ── After ──
[harness.chat_store]
name = "_default"
store_dir = "./messages"
tool_noise_filter = "keep_signatures"   # default: keep_signatures
```

The harness-level `tool_noise_filter` is the default. Individual agents may override it by passing an explicit `filter_mode` to `get_context()` and `get_compaction_messages()`.

ChatStore does not depend on `LLMClient` and does not perform LLM calls. It may depend on a tokenizer implementation such as `litellm.token_counter`, with a deterministic fallback heuristic.

---

## Impact on ReActAgent

### Constructor Signature

```python
# Before
class ReActAgent:
    def __init__(
        self,
        *,
        message_store: MessageStore,
        consolidator: Consolidator,
        ...
    ):
        self._message_store = message_store
        self._consolidator = consolidator

# After
class ReActAgent:
    def __init__(
        self,
        *,
        chat_store: ChatStore,
        consolidator: Consolidator,
        tool_noise_filter: ToolNoiseFilter | None = None,
        ...
    ):
        self._chat_store = chat_store
        self._consolidator = consolidator
        self._tool_noise_filter = tool_noise_filter
```

### `ask()` Method

`_get_chat_history()` is removed as a history-loading/consolidation method. `ask()` calls ChatStore directly and uses the standalone consolidator for budget decisions.

```python
ctx_result = await self._chat_store.get_context(
    chat_id,
    tokenizer_model=budget_model,
    filter_mode=self._tool_noise_filter,
)

if ctx_result.estimated_tokens > self._max_tokens:
    async with self._chat_compaction_lock(chat_id):
        ctx_result = await self._chat_store.get_context(
            chat_id,
            tokenizer_model=budget_model,
            filter_mode=self._tool_noise_filter,
        )
        if ctx_result.estimated_tokens > self._max_tokens:
            messages = await self._chat_store.get_compaction_messages(
                chat_id,
                filter_mode=self._tool_noise_filter,
            )
            summary = await self._consolidator.consolidate(messages)
            await self._chat_store.save_summary(chat_id, summary)
            ctx_result = await self._chat_store.get_context(
                chat_id,
                tokenizer_model=budget_model,
                filter_mode=self._tool_noise_filter,
            )

ctx = TurnContext(history=self._format_history(ctx_result), ...)

# ... at end of turn:
await self._chat_store.save_turn(chat_id, messages)
```

### New Protected Formatting Hook

NamedAgent still needs attribution. Instead of overriding the deleted `_get_chat_history()`, ReActAgent exposes a narrow formatting hook:

```python
class ReActAgent:
    def _format_history(self, result: ContextResult) -> list[dict[str, Any]]:
        return result.messages
```

Subclasses may transform already-filtered, provider-ready history messages. `result.source_messages` carries the raw `Message` objects (with metadata) that were used to produce `result.messages`, so subclasses that need per-message metadata for attribution or labeling can access it without a second store read. Subclasses do not reload messages, estimate tokens, or run consolidation.

### Deleted from ReActAgent

- `_get_chat_history()`
- direct import/use of `estimate_message_history_tokens()`
- direct `MessageStore` dependency

---

## Impact on NamedAgent

NamedAgent applies speaker attribution labels to history messages using per-message metadata
from `ContextResult.source_messages`.

```python
class NamedAgent(ReActAgent):
    def _format_history(self, result: ContextResult) -> list[dict[str, Any]]:
        return [self._history_item(src) for src in result.source_messages]
```

`_history_item()` reads `Message.metadata` (e.g., `speaker_type`, `from_display`, `to_display`)
to produce labels like `[Alice -> Bob]: ...`. It returns a provider-ready dict derived from
`Message.llm_message` with the labeled content. The method already works on `Message` objects
and does not need the projected dicts from `result.messages`.

The following NamedAgent-specific history mechanisms are removed:

- `_get_chat_history()` override
- `_get_history_messages()` helper
- `_filter_tool_noise_messages()`

`_history_item()` must be audited against post-filter source message shapes:

- no historical provider-native `tool_calls` on `Message.llm_message`,
- no historical `role: "tool"` messages under default filtering,
- compact tool signatures may appear as assistant text content,
- summary messages carry `is_summary=True` and `role: "system"`.

If an agent explicitly uses `filter_mode="keep_all"`, ChatStore source messages may still
contain historical `tool_calls` and `role: "tool"` messages. `_history_item()` must not
reintroduce provider-invalid tool sequences into the final dicts.

---

## Impact on Slash Commands

### `/compact`

Uses the standalone consolidator, then persists via ChatStore:

```python
messages = await agent._chat_store.get_compaction_messages(
    chat_id,
    filter_mode=agent._tool_noise_filter,
)
summary = await agent._consolidator.consolidate(messages)
await agent._chat_store.save_summary(chat_id, summary)
```

### `/history`

Uses raw messages. The command may offer active/all scope later.

```python
messages = await agent._chat_store.get_messages(chat_id, active_only=True)
```

### `/tokens`

```python
estimate = await agent._chat_store.estimate_tokens(
    chat_id,
    tokenizer_model=budget_model,
    filter_mode=agent._tool_noise_filter,
)
```

### `/chats`

```python
result: dict[str, ChatMeta] = await agent._chat_store.list_chats()
```

---

## Impact on Harness

The `AgentHarness` constructor replaces `message_store` config with `chat_store` config.

```python
# Before
class AgentHarness:
    def __init__(self, *, message_store=None, ...):
        self._message_store_cfg = message_store

# After
class AgentHarness:
    def __init__(self, *, chat_store=None, ...):
        self._chat_store_cfg = chat_store
```

`__aenter__` creates ChatStore instead of MessageStore:

```python
async def __aenter__(self):
    self.mail_route = ...
    self.llm = LLMClient(self._providers_cfg)
    self.chat_store = self._create_chat_store()
    self.consolidator = self._create_consolidator()
    self.memory = ...
    self.skills_loader = ...
    self.interceptor = ...
```

`create_agent()` passes `chat_store` instead of `message_store`:

```python
def create_agent(self, role=None, agent_cfg=None):
    kwargs = (agent_cfg or {}) | {
        "chat_store": self.chat_store,
        ...
    }
    return ep_agent.invoke(role, kwargs) if role else ReActAgent(**kwargs)
```

---

## Extension Point Changes

```python
# Before
ep_message_store = ExtensionPoint(description="Message store factory.")

# After
ep_chat_store = ExtensionPoint(description="Chat store factory.")
```

`ep_message_store` is removed. No compatibility adapter is required for BEP 5 because the project is still in development and has no production migration constraint.

---

## Component Taxonomy Update

| Component | Classification | Change |
|---|---|---|
| `ChatStore` | **Core Service** | New. Persistence + context assembly: load, filter, project, estimate, summary boundaries. |
| `MessageStore` | ~~Core Service~~ | Replaced by `ChatStore`. |
| `Consolidator` | **Core Service** | Unchanged. Harness-level LLM-backed summarizer. |
| `LLMClient` | **Core Service** | Unchanged. Not consumed by ChatStore. |
| `MailRoute` | **Core Service** | Unchanged. |

---

## What Stays Separate

| Concept | Location | Rationale |
|---|---|---|
| `ChatState` (cursors, aliases) | [chat_state.py](../../src/bos/core/chat_state.py) in actor layer | Routing metadata, not persistence. Different lifecycle: per workspace/session, not per chat log. |
| `SessionExecution` (task handles, interrupts) | [actor.py](../../src/bos/core/actor.py#L22-L28) | Runtime concurrency control, not persistence. |
| `TurnContext` (ephemeral turn state) | [agent.py](../../src/bos/core/agent.py#L106-L134) | Per-turn in-memory state. Not stored. |
| `_TaskList` | Agent / plugin per BEP 4 | Handled by plugin system, not chat persistence. |

---

## Error Handling

`get_context()` can fail at multiple points:

1. **Token estimation failure** — use deterministic fallback heuristic and set `estimation_source = "fallback"`; if the primary fallback path itself fails and a worst-case emergency heuristic is used, set `estimation_source = "fallback-error"` and log a warning.
2. **Message load failure** — raise `IOError`. Store corruption or unreadable files must surface to the caller.
3. **Projection/filtering failure** — raise `ValueError` for malformed stored messages that cannot be sanitized into provider-valid history.
4. **Consolidation failure** — raise `ConsolidationError` at the consolidator level. ChatStore is not involved and must remain unchanged.

`save_turn()` and `save_summary()` write failures raise `IOError`. Implementations must avoid partial writes; JSONL implementations should use per-chat file locking and append atomically under that lock.

---

## Concurrency

ChatStore may be accessed concurrently by multiple agents sharing a chat.

Implementations MUST provide:

- **Per-chat serialization for mutations:** `save_turn()` and `save_summary()` on the same `chat_id` are serialized.
- **Concurrent read safety:** `get_context()`, `get_compaction_messages()`, `get_messages()`, `estimate_tokens()`, `get_summary()`, and `list_chats()` are safe during concurrent reads and during a mutation on the same chat. Readers see a consistent point-in-time snapshot.

The multi-step consolidation sequence is **not** made atomic by ChatStore:

```text
get_context → if over budget → get_compaction_messages → consolidate → save_summary → get_context
```

The harness or actor runtime must serialize this sequence with a per-chat compaction lock when multiple agents may write to the same chat. The agent must re-check the budget after acquiring the lock.

---

## System Prompt Budget Exclusion

Token estimates from ChatStore cover conversation history only. System prompt, tool definitions, skill descriptions, subagent manifests, and other per-turn scaffolding are managed separately by the agent.

The caller is responsible for reserving adequate headroom before comparing `ContextResult.estimated_tokens` to the usable history budget.

---

## Test Plan

1. **Protocol behavior** — JSONL and InMem ChatStore implementations exercise every method; runtime Protocol checks are only smoke tests.
2. **Summary boundary round-trip** — `save_summary()` persists `is_summary=True`; `get_summary()` returns latest summary; active reads include latest summary and post-summary messages only.
3. **Context assembly** — `get_context()` applies active summary boundary, filtering, provider-safe projection, and token estimation.
4. **Compaction input** — `get_compaction_messages()` returns active, filtered `Message` objects; consolidator does not receive raw verbose tool results under default filtering.
5. **Tool noise filtering** — verify `keep_signatures`, `strip_all`, and `none`; no provider-invalid dangling historical `tool_calls`; statuses include `success`, `error`, and `unknown`.
6. **Turn-level save/load** — `save_turn()` preserves turn metadata and extracts `turn_id` from message metadata when not provided.
7. **Aborted turn persistence** — aborted metadata preserved; missing tool result becomes `unknown`; provider projection remains valid.
8. **NamedAgent attribution** — `_format_history()` reads `source_messages` metadata to produce attribution labels; returns provider-valid dicts without reloading, filtering, or consolidating.
9. **Concurrent access** — concurrent `save_turn()` calls on same chat serialize; reads during writes see consistent snapshots; compaction lock re-check prevents stale compaction decisions.
10. **Consolidation failure recovery** — consolidator failure raises cleanly; no summary boundary is written; previous context remains readable.
11. **Command compatibility** — `/compact`, `/history`, `/tokens`, and `/chats` use ChatStore APIs and structured return types.
12. **Token diagnostics** — `/tokens` uses `estimate_tokens()` and displays effective context token count with named `TokenEstimate` fields.

---

## Migration Path

This migration is allowed to be breaking because the project is still in development and has no production users to preserve.

The implementation should land as one atomic PR that keeps the repository green at each commit where practical, but does not need a compatibility adapter.

1. Define `ChatStore`, `ContextResult`, `TokenEstimate`, `ChatMeta`, filter/scope literals, and updated `Message` semantics in `contract.py` or adjacent core contract modules.
2. Replace `ep_message_store` with `ep_chat_store`.
3. Implement `JsonlChatStore` using the existing JSONL file format where possible; summary records continue to use `Message(is_summary=True)`.
4. Implement `InMemChatStore` with equivalent behavior.
5. Add shared filtering/projection helpers for provider-safe historical tool signatures.
6. Update `AgentHarness` config and construction from `message_store` to `chat_store`.
7. Update `ReActAgent` to use `get_context()`, `get_compaction_messages()`, `save_summary()`, and `save_turn()`; add per-chat compaction locking and `_format_history()` hook.
8. Update `NamedAgent` to attribution-only `_format_history()` and remove history-loading/filtering overrides.
9. Update slash commands to use ChatStore methods and structured metadata.
10. Update tests per the test plan.
11. Update README and architecture docs that mention MessageStore or `[harness.message_store]`.
12. Remove obsolete MessageStore implementations, imports, fixtures, and tests.

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-20 | Initial draft | Propose merging `MessageStore` + consolidation behavior into a unified `ChatStore`. |
| 2026-05-20 | Revised per consolidated review | Narrow scope to persistence + context assembly. Consolidator stays harness-level. `get_context()` is pure read. |
| 2026-05-22 | Revised after independent post-revision reviews | Remove misleading `max_tokens`; add per-call filter mode, filtered compaction input, explicit summary boundary semantics, provider-safe tool signatures, structured token/chat metadata, NamedAgent formatting hook, aborted-turn semantics, and dev-mode breaking migration stance. |
