# BEP 13 — Phase 2 Handoff

Companion to [BEP 13: Clean Architecture — Concentric Dependency Rings](./BEP%2013%3A%20Clean%20Architecture%20%E2%80%94%20Concentric%20Dependency%20Rings.md).
Read the BEP first — it is the design of record. This doc is the **working state + next-phase plan**, not
new design. Branch: `re-arch`.

---

## 1. Where Phase 1 left things (done)

The two innermost rings are extracted, isolated, and guard-enforced:

- **`bos.core.agent`** (domain foundation) and **`bos.core.actor`** (system foundation) each import stdlib
  + their own leaves only. Two CI guards assert it:
  [test_agent_ring_isolation.py](../../tests/test_agent_ring_isolation.py),
  [test_actor_ring_isolation.py](../../tests/test_actor_ring_isolation.py).
- The actor foundation owns the generic primitives: `Envelope[ContentT=Any]`, `MailBox[ContentT=Any]`,
  `MessageType`, and the type-keyed `EventBus` + `Event` marker.
- **`AgentActor`** (composes both foundations) lives in the **gateway** and is a **pure data-plane actor**;
  the slash-command **control plane** is a mailbox-free gateway `CommandHandler` over the `ChatCoordinator`.
- The event model is settled: `TurnEventSink` (Plane 1, emit-only) vs. `EventBus`/`SessionEvent` (Plane 2,
  broadcast). See BEP §2.6.

Gate is green: `uv run pytest -q` (658), `uv run ruff check src tests`, `npx -y pyright src` (0), both ring
guards.

---

## 2. The invariants the next phase must not break

1. **Foundations stay zero-dependency.** `bos.core.agent` and `bos.core.actor` import stdlib + own leaves
   only — never each other, never `bos.protocol`, never outward. The guards fail CI on regression. Do not
   relax them.
2. **Dependencies point inward only.** Every ring imports the owning foundation/inner ring directly.
3. **`bos.protocol` is a shim, not a dependency target.** Do not add new `from bos.protocol import …`.
   New code imports the owning foundation. (See §3 — the goal is to delete the shim.)
4. **A ring is "done" only against the §1.6 checklist** (zero outward imports + a guard, owns its ports,
   inner defaults, no inner-privacy reaches, re-exports keep call sites stable, gate green).

---

## 3. Track A — Retire the `bos.protocol` shim

