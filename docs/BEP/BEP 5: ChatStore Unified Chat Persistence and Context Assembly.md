# BEP 5: ChatStore — Unified Chat Persistence and Context Assembly

Status: **design** — revised per consolidated review, ready for implementation planning.

---

## Core Insight

Currently, [ReActAgent](file:///home/jzhang/bos-ai/src/bos/core/agent.py#L181) orchestrates chat history management across three separate abstractions:

1. **`MessageStore`** — a thin append/read protocol for individual LLM messages, keyed by `chat_id`.
2. **`Consolidator`** — an LLM-backed summarizer called when the history exceeds the token budget (harness-level service, stays unchanged).
3. **`_get_chat_history`** — agent-internal logic that loads messages, estimates tokens, triggers consolidation, reloads, and projects messages into LLM format.

Additionally, [NamedAgent](file:///home/jzhang/bos-ai/src/bos/named_actors/actor.py#L42) overrides `_get_chat_history` to filter tool call noise and add speaker attribution. The tool noise filtering is universally useful — all agents should benefit, not just named actors.

By introducing a **`ChatStore`** protocol that unifies persistence and context assembly (load, filter, project), the agent loop simplifies: `get_context()` replaces `_get_chat_history`. The consolidator remains a harness-level service; ChatStore provides `save_summary()` as the public contract for persisting its results.

---

## The `ChatStore` Protocol

```python
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, runtime_checkable

@dataclass
class ContextResult:
    """Result of get_context(). Messages are LLM-ready dicts (filtered, projected)."""
    messages: list[dict[str, Any]]
    estimated_tokens: int
    tokenizer_model: str | None
    estimation_source: str                # "litellm" | "fallback"

@runtime_checkable
class ChatStore(Protocol):
    # ── Turn persistence ──
    async def save_turn(
        self, chat_id: str, messages: Iterable[Message], *, turn_id: str | None = None
    ) -> None: ...

    # ── Context assembly (pure read: load + filter + project) ──
    async def get_context(
        self,
        chat_id: str,
        *,
        max_tokens: int,
        tokenizer_model: str | None = None,  # inference model tokenizer, NOT consolidation model
    ) -> ContextResult: ...

    # ── Token estimation (for /tokens command, diagnostics) ──
    async def estimate_tokens(
        self, chat_id: str, *, tokenizer_model: str | None = None
    ) -> tuple[int, str, str]: ...  # (count, model, source)

    # ── Compaction boundary persistence ──
    async def save_summary(self, chat_id: str, summary: str) -> None: ...

    # ── Raw access ──
    async def get_messages(self, chat_id: str, *, original: bool = False) -> list[Message]: ...

    # ── Metadata ──
    async def list_chats(self) -> dict[str, dict[str, Any]]: ...
```

### Method Semantics

**`save_turn(chat_id, messages, *, turn_id=None)`** — Persist a completed turn. Replaces `save_messages(chat_id, messages)`. `turn_id` is optional; when `None`, implementations extract it from the messages' metadata. The explicit parameter makes turns a first-class concept for future capabilities (turn listing, selective deletion, turn-range compaction) without forcing callers to track IDs separately.

**`get_context(chat_id, *, max_tokens, tokenizer_model)`** — Return budget-aware, filtered, LLM-ready conversation history. `tokenizer_model` specifies the inference model whose tokenizer is used for estimation — this is the model that will consume the context, not the consolidation model (which stays on the harness-level consolidator). `get_context()` is a **pure read** with no side effects:

1. Load messages (respecting summary boundaries)
2. Apply tool noise filtering (retain brief tool call signatures, drop full tool results — see §Tool Noise Filtering)
3. Project to LLM format (including content truncation: tool results capped at 150 chars)
4. Estimate tokens and return `ContextResult`

Auto-consolidation is NOT performed. The caller (agent) decides whether to consolidate using the standalone consolidator, then calls `save_summary()` to persist the result and re-fetches context.

**`estimate_tokens(chat_id, *, tokenizer_model)`** — Return token count, model, and estimation source without filtering or projecting. Used by `/tokens` command for diagnostics. Implementations use `litellm.token_counter` with a character-based fallback.

**`save_summary(chat_id, summary)`** — Persist a compaction boundary. The summary string is produced by the harness-level consolidator; ChatStore stores it and treats it as a boundary for subsequent `get_context()` calls (only messages after the latest summary are loaded). This is the public contract between the consolidator and persistence.

**`get_messages(chat_id, *, original=False)`** — Return raw, unfiltered messages. `original=True` returns all messages including those before the latest summary; `original=False` (default) returns only post-summary messages. For slash commands (`/history`, `/tokens`), debugging, and export. No filtering applied.

**`list_chats()`** — Return metadata for all chats. Used by the `/chats` command.

---

## Consolidator Relationship

The `Consolidator` remains a harness-level service (unchanged). ChatStore provides `save_summary()` as the public contract for persisting its results — the storage layer, not the consolidation orchestrator. Background uses (self-evolution, cross-chat extraction) work directly with the consolidator, independent of ChatStore.

The agent receives both and orchestrates the flow:

```python
# 1. Get filtered, projected context (pure read)
ctx_result = await self._chat_store.get_context(
    chat_id, max_tokens=self._max_tokens, tokenizer_model=budget_model
)

# 2. If over budget, consolidate then persist summary
if ctx_result.estimated_tokens > self._max_tokens:
    messages = await self._chat_store.get_messages(chat_id)
    summary = await self._consolidator.consolidate(messages)
    await self._chat_store.save_summary(chat_id, summary)
    ctx_result = await self._chat_store.get_context(
        chat_id, max_tokens=self._max_tokens, tokenizer_model=budget_model
    )

# 3. Build turn context
ctx = TurnContext(history=ctx_result.messages, ...)
```

---

## Tool Noise Filtering

`get_context()` filters tool noise from historical turns by default. The goal is to retain enough provenance for the model to avoid redundant tool calls while dropping verbose tool results.

### Filtering Rules

1. **Drop** all `role: "tool"` messages — tool results are too verbose to retain across turns.
2. **Retain brief tool call signatures** on assistant messages that have `tool_calls`:
   - Keep: function name, truncated arguments (~80 chars), and success/failure status
   - Drop: full tool result bodies
3. **Skip** assistant messages that have `tool_calls` but no meaningful content (tool-call-only messages carry no useful context once results are dropped).

### Success/Failure Status

Derived from the corresponding tool result message: absence of `error` field → `"success"`, presence of `error` → `"error"`. Gives the model enough signal to know whether a prior tool call succeeded without keeping the full result.

### Example

Before filtering (3 messages, ~600 tokens):
```
assistant: {role: "assistant", content: "Let me check.", tool_calls: [{function: {name: "get_weather", arguments: "{\"city\": \"Tokyo\", \"units\": \"celsius\"}"}}]}
tool:      {role: "tool", content: "{\"temperature\": 22, \"humidity\": 65, \"wind_speed\": 12, ...}"}
assistant: {role: "assistant", content: "It's 22°C and sunny in Tokyo."}
```

After filtering (2 messages, ~80 tokens):
```
assistant: {role: "assistant", content: "Let me check.", tool_calls: [{function: {name: "get_weather", arguments: "{\"city\": \"Tokyo\", ...}"}, result_status: "success"}]}
assistant: {role: "assistant", content: "It's 22°C and sunny in Tokyo."}
```

### Configurability

The filter is configurable per-agent via `tool_noise_filter` setting:
- `"keep_signatures"` (default) — retain function name + truncated args + success/failure, drop full results
- `"strip_all"` — drop all tool messages and strip all tool_calls (current NamedAgent behavior)
- `"none"` — no filtering, keep all messages (debugging, short-lived agents)

Raw access via `get_messages()` returns everything unfiltered regardless of this setting.

---

## First-Class Turns

Today, `turn_id` is a metadata field on `Message`, and the store is oblivious to turn boundaries. `save_turn()` makes this explicit at the API level while keeping `turn_id` optional — it can be extracted from message metadata when not provided.

### Immediate Benefits

- The API communicates intent: "this is a complete turn" vs "here are some messages"
- Implementations can use `turn_id` for indexing, grouping, or storage layout
- Aborted turn handling becomes clearer — `save_turn` with aborted messages is a conscious choice

### Future Capabilities (not in initial protocol)

- `list_turns(chat_id) -> list[TurnMeta]` — turn-by-turn history display
- `get_turn(chat_id, turn_id) -> list[Message]` — fetch a specific turn
- Selective turn deletion or pruning
- Turn-range compaction

These can be added to the protocol later without breaking changes.

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
# tool_noise_filter = "keep_signatures"   # optional
```

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
        consolidator: Consolidator,   # still passed; agent orchestrates
        ...
    ):
        self._chat_store = chat_store
        self._consolidator = consolidator
```

### `ask()` Method

`_get_chat_history` is deleted. The agent calls `get_context()` for context assembly, uses the standalone consolidator for budget decisions, and calls `save_turn()` for persistence:

```python
# Before
ctx = TurnContext(
    history=await self._get_chat_history(chat_id, budget_model=budget_model),
    ...
)
# ... at end of turn:
await self._message_store.save_messages(chat_id, messages)

# After
ctx_result = await self._chat_store.get_context(
    chat_id, max_tokens=self._max_tokens, tokenizer_model=budget_model
)
if ctx_result.estimated_tokens > self._max_tokens:
    messages = await self._chat_store.get_messages(chat_id)
    summary = await self._consolidator.consolidate(messages)
    await self._chat_store.save_summary(chat_id, summary)
    ctx_result = await self._chat_store.get_context(
        chat_id, max_tokens=self._max_tokens, tokenizer_model=budget_model
    )
ctx = TurnContext(history=ctx_result.messages, ...)
# ... at end of turn:
await self._chat_store.save_turn(chat_id, messages)
```

### Deleted from Agent

- `_get_chat_history()` — replaced by `ChatStore.get_context()` + consolidator orchestration
- Import of `estimate_message_history_tokens` — token estimation is `get_context()`-internal, surfaced via `ContextResult`

---

## Impact on NamedAgent

[NamedAgent](file:///home/jzhang/bos-ai/src/bos/named_actors/actor.py#L42) currently overrides `_get_chat_history` to perform tool noise filtering, attribution labeling, and its own consolidation check. With `ChatStore`:

- **Tool noise filtering** — handled by `get_context()`, no longer needed in NamedAgent
- **Consolidation** — handled by the standard agent consolidation flow (consolidator stays harness-level)
- **Attribution labeling** — remains NamedAgent-specific

The override simplifies to attribution-only:

```python
class NamedAgent(ReActAgent):
    async def _get_chat_history(self, chat_id: str, *, budget_model: str | None = None) -> list[dict]:
        ctx_result = await self._chat_store.get_context(
            chat_id, max_tokens=self._max_tokens, tokenizer_model=budget_model
        )
        return [self._history_item(m) for m in ctx_result.messages]
```

The `_filter_tool_noise_messages` method and `_get_history_messages` helper can be deleted. `_history_item` must be audited against post-filter message shapes (tool_calls stripped from messages with content, tool messages removed) to ensure attribution labels are correct.

---

## Impact on Slash Commands

### `/compact`

Uses the standalone consolidator directly, then persists via ChatStore:

```python
# Before
messages = list(await agent._message_store.get_messages(chat_id))
summary = await agent._consolidator.consolidate(messages)
await agent._message_store.save_summary(chat_id, summary)

# After
messages = list(await agent._chat_store.get_messages(chat_id))
summary = await agent._consolidator.consolidate(messages)
await agent._chat_store.save_summary(chat_id, summary)
```

### `/history`

```python
# Before
messages = await agent._message_store.get_messages(chat_id)

# After
messages = await agent._chat_store.get_messages(chat_id)
```

### `/tokens`

Uses `estimate_tokens()` for metadata:

```python
# Before
messages = list(await agent._message_store.get_messages(chat_id))
projection = estimate_message_history_tokens(messages, budget_model=budget_model)

# After
count, model, source = await agent._chat_store.estimate_tokens(chat_id, tokenizer_model=budget_model)
messages = await agent._chat_store.get_messages(chat_id)  # for detailed breakdown
```

### `/chats`

```python
# Before
result = await agent._message_store.list_chats()

# After
result = await agent._chat_store.list_chats()
```

---

## Impact on Harness

The `AgentHarness` constructor replaces `message_store` config with `chat_store` config:

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

`__aenter__` creates ChatStore instead of MessageStore. ChatStore does NOT depend on LLMClient:

```python
async def __aenter__(self):
    self.mail_route = ...
    self.llm = LLMClient(self._providers_cfg)
    self.chat_store = self._create_chat_store()     # replaces message_store; no LLMClient dependency
    self.consolidator = self._create_consolidator()
    self.memory = ...
    self.skills_loader = ...
    self.interceptor = ...
```

`create_agent` passes `chat_store` instead of `message_store`:

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
# ep_message_store removed
```

---

## Component Taxonomy Update

| Component | Classification | Change |
|---|---|---|
| `ChatStore` | **Core Service** | New. Persistence + context assembly (load, filter, project). |
| `MessageStore` | ~~Core Service~~ | Replaced by `ChatStore`. |
| `LLMClient` | **Core Service** | Unchanged. Not consumed by ChatStore. |
| `MailRoute` | **Core Service** | Unchanged. |

---

## What Stays Separate

| Concept | Location | Rationale |
|---|---|---|
| `ChatState` (cursors, aliases) | [chat_state.py](file:///home/jzhang/bos-ai/src/bos/core/chat_state.py) in actor layer | Routing metadata, not persistence. Different lifecycle (per-workspace, not per-chat). |
| `SessionExecution` (task handles, interrupts) | [actor.py](file:///home/jzhang/bos-ai/src/bos/core/actor.py#L22-L28) | Runtime concurrency control, not persistence. |
| `TurnContext` (ephemeral turn state) | [agent.py](file:///home/jzhang/bos-ai/src/bos/core/agent.py#L106-L134) | Per-turn, in-memory. Not stored. |
| `_TaskList` | Agent / Plugin (per BEP 4) | Handled by plugin system, not chat persistence. |

---

## Error Handling

`get_context()` can fail at multiple points. The degradation model:

1. **Token estimation failure** (LiteLLM unavailable, fallback also fails) → use worst-case character-based heuristic (`len(json.dumps(...)) / 3 + overhead`), set `estimation_source = "fallback-error"`, log warning.
2. **Message load failure** → raise `IOError`. Store corruption must surface to the caller.
3. **Consolidation failure** (LLM error during `consolidator.consolidate()`) → raise `ConsolidationError` at the consolidator level. ChatStore is not involved; the agent handles the error.

`save_turn()` and `save_summary()`:
- Write failures raise `IOError`. Implementations must ensure writes are atomic (no partial state on failure).

---

## Concurrency

ChatStore may be accessed concurrently by multiple agents sharing a chat (named actors in multi-agent conversations). Implementations MUST provide:

- **Per-chat serialization for mutations**: `save_turn()` and `save_summary()` on the same `chat_id` are serialized.
- **Concurrent read safety**: `get_context()`, `get_messages()`, and `estimate_tokens()` are safe for concurrent reads, even during a mutation on the same `chat_id` (readers see a consistent snapshot).

Consolidation atomicity (`check budget → consolidate → save_summary → re-fetch`) is the caller's responsibility, not ChatStore's. The agent/harness must serialize the full consolidation flow if concurrent consolidation on the same chat is possible.

---

## Test Plan

1. **Protocol conformance** — JSONL and InMem implementations satisfy `ChatStore` (`isinstance(check, ChatStore)`)
2. **Consolidation round-trip** — consolidator produces summary → `save_summary` persists it → `get_context()` scopes to post-summary messages
3. **Tool noise filtering** — all three rules verified; tool call signature retention (name + args + success/failure); `strip_all` and `none` modes
4. **Turn-level save/load** — round-trips preserve turn boundaries; `turn_id` extracted from message metadata when not provided
5. **Aborted turn persistence** — aborted turns sanitized correctly; `get_context()` handles abort messages
6. **NamedAgent attribution** — `_history_item()` produces correct labels on post-filter messages from `get_context()`
7. **Concurrent access** — concurrent `save_turn()` on same `chat_id` serialized; `get_context()` during mutation returns consistent snapshot
8. **Consolidation failure recovery** — LLM failure raises cleanly at consolidator level; store remains consistent
9. **Command compatibility** — `/compact`, `/history`, `/tokens`, `/chats` work through ChatStore

---

## System Prompt Budget Exclusion

The `max_tokens` budget passed to `get_context()` covers ONLY conversation history. System prompt, tool definitions, skill descriptions, and subagent manifests are managed separately by the agent and are not included in the `max_tokens` value. The caller is responsible for reserving adequate headroom.

---

## Migration Path

1. **Define `ChatStore` protocol** and `ContextResult` in `contract.py`. Remove `MessageStore`.
2. **Implement `JsonlChatStore`** — wraps existing JSONL storage with `get_context()` (filtering + projection), `save_turn()`, and `estimate_tokens()`. No consolidator dependency.
3. **Update `AgentHarness`** — create `ChatStore` instead of `MessageStore`. `[harness.message_store]` config becomes `[harness.chat_store]`.
4. **Update `ReActAgent`** — replace `message_store` param with `chat_store`. Delete `_get_chat_history`. Add consolidation orchestration inline.
5. **Update `NamedAgent`** — simplify `_get_chat_history` override to attribution-only. Audit `_history_item` against post-filter messages. Delete `_filter_tool_noise_messages` and `_get_history_messages`.
6. **Update slash commands** — point at `chat_store` instead of `message_store`. `/tokens` uses `estimate_tokens()`. `/compact` uses consolidator + `save_summary()`.
7. **Update `InMemChatStore`** — mirror changes for the in-memory implementation (token estimation via fallback heuristic).
8. **Update tests** — adapt fixtures and assertions per the test plan above.

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-20 | Initial draft (BEP 5) | Propose merging `MessageStore` + `Consolidator` into a unified `ChatStore`. |
| 2026-05-20 | Revised per consolidated review | Narrow scope to persistence + context assembly only. Consolidator stays harness-level. `get_context()` is pure read. `tokenizer_model` naming. Optional `turn_id`. Tool call signature retention. Error handling, concurrency, test plan added. |
