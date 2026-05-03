# Memory Enhancement Design

Status: **design** — contract and tool surface finalized, ready for implementation planning.

---

## Core Insight

Memory in BOS AI has two cognitive roles:

| | Maxim | Memory |
|---|---|---|
| **Cognitive role** | Deeply held principles that drive behavior and decisions | Episodic/semantic records that inform decisions |
| **Change rate** | Deliberate, slow, curated | Frequent, incremental, accumulated |
| **Load pattern** | Injected into system prompt every turn | Queried on demand via tool call |
| **Authority** | Operator-configured keys, agent-curated content | Agent freely creates and manages |
| **Filter** | `maxims = ["user", "soul"]` in agent config | No allow-list on write; filter on recall |

Both are backed by a single extension. The distinction is access temperature, not storage.

## Extension Point: `ep_memory`

A single extension point replacing the current `ep_memory_store`.

### Protocol

```python
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

@dataclass
class MemoryEntry:
    id: str
    content: str          # full content from get_memory; truncated snippet from search_memories
    tags: list[str]
    created_at: str
    metadata: dict | None = None


@runtime_checkable
class MemoryExtension(Protocol):
    # ── Maxims: preloaded into system prompt every turn ──
    async def get_maxim(self, key: str) -> str: ...
    async def set_maxim(self, key: str, content: str) -> None: ...
    async def list_maxims(self) -> dict[str, str]: ...

    # ── Memories: searched on demand ──
    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]: ...
    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str: ...
    async def get_memory(self, entry_id: str) -> MemoryEntry | None: ...
    async def forget_memory(self, entry_id: str) -> None: ...

    # ── Background optimization ──
    async def optimize(self) -> None: ...
```

### Framework behavior

- At turn start, `list_maxims()` is called. Results are filtered against the agent's `maxims` allow-list, then injected into the system prompt as a `--- MAXIMS ---` section.
- Maxim keys are framework-configured. The agent cannot create new keys via tools — `Remember` with a `key` only writes to existing keys.
- Memories are freely created by the agent. No allow-list on write.
- `optimize()` is called by the framework on idle/cron. Extensions define what this means — deduplicate, promote memory → maxim, decay stale entries, run Nudge Engine, run Dreaming pipeline, nothing.
- The default implementation (`MarkdownMemoryExtension`) stores maxims as `maxims/<key>.md` and memories as `memories/<entry_id>.md`.

### Agent configuration migration

Current:
```toml
[[platform.agents]]
name = "main"
memories = ["user", "memory"]
exclude_memories = ["tasks"]
```

Proposed:
```toml
[[platform.agents]]
name = "main"
maxims = ["user", "soul"]
exclude_maxims = ["tasks"]
```

The `memories` field is deprecated and mapped to the new field during a transition period.

## Agent Tools

Three tools, routing to maxims or memories based on parameters:

### `Remember`

| Parameters | Target | Protocol call |
|---|---|---|
| `key`, `content` | Maxim | `set_maxim(key, content)` — full overwrite |
| `content`, `tags?` | Memory | `ingest_memory(content, tags)` — returns `entry_id` |

The pivot: `key` present → maxim write. No `key` → memory ingest.

### `Recall`

| Parameters | Target | Protocol call | Returns |
|---|---|---|---|
| `query`, `top_k?` | Memory | `search_memories(query, top_k)` | `[{id, snippet, tags}]` — lightweight summaries |
| `entry_id` | Memory | `get_memory(entry_id)` | `{id, content, tags, created_at}` — full entry |

Progressive loading: the agent searches with a query, gets ranked snippets, decides which entry is relevant, then fetches full content by `entry_id`. Avoids bloating the context with full content of every match.

Maxims are already in the system prompt — no search needed.

### `Forget`

| Parameters | Target | Protocol call |
|---|---|---|
| `query` | Memory | `search_memories(query)` → `forget_memory()` on each match |
| `entry_id` | Memory | `forget_memory(entry_id)` |

Removes matching memories. Does not touch maxims. When the agent needs to record "why" something was forgotten (e.g., user said stop referencing project X), it calls `Remember(key="user", content="...")` as a separate step.

## Maxim Scopes

Each maxim key has a defined scope, documented in the system prompt so the agent knows what belongs where:

| Key | Scope | Example content |
|---|---|---|
| `user` | What you know about the user — preferences, background, projects, style, constraints they've asked you to follow. | "User prefers synchronous patterns. TypeScript for frontend, Python for backend. Lives in UTC+8." |
| `soul` | Your character and operating philosophy — how you approach problems, your communication style, what you value. | "Communicate concisely. Prefer working code over explanations. Ask clarifying questions when ambiguous." |
| `identity` | Who you are — your role, your purpose, the system you're part of. | "You are a research assistant for the BOS AI project. Your primary user is the lead developer." |
| `rules` | Hard constraints — things you must never do, formats you must follow, boundaries you cannot cross. | "Never modify files outside the workspace. Never include secrets in responses. Always confirm before destructive operations." |

Maxim keys are static and operator-configured. The agent cannot create new keys — it can only update the content of existing ones via `Remember(key, content)`.

### Maxim length limit

Each maxim is capped at **2048 characters**. This is a hard framework-enforced limit, not extension-configurable. The rationale matches Hermes' design: unbounded maxims silently bloat the system prompt every turn. A cap forces the agent to curate what matters most.

When the agent calls `Remember(key, content)` and the content exceeds 2048 characters, the framework rejects the write with an error message telling the agent the current length and the limit. The agent should then summarize rather than expanding.

The `optimize()` hook can also be used by extensions to proactively compact maxims that approach the limit.

## System Prompt Integration