`bos.protocol` exists only for back-compat and is slated for deletion (BEP topology + Non-Goal #3). The
foundations already don't depend on it; in-tree `bos.core` mostly imports foundations directly. What
remains is to migrate the ~19 consumer source files (+ ~17 test files) off the shim, then delete it.

**The shim surface is broader than wire-type re-exports — this is the non-obvious part:**

| Shim export | What it is | Likely home after migration |
|---|---|---|
| `TurnEvent`, `MessageContent`, `MessageContentPart` | agent-ring data | `bos.core.agent` (import directly) |
| `Envelope`, `MessageType` | actor-ring primitives | `bos.core.actor` (import directly) |
| `bos.protocol.content` helpers (`image_source_to_model_url`, `content_preview`, `content_to_plain_text`) | content utilities over `MessageContent` | **needs a decision** — most likely an agent-foundation leaf (they operate on agent content) |
| `WS_TAKEOVER_CLOSE_CODE`, `WS_TAKEOVER_CLOSE_REASON` | WebSocket wire constants | **needs a decision** — these are gateway/WS-transport concerns, candidate for `bos.gateway` (channels) |

**Suggested order:**
1. Re-point the pure wire-type imports (`TurnEvent`/`Envelope`/`MessageType`/`MessageContent`) to their
   owning foundations — mechanical, no design decision. Consumers: `core/events.py`, `core/defaults/*`,
   `extensions/**`, `gateway/**`, `plugins/**`, `cli/**`, and the matching tests.
2. Decide homes for `content.py` and the `WS_TAKEOVER_*` constants (the table above), relocate, re-point.
3. Delete `src/bos/protocol/` and the three docstring mentions in the foundations
   (`core/agent/__init__.py`, `core/agent/_content.py`, `core/actor/__init__.py`).
4. Optional safety rail: add a guard test forbidding any new `from bos.protocol import …` while the
   migration is in flight, then drop it once the package is gone.

Worklist (current `bos.protocol` consumers in `src/`): `cli/commands/agent.py`, `cli/tui_app.py`,
`core/events.py`, `core/defaults/{jsonl_chat_store,jsonl_mailbox,litellm_provider}.py`,
`extensions/channels/{lark,telegram}.py`, `extensions/chat_stores/in_memory.py`,
`extensions/mailboxes/in_memory.py`, `extensions/providers/codex_provider.py`,
`gateway/{client.py,actors/agent_actor.py,channels/ws_channel.py,core/actor_resolver.py,core/chat_coordinator.py}`,
`plugins/{plan,task,memory/auto_recall}.py`. Plus the test files that import it.

---

## 4. Track B — Formally extract the outer rings (inside → out)

Apply the §1.6 checklist ring by ring. Order matters: extract inner before outer so each rests on a
finished ring.

**B1 — assembly ring: `bos.core` (harness/config/extension) + `defaults`.**
- Good news: it has **no outward imports to `gateway`/`cli`/`runner`/`extensions`** today (verified).
- Work: confirm it owns the ports outer rings implement (it largely does — `MailRoute`, `Channel`,
  `ep_*`, `DefaultEventBus`), confirm inner defaults exist per port, and add an isolation guard analogous
  to the foundation guards (assert `bos.core` modules import only stdlib / foundations / themselves —
  *not* `bos.gateway`/`bos.cli`/`bos.runner`/`bos.extensions`).
- Note `bos.extensions` is a separate concern: it holds **adapters** (chat stores, mailboxes, providers,
  channels). Adapters legitimately live in an outer ring and are injected inward; decide whether
  `extensions` is part of the assembly ring or a ring outside it before writing its guard.

**B2 — `bos.gateway`.**
- Already structurally organized (`core/`, `actors/`, `channels/`) and has **no inner-privacy reaches**
  into agent/actor (verified — the `_`-prefixed hits are its own methods). `AgentActor` + control plane
  already seated here.
- Work: audit that it imports only inward (foundations + assembly ring, no `cli`/`runner`), give it a
  guard, and confirm the ws-takeover constants land here if Track A §2 decides so.

**B3 — `bos.cli` / `bos.runner` (outermost).** Process entrypoints. Audit + guard last.

Each extracted ring gets its own numbered section appended to BEP §3, capturing decisions made *during*
extraction (per the BEP's ring-by-ring method) — don't pre-guess them here.

---

## 5. Gotchas / things easy to get wrong

- **PEP 696 generics:** bare `Envelope`/`MailBox` mean `[Any]`; the harness ring annotates
  `[MessageContent]` for precision. Keep call sites unannotated where they were — no churn.
- **`session_close` is a precondition, not a guarantee** (BEP §2.6): emitted only on explicit retirement,
  not on abandonment. Don't write subscribers assuming universal end-of-life delivery.
- **The reply is a message, never an event:** `Agent.ask()`'s return is sent via `mailbox.send(...)`
  *before* `turn_complete` is emitted. Don't route replies through `EventBus`.
- **Re-export, don't move-and-break:** when a symbol's home moves inward, re-export from the old module so
  public call sites stay stable; migrate call sites separately.
- **Tests import the shim too** — Track A is not done until the test suite is off `bos.protocol` as well.

---

## 6. How to verify (every step)

```
uv run pytest -q                      # full suite (currently 658)
uv run ruff check src tests           # no new findings
npx -y pyright src                    # must stay at 0 errors
uv run pytest -q tests/test_agent_ring_isolation.py tests/test_actor_ring_isolation.py
```

Commit in small, reversible diffs with conventional titles (`refactor(...)`, `docs(bep): ...`). Keep all
three gates + the ring guards green per commit.

---

## 7. Suggested first move

Start with **Track A step 1** (mechanical wire-type re-pointing) — it shrinks the shim to just `content.py`
+ the WS constants and makes the two remaining ownership decisions concrete and small. Then take the
`content.py`/WS-constant home decisions, delete the shim, and only then begin Track B (assembly-ring
guard first).
