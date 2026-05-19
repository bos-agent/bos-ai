# Harness-Level Consolidator Service

Status: **design** — contract, configuration precedence, and runtime ownership finalized; ready for implementation planning.

---

## Core Insight

Consolidation is a harness-level runtime service, not an agent-personality concern.

An agent decides **when** its conversation history needs compaction because the agent owns its dynamic history budget. The consolidator decides **how** to compact because summarization, extraction, and distillation may need a different model and instruction than the agent uses for normal turns.

This distinction matters:

| | Agent | Consolidator |
|---|---|---|
| **Owns** | `max_tokens`, compaction trigger, current turn flow | Model, instruction, summary/extraction behavior |
| **Scope** | Agent-specific runtime policy | Harness service shared by agents |
| **Default model source** | Agent config / runtime override | `harness.consolidator.model` / environment |
| **System prompt budget** | Excluded from dynamic history compaction budget | Not involved |
| **Future uses** | Conversation flow | Chat compaction, memory extraction, history distillation |

The consolidator is a reusable reasoning service over stored `Message` objects. It should not be tied to each agent's model by default, because an agent model may be too expensive, too slow, or simply not well suited for compression and extraction.

## Non-Compatibility Stance

BOS AI is still under active development. This BEP intentionally permits breaking changes.

Do **not** add compatibility shims for the old consolidator contract. In particular:

- Do not preserve `consolidate(messages: list[dict], ...)`.
- Do not add adapter layers that accept both dicts and `Message` objects.
- Do not keep old call sites alive by silently projecting `[m.llm_message for m in messages]`.
- Do not make the code messier to protect external consolidator implementations.

The clean contract is the migration path.

## Extension Point: `ep_consolidator`

The consolidator remains an extension point, but the protocol changes to operate on native message records.

### Protocol

```python
from typing import Protocol, runtime_checkable


@runtime_checkable
class Consolidator(Protocol):
    async def consolidate(
        self,
        messages: list[Message],
        instruction: str | None = None,
    ) -> str: ...
```

`Message` is the existing framework dataclass:

```python
@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    is_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

The consolidator receives `Message` objects so it can see summary identity, timestamps, turn IDs, and metadata. Prompt-ready dict projection is a caller/helper concern, not the extension contract.

### Framework Behavior

- The harness constructs one consolidator service during `AgentHarness.__aenter__()`.
- The consolidator receives the shared harness `LLMClient`.
- Agents call the consolidator when their dynamic history budget is exceeded.
- `/compact` also calls the same consolidator service.
- `/tokens`, automatic compaction, and manual compaction use the same history projection and token estimation helper.
- The built system prompt is excluded from the compaction budget.
- Stored summaries remain `Message(is_summary=True, llm_message={"role": "system", "content": "Chat summary:\n..."})`.

## Model Configuration

The consolidator model is resolved independently from agent models.

Resolution order:

1. `harness.consolidator.model`
2. `BOS_CONSOLIDATOR_MODEL`
3. `BOS_MODEL`
4. Abort bootstrap if none is available

Do **not** fall back to `platform.agent_defaults.model`. Agent defaults and harness service defaults are separate configuration domains.

### Example Configuration

```toml
[harness.consolidator]
model = "gemini/gemini-2.5-flash"
instruction = """
Summarize the conversation history for future turns.
Preserve user intent, decisions, unresolved tasks, tool results, and important constraints.
Avoid transcript style.
"""
```

Environment-only setup:

```bash
export BOS_CONSOLIDATOR_MODEL="gemini/gemini-2.5-flash"
```

Fallback setup:

```bash
export BOS_MODEL="gemini/gemini-3.1-pro-preview"
```

If none of these sources is present, the harness must fail early with an error that names the accepted sources.

## Instruction Model

The current implementation only needs chat compaction, but the contract should not hardcode chat compaction so deeply that future distillation workflows require another break.

The consolidator owns:

- default instruction text
- optional instruction presets
- provider/model-specific prompt shaping

The `instruction` parameter supports one-off overrides without changing the method shape.

Future built-in instruction profiles may include:

| Profile | Purpose | Current scope |
|---|---|---|
| `chat_compaction` | Compact chat history for future turns | Implement now as default behavior |
| `memory_extraction` | Extract key points for memories or maxims after a turn | Future |
| `history_distillation` | Periodically distill longer-term historical information | Future |

Memory update and dreaming/distillation are explicitly out of scope for this BEP's implementation, but the design must not block them.

## Dynamic History Budget

`max_tokens` remains an agent-owned threshold.

The agent decides when to compact. The consolidator does not inspect or own context-window thresholds.

The budget applies to dynamic conversation/history content. It does not include:

- built system prompt
- maxims injected into the system prompt
- tool definitions
- static harness/agent instructions

The rationale is practical: those static components are expected to hit provider cache most of the time, while history grows turn by turn and drives compaction pressure.

## History Projection and Token Estimation

Introduce one shared helper for history projection and token estimation.

The helper should:

- accept `list[Message]`
- produce prompt-projected `list[dict]`
- preserve current prompt-history behavior, including tool-output truncation
- exclude the built system prompt
- return token estimate metadata

Token counting:

1. Primary path: `litellm.token_counter(model=<budget_model>, messages=<prompt_projected_history>)`
2. Fallback path: `ceil(len(serialized_text) / 3) + 8 * message_count`

The fallback is intentionally conservative and must be reported as such.

`/tokens` response must include:

- estimated token count
- model used for estimation
- source: `litellm` or `fallback`

## Repeated Compaction

Repeated compaction must be summary-aware.

Primary signal:

- `Message.is_summary`

Compatibility fallback:

- `llm_message.role == "system"` and content starts with `Chat summary:`

Prior summaries are folded into the next summary as existing summary context. They are not treated as ordinary conversation turns and should not produce duplicated nested `Chat summary:` prefixes.

The persisted message format does not change.

## Command Surface

### `/compact`

Manual compaction must use the same `Message`-based consolidator path as automatic compaction.

It must not call:

```python
agent._consolidator.consolidate([m.llm_message for m in messages])
```

### `/tokens`

Token reporting must use the shared token estimation helper.

It must not report:

```text
Approx chars: N · ~N // 4 tokens
```

The response should identify the estimate model and source.

## Configuration Wording

The `max_tokens` comment in `src/bos/config/template.toml` should describe an agent-owned dynamic history compaction budget.

It should not describe:

- the full model context window
- total prompt size including system prompt
- consolidator-owned behavior

## Implementation Plan

1. Update `Consolidator` in `src/bos/core/contract.py` to accept `list[Message]`.
2. Replace the default `NaiveConsolidator` with an LLM-backed harness-level consolidator.
3. Construct `LLMClient` before the consolidator, or otherwise pass the shared `LLMClient` into consolidator construction.
4. Resolve consolidator model from `harness.consolidator.model`, `BOS_CONSOLIDATOR_MODEL`, then `BOS_MODEL`; abort if none exists.
5. Keep `max_tokens` on `ReActAgent`; use it only to decide when history should be compacted.
6. Add the shared history projection/token estimation helper.
7. Update `ReActAgent._get_chat_history()`, `/compact`, and `/tokens` to use the helper and `Message` contract.
8. Make repeated compaction summary-aware using `Message.is_summary`.
9. Update `src/bos/config/template.toml` and any relevant help text.
10. Remove old dict-based consolidator call sites instead of adapting them.

## Acceptance Criteria

- Harness bootstrap aborts clearly when no consolidator model is configured and `BOS_CONSOLIDATOR_MODEL` / `BOS_MODEL` are unset.
- Explicit `harness.consolidator.model` takes precedence over `BOS_CONSOLIDATOR_MODEL`, which takes precedence over `BOS_MODEL`.
- `platform.agent_defaults.model` is not used as a consolidator model fallback.
- Default LLM compaction calls `LLMClient.complete()` with the resolved consolidator model.
- `Consolidator.consolidate()` receives `list[Message]`, not prompt-projected dicts.
- No compatibility shim accepts both `list[dict]` and `list[Message]`.
- `ReActAgent.max_tokens` remains the trigger threshold and is not moved into the consolidator.
- Automatic compaction no longer uses raw `content_length()` sum as the primary threshold.
- The built system prompt is excluded from compaction-budget accounting.
- `/compact` and `/tokens` use the same shared history projection/token accounting helper as automatic compaction.
- `/tokens` includes estimation model and source: `litellm` or `fallback`.
- Prior summaries are folded using `Message.is_summary` without duplicating `Chat summary:` prefixes.
- JSONL message-store schema remains unchanged.
- Closeable harness-level consolidators are closed during harness teardown.
- The consolidator contract remains capable of accepting custom instructions for future memory/dreaming workflows.

## Test Plan

### Contract and Default Consolidator

- Verify consolidators receive `list[Message]`.
- Verify old dict input is not accepted or silently adapted.
- Verify default LLM-backed consolidator calls `LLMClient.complete()` with the resolved consolidator model.
- Verify custom instruction override works without changing the contract.

### Model Resolution

- `harness.consolidator.model` wins.
- `BOS_CONSOLIDATOR_MODEL` is used when config model is absent.
- `BOS_MODEL` is used when both config model and `BOS_CONSOLIDATOR_MODEL` are absent.
- Bootstrap aborts clearly when none are available.
- `platform.agent_defaults.model` is ignored for consolidator fallback.

### History Budget and Token Accounting

- Shared helper accepts `list[Message]` and returns prompt dicts plus token metadata.
- Tool output truncation matches current runtime behavior.
- Built system prompt is excluded.
- LiteLLM token counter path records source `litellm`.
- Fallback path records source `fallback` and computes `ceil(len(serialized_text) / 3) + 8 * message_count`.
- `/tokens` includes model and source.

### Repeated Compaction

- Prior `Message.is_summary` content is folded into new summary context.
- `Chat summary:` prefix is compatibility fallback only.
- New summary does not duplicate nested `Chat summary:` prefixes.
- Store schema remains unchanged.

### Command Surface

- `/compact` passes `Message` objects to the consolidator.
- `/compact` saves summary using the existing store format.
- `/tokens` uses shared token estimation and does not use `char_count // 4`.

## Verification Commands

```bash
uv run pytest -q tests/test_harness.py tests/test_actor_commands.py tests/test_multimodal_support.py
uv run ruff check src tests
uv run pytest -q
```

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Bootstrap becomes stricter for users without a model env/config | Error must name `harness.consolidator.model`, `BOS_CONSOLIDATOR_MODEL`, and `BOS_MODEL` |
| External custom consolidators break | Accepted; this is an intentional development-stage protocol break |
| Token estimates differ from a specific agent model | Accepted; consolidation is harness-level and `/tokens` reports the model used |
| Summary quality degrades over repeated compaction | Use `Message.is_summary` and test repeated compaction |
| Future memory/dreaming workflows need different prompts | Keep instruction override and future preset shape in the contract |

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-04 | Initial BEP 3 drafted | Capture harness-level LLM consolidator design after option debate and clarify intentional breaking contract migration |

