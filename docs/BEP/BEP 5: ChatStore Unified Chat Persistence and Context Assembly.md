# BEP 5: ChatStore — Unified Chat Persistence and Context Assembly

Status: **design** — protocol surface agreed, ready for implementation planning.

---

## Core Insight

Currently, [ReActAgent](file:///home/jzhang/bos-ai/src/bos/core/agent.py#L181) orchestrates chat history management across three separate abstractions:

1. **`MessageStore`** — a thin append/read protocol for individual LLM messages, keyed by `chat_id`.
2. **`Consolidator`** — an LLM-backed summarizer called when the history exceeds the token budget.
3. **`_get_chat_history`** — agent-internal logic that loads messages, estimates tokens, triggers consolidation, reloads, and projects messages into LLM format.

This forces the agent loop to own context-window management policy — token estimation, compaction decisions, re-loading after summary — which should be the store's responsibility. The agent should ask "give me the context for this chat" and "save this turn," nothing more.

Additionally, [NamedAgent](file:///home/jzhang/bos-ai/src/bos/named_actors/actor.py#L42) overrides `_get_chat_history` to filter tool call noise from historical turns and add speaker attribution. The tool noise filtering is a universally useful behavior that all agents should benefit from, not just named actors.

By introducing a **`ChatStore`** protocol that combines persistence, context assembly, and consolidation into a single Core Service, the agent loop becomes purely mechanical: receive input → get context → run LLM → execute tools → save turn.

---

## The `ChatStore` Protocol

```python
from typing import Any, Iterable, Protocol, runtime_checkable

@runtime_checkable
class ChatStore(Protocol):
    # ── Turn persistence ──
    async def save_turn(
        self, chat_id: str, turn_id: str, messages: Iterable[Message]
    ) -> None: ...

    # ── Context assembly (budget-aware, noise-filtered) ──
    async def get_context(
        self,
        chat_id: str,
        *,
        max_tokens: int,
        budget_model: str | None = None,  # inference model tokenizer, NOT consolidation model
    ) -> list[Message]: ...

    # ── Raw access ──
    async def get_messages(self, chat_id: str) -> list[Message]: ...
    async def save_summary(self, chat_id: str, summary: str) -> None: ...

    # ── Metadata ──
    async def list_chats(self) -> dict[str, dict[str, Any]]: ...
```

### Method Semantics

**`save_turn(chat_id, turn_id, messages)`** — Persist a completed turn. Replaces the current `save_messages(chat_id, messages)`. The `turn_id` parameter makes turns a first-class concept at the API level. Implementations may use it for grouping, indexing, or future turn-level operations.

**`get_context(chat_id, *, max_tokens, budget_model)`** — Return budget-aware, LLM-ready conversation history. `budget_model` specifies the inference model whose tokenizer is used for token estimation — this is the model that will consume the context, not the model used for consolidation (which is configured on the ChatStore at construction time). Internally performs three operations:
1. Load messages (respecting summary boundaries)
2. Filter tool noise from historical turns (drop `role: "tool"` messages, strip `tool_calls` from assistant messages)
3. Auto-consolidate if estimated tokens exceed `max_tokens`

Returns `list[Message]` — the caller projects to LLM format (`m.llm_message`) and may apply additional transforms (e.g., NamedAgent attribution).

**`get_messages(chat_id)`** — Return raw, unfiltered messages. For slash commands (`/history`, `/tokens`), debugging, and export. No filtering, no consolidation.

**`save_summary(chat_id, summary)`** — Append a summary message. Exposed for explicit compaction via `/compact`. Also called internally by `get_context()` during auto-consolidation.

**`list_chats()`** — Return metadata for all chats. Used by the `/chats` command.

---

## Consolidator Absorption

The `Consolidator` becomes an internal dependency of `ChatStore`, not a standalone harness-level service. The agent no longer holds or knows about the consolidator.

### Current Wiring

```python
# In AgentHarness.__aenter__
self.message_store = self._create_and_own("ep_message_store", MessageStore, ...)
self.consolidator = self._create_consolidator()  # separate service

# In AgentHarness.create_agent
return ReActAgent(
    message_store=self.message_store,
    consolidator=self.consolidator,   # agent owns consolidation orchestration
    ...
)
```

### New Wiring

```python
# In AgentHarness.__aenter__
self.llm = LLMClient(self._providers_cfg)          # must come before chat_store
self.chat_store = self._create_chat_store()         # internally creates consolidator

# In AgentHarness.create_agent
return ReActAgent(
    chat_store=self.chat_store,
    # no consolidator parameter
    ...
)
```

The concrete `ChatStore` implementation takes `LLMClient` and consolidation config at construction time:

```python
class JsonlChatStore:
    def __init__(
        self,
        store_dir: str | Path | None = None,
        bos_dir: str | Path | None = None,
        *,
        llm: LLMClient | None = None,
        consolidation_model: str | None = None,
        consolidation_instruction: str | None = None,
    ) -> None:
        ...
        self._consolidator = Consolidator(llm=llm, model=consolidation_model, ...)
```

---

## Tool Noise Filtering

Historical tool call messages are noise for the LLM — they consume tokens without providing useful context. `get_context()` filters them by default:

1. **Drop** all `role: "tool"` messages (tool results from prior turns)
2. **Strip** `tool_calls` from assistant messages that also have content — keep only the content
3. **Skip** assistant messages that have `tool_calls` but no meaningful content

This is the same logic currently in [NamedAgent._filter_tool_noise_messages](file:///home/jzhang/bos-ai/src/bos/named_actors/actor.py#L59-L74), promoted to a universal default.

Raw access via `get_messages()` returns everything unfiltered.

---

## First-Class Turns

Today, `turn_id` is a metadata field on `Message`, and the store is oblivious to turn boundaries. `save_turn()` makes this explicit at the API level.

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

Consolidator configuration moves under the chat store section. The separate `[harness.consolidator]` section is deprecated.

```toml
# ── Before ──
[harness.message_store]
name = "_default"
store_dir = "./messages"

[harness.consolidator]
model = "gemini/gemini-2.5-flash"

# ── After ──
[harness.chat_store]
name = "_default"
store_dir = "./messages"
consolidation_model = "gemini/gemini-2.5-flash"
# consolidation_instruction = "..."   # optional custom instruction
```

For backward compatibility during migration, the harness should check for the legacy `[harness.consolidator]` and `[harness.message_store]` sections and merge them into the chat store configuration with a deprecation warning.

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
        ...
    ):
        self._chat_store = chat_store
```

### `ask()` Method

The `_get_chat_history` method is deleted. The `_persist_turn` closure calls `save_turn` instead of `save_messages`:

```python
# Before
ctx = TurnContext(
    history=await self._get_chat_history(chat_id, budget_model=budget_model),
    ...
)
# ... at end of turn:
await self._message_store.save_messages(chat_id, messages)

# After
context_messages = await self._chat_store.get_context(
    chat_id, max_tokens=self._max_tokens, budget_model=budget_model
)
ctx = TurnContext(
    history=[m.llm_message for m in context_messages],
    ...
)
# ... at end of turn:
await self._chat_store.save_turn(chat_id, ctx.turn_id, messages)
```

### Deleted from Agent

- `_get_chat_history()` — absorbed by `ChatStore.get_context()`
- `_consolidator` field — consolidator is ChatStore-internal
- Import of `estimate_message_history_tokens` — token estimation is ChatStore-internal

---

## Impact on NamedAgent

[NamedAgent](file:///home/jzhang/bos-ai/src/bos/named_actors/actor.py#L42) currently overrides `_get_chat_history` to perform tool noise filtering, attribution labeling, and its own consolidation check. With `ChatStore`:

- **Tool noise filtering** — handled by `get_context()`, no longer needed in NamedAgent
- **Consolidation** — handled by `get_context()`, no longer needed in NamedAgent
- **Attribution labeling** — remains NamedAgent-specific

The override simplifies from ~12 lines to ~3:

```python
class NamedAgent(ReActAgent):
    async def _get_chat_history(self, chat_id: str, *, budget_model: str | None = None) -> list[dict]:
        messages = await self._chat_store.get_context(
            chat_id, max_tokens=self._max_tokens, budget_model=budget_model
        )
        return [self._history_item(m) for m in messages]
```

The `_filter_tool_noise_messages` method and `_get_history_messages` helper can be deleted.

---

## Impact on Slash Commands

### `/compact`

```python
# Before
messages = list(await agent._message_store.get_messages(chat_id))
summary = await agent._consolidator.consolidate(messages)
await agent._message_store.save_summary(chat_id, summary)

# After — two options:
# Option A: use existing protocol methods
messages = list(await agent._chat_store.get_messages(chat_id))
# ... still needs a consolidator reference for explicit compaction

# Option B: add compact() to protocol (recommended)
await agent._chat_store.compact(chat_id)
```

Adding `compact(chat_id)` to the protocol is clean — it encapsulates the load → consolidate → save-summary flow. This is optional for the initial version; the `/compact` command can call `save_summary` directly with a summary produced by the agent.

### `/history`

```python
# Before
messages = await agent._message_store.get_messages(chat_id)

# After — unchanged, uses raw access
messages = await agent._chat_store.get_messages(chat_id)
```

### `/tokens`

```python
# Before
messages = list(await agent._message_store.get_messages(chat_id))

# After — unchanged
messages = list(await agent._chat_store.get_messages(chat_id))
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

The `AgentHarness` constructor signature changes:

```python
# Before
class AgentHarness:
    def __init__(self, *, message_store=None, consolidator=None, ...):
        self._message_store_cfg = message_store
        self._consolidator_cfg = consolidator

# After
class AgentHarness:
    def __init__(self, *, chat_store=None, ...):
        self._chat_store_cfg = chat_store
```

The `__aenter__` startup order changes — `LLMClient` must be created before `ChatStore`:

```python
async def __aenter__(self):
    self.mail_route = ...
    self.llm = LLMClient(self._providers_cfg)      # moved up
    self.chat_store = self._create_chat_store()     # replaces message_store + consolidator
    self.memory = ...
    self.skills_loader = ...
    self.interceptor = ...
```

---

## Extension Point Changes

```python
# Before
ep_message_store = ExtensionPoint(description="Message store factory.")
ep_consolidator = ExtensionPoint(description="Consolidator factory.")

# After
ep_chat_store = ExtensionPoint(description="Chat store factory.")
# ep_consolidator removed — consolidation is ChatStore-internal
```

Backward compatibility: `ep_message_store` can remain as an alias for `ep_chat_store` during migration. Existing `MessageStore` implementations can be wrapped in a `ChatStore` adapter.

---

## Component Taxonomy Update

The following updates [BEP 4](file:///home/jzhang/bos-ai/docs/BEP/BEP%204:%20Micro-Kernel%20and%20Plugin%20Architecture.md)'s component taxonomy:

| Component | Classification | Change |
|---|---|---|
| `ChatStore` | **Core Service** | New. Replaces `MessageStore` + `Consolidator`. |
| `MessageStore` | ~~Core Service~~ | Absorbed into `ChatStore`. |
| `Consolidator` | ~~Core Service~~ | Absorbed into `ChatStore` as internal dependency. |
| `LLMClient` | **Core Service** | Unchanged. Now also consumed by `ChatStore`. |
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

## Migration Path

1. **Define `ChatStore` protocol** in `contract.py`. Keep `MessageStore` as a backward-compat alias.
2. **Implement `JsonlChatStore`** — wraps the existing JSONL storage with `get_context()` (tool noise filtering + auto-consolidation) and `save_turn()`.
3. **Update `AgentHarness`** — create `ChatStore` instead of separate `MessageStore` + `Consolidator`. Support legacy config keys with deprecation warnings.
4. **Update `ReActAgent`** — replace `message_store` + `consolidator` params with `chat_store`. Delete `_get_chat_history`.
5. **Update `NamedAgent`** — simplify `_get_chat_history` override to attribution-only.
6. **Update slash commands** — point at `chat_store` instead of `message_store` / `consolidator`.
7. **Update `InMemChatStore`** — mirror changes for the in-memory implementation.
8. **Update tests** — adapt fixtures and assertions.

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-20 | Initial draft (BEP 5) | Propose merging `MessageStore` + `Consolidator` into a unified `ChatStore` with first-class turns, tool noise filtering, and budget-aware context assembly. |