```
--- MAXIMS ---

* **user** (your knowledge about the user — preferences, background, projects, style)
```
(content)
```

* **soul** (your character and operating philosophy — how you work, communicate, and make decisions)
```
(content)
```

* **identity** (who you are — your role, purpose, and context)
```
(content)
```

* **rules** (hard constraints — things you must always or never do)
```
(content)
```
```

Each maxim header includes its scope description so the agent understands what content belongs in each key. The descriptions are framework-provided, not extension-defined.

Memories (cold) are never injected into the system prompt. They only appear when the agent calls `Recall`.

## Memory Usage Prompt

The following prompt is injected into the system prompt to teach the agent how to use its memory effectively:

```
--- USING YOUR MEMORY ---

You have two kinds of memory, accessed through three tools: Remember, Recall, and Forget.

## Maxims (your principles)

Maxims are deeply held convictions that shape how you behave and make decisions.
They are always visible to you — they appear above in the MAXIMS section.
Think of them as your conscience, not your notepad.

Each maxim has a defined scope, described in its header. Respect the scope — put user preferences in "user", not in "rules".

Use Remember(key, content) to update a maxim when:

- The user explicitly asks you to change how you operate.
  Example: "From now on, always use TypeScript instead of JavaScript."
  → Remember(key="user", content="User prefers TypeScript over JavaScript for all projects.")

- You discover a fundamental truth about the user that should change your default behavior.
  Example: The user always rejects async solutions in favor of sync alternatives.
  → Remember(key="user", content="User strongly prefers synchronous patterns. Default to sync unless async is unavoidable.")

- You are given a new rule or constraint you must follow.
  Example: "Never deploy on Fridays."
  → Remember(key="rules", content="Never deploy on Fridays. Deployments only on Mon-Thu before 14:00 UTC.")

Do NOT use maxims for:
- Facts about projects, tools, or APIs — those are memories.
- Meeting notes, code snippets, URLs — those are memories.
- Anything you might need once or twice but doesn't change who you should be.
- New categories of information — maxim keys are fixed. Use memory tags instead.

When updating a maxim, you are overwriting the ENTIRE content. Include all existing
information alongside your changes. The system cannot merge for you — you must read
the current maxim content (visible above), incorporate the new information, and write
the complete updated text.

Each maxim has a hard limit of 2048 characters. If your update would exceed this,
you must summarize and prioritize rather than expanding. Focus on what matters most
for shaping your future behavior. The system will reject writes that exceed the limit.

## Memories (your knowledge)

Memories are facts, experiences, and details you accumulate over time.
They are NOT visible to you by default — you must Recall them when needed.
Think of them as a searchable notebook, not your working memory.

Use Remember(content, tags?) to record a memory when:

- You learn a factual detail that might matter later.
  Example: "The user's database is on AWS RDS, us-east-1, PostgreSQL 16."
  → Remember(content="User's production database: AWS RDS, us-east-1, PostgreSQL 16. Connection via pgbouncer.", tags=["infra", "database"])

- The user shares context you should carry forward across sessions.
  Example: "We're building a CLI tool for managing Kubernetes secrets."

- You complete a task and want to record the outcome for future reference.
  Example: "Deployed v2.3.1 to staging. All tests passed. Rollback window: 24h."

Use Recall(query, top_k?) to search your memories:

- Before answering a question that might depend on past context.
  Example: The user asks "what's the status of the migration?" → Recall(query="migration status")

- When the user references something you don't fully remember.
  Example: "Remember that bug we fixed last month?"

- After Recall returns snippets: if a snippet looks relevant and you need full detail,
  fetch it with Recall(entry_id=...).

Use Forget(entry_id) or Forget(query) to remove memories:

- The user explicitly asks you to forget something.
  Example: "Stop bringing up project X — we're done with it."
  → Recall(query="project X") → identify entries → Forget(entry_id=...) on each
  → Then: Remember(key="user", content="...user asked to stop referencing project X...")

- Information is clearly stale or contradicted by newer information.
  Example: Two memories contradict each other about the same topic. Keep the newer one, Forget the stale one.

## Memory hygiene

- Write memories AFTER the conversation, not during it. If you're mid-task, focus on the task. Record learnings when the user pauses or the topic concludes.
- Be concise. A memory entry is a note to your future self, not a transcript.
- Use tags. They help you find things later with Recall.
- When in doubt, write it. A slightly noisy memory is better than a lost insight.
- If you update a maxim, be thorough. Read the current content, merge carefully, and write the complete updated text.
```

## Default Implementation

`MarkdownMemoryExtension` replaces `MarkdownMemoryStore`:

- Maxims: `<store_dir>/maxims/<key>.md` — one file per key, same as current behavior
- Memories: `<store_dir>/memories/<entry_id>.md` — one file per entry
- `search_memories`: substring match across all memory files (upgraded later with optional embedding support)
- `optimize`: no-op by default

This is a pure renaming + protocol expansion. Existing memory files are trivially migrated.

## Extension Philosophy

The framework owns: what gets loaded into the system prompt, when optimization fires, the tool surface.

The extension owns: how storage works, what optimization means.

This lets the same tool surface back completely different memory philosophies:

| Extension style | Maxim storage | Memory storage | `optimize()` behavior |
|---|---|---|---|
| Default (Markdown) | `maxims/*.md` | `memories/*.md`, substring search | No-op |
| Hermes-style | Hard-capped MEMORY.md / USER.md | SQLite FTS5 + optional vectors | Nudge Engine review agent |
| OpenClaw-style | SOUL.md / IDENTITY.md | `memory/YYYY-MM-DD.md` daily logs | Dreaming pipeline (Light → REM → Deep) |
| Obsidian-style | Tagged identity notes | Full vault | Backlink analysis, graph maintenance |

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-02 | Initial design drafted | Capture memory enhancement vision after researching Hermes and OpenClaw role models |
