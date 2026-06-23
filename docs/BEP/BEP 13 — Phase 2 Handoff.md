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
- **Track A is also done now (2026-06-23): the `bos.protocol` shim is deleted** — every call site imports
  the owning foundation directly, the content helpers and WS constants moved to their real homes, and a
  guard ([test_no_protocol_shim.py](../../tests/test_no_protocol_shim.py)) blocks reintroduction. See §3.
- **Track B / B1 is also done now (2026-06-23): the assembly ring (`bos.core`) is extracted** — zero
  outward imports, guard-enforced ([test_assembly_ring_isolation.py](../../tests/test_assembly_ring_isolation.py)),
  with `config`/`extensions`/`exts` confirmed outside it. See BEP §3.1 and §4 below.
- **Track B / B2a is also done now (2026-06-23): the config ring (`bos.config`) is extracted** — imports
  only inward, guard-enforced ([test_config_ring_isolation.py](../../tests/test_config_ring_isolation.py)).
  See BEP §3.2. Remaining in Track B: the gateway, then cli/runner.

Gate is green: `uv run pytest -q` (662), `uv run ruff check src tests`, `npx -y pyright src` (0), all four
ring guards (agent, actor, assembly) + the no-shim guard.

---

## 2. The invariants the next phase must not break

1. **Foundations stay zero-dependency.** `bos.core.agent` and `bos.core.actor` import stdlib + own leaves
   only — never each other, never `bos.protocol`, never outward. The guards fail CI on regression. Do not
   relax them.
2. **Dependencies point inward only.** Every ring imports the owning foundation/inner ring directly.
3. **`bos.protocol` is gone — never reintroduce it.** Import the owning foundation directly
   (`bos.core.agent` / `bos.core.actor`). The no-shim guard fails CI on any `from bos.protocol import …`.
4. **A ring is "done" only against the §1.6 checklist** (zero outward imports + a guard, owns its ports,
   inner defaults, no inner-privacy reaches, re-exports keep call sites stable, gate green).

---

## 3. Track A — Retire the `bos.protocol` shim ✅ DONE

**Complete (2026-06-23).** `bos.protocol` is deleted; every call site imports the owning foundation
directly. The two ownership decisions the table flagged are resolved:

| Former shim export | Resolved home |
|---|---|
| `TurnEvent`, `MessageContent`, `MessageContentPart` | `bos.core.agent` (imported directly) |
| `Envelope`, `MessageType` | `bos.core.actor` (imported directly) |
| `content.py` helpers (`content_to_plain_text`, `content_preview`, `content_length`, `image_source_to_model_url`) | **agent ring** — merged into `core/agent/_content.py` (deduped the copy of `_validate_source`), exported from `bos.core.agent` |
| `WS_TAKEOVER_CLOSE_CODE`, `WS_TAKEOVER_CLOSE_REASON` | **gateway** — defined in `gateway/channels/ws_channel.py` (the WS transport that emits them), re-exported from `bos.gateway`; `client.py` imports the definition site directly to avoid a `__init__` cycle |

What was done: re-pointed ~19 source files + ~15 test files; relocated the two non-wire surfaces above;
deleted `src/bos/protocol/`; scrubbed the stale `bos.protocol` mentions from the foundation/guard
docstrings; added the permanent anti-regression guard
[test_no_protocol_shim.py](../../tests/test_no_protocol_shim.py) (asserts the package is gone *and* that
no source/test imports it). Gate green: 660 passed, ruff clean, pyright 0, both ring guards.

Invariant #3 below now reads as an absolute: there is no shim to import — always import the owning
foundation.

---

## 4. Track B — Formally extract the outer rings (inside → out)

Apply the §1.6 checklist ring by ring. Order matters: extract inner before outer so each rests on a
finished ring.

**B1 — assembly ring: `bos.core` (harness · contract · registry · events · llm · `defaults`). ✅ DONE
(2026-06-23, BEP §3.1).** Verified zero outward imports; added
[test_assembly_ring_isolation.py](../../tests/test_assembly_ring_isolation.py) enforcing (1) no import to
`gateway`/`cli`/`runner`/`extensions`/`config`/`exts` and (2) no foundation-private reach. Decisions
settled during extraction:
- **`defaults` is *in* the ring.** The harness imports `DefaultEventBus`/`DefaultBackgroundLLM` directly
  (intra-ring); the other five defaults are `@ep_*(name="_default")`, resolved by name and force-imported
  by the composition root `bos.exts`.
- **`bos.extensions` and `bos.exts` are *out*** (your call): adapters injected via `ep_*`, and the
  composition root. Neither is imported by `bos.core`.
- **Topology correction: `bos.config` is *out*, a consumer of the harness** — it imports `bos.core`, never
  the reverse, so it is a ring *outward* of the assembly ring (the old "harness/config/extension" grouping
  was wrong on import direction).
- Resolved the one rule-4 reach: published the shared `_`-helpers from `bos.core.agent`'s `__init__` and
  re-pointed `core/_utils` to the package API (no more `from .agent._utils import …`).

**B2a — `bos.config`. ✅ DONE (2026-06-23, BEP §3.2).** Guard
[test_config_ring_isolation.py](../../tests/test_config_ring_isolation.py): no imports to
`gateway`/`cli`/`runner`/`extensions`/`exts`; no underscore-private `bos.core` reach. The `"bos.exts"` /
`"./extensions"` occurrences in `schema.py`/`workspace.py` are **string defaults** for the extension
loader, not imports — the AST guard does not flag them. Resolved the one rule-4 reach by publishing
`_resolve_path` from `bos.core` and re-pointing `workspace.py` off `bos.core._utils`.

**B2b — `bos.gateway`** (next). Already structurally organized (`core/`, `actors/`, `channels/`) with **no
inner-privacy reaches** into agent/actor (verified — the `_`-prefixed hits are its own methods).
`AgentActor` + control plane are seated here. Audit that it imports only inward (foundations + assembly
ring + config, no `cli`/`runner`), give it a guard. The ws-takeover constants already landed here
(Track A): `WS_TAKEOVER_CLOSE_CODE`/`WS_TAKEOVER_CLOSE_REASON` in `gateway/channels/ws_channel.py`,
re-exported from `bos.gateway`.

**B3 — `bos.cli` / `bos.runner` (outermost).** Process entrypoints. Audit + guard last.

Each extracted ring gets its own numbered subsection appended to BEP §3, capturing decisions made *during*
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
- **Watch for `__init__` cycles when re-exporting from a package root:** a same-ring consumer should
  import the *definition site*, not the package `__init__` that imports it (e.g. `gateway/client.py`
  imports `WS_TAKEOVER_*` from `.channels.ws_channel`, not from `bos.gateway`). External rings can use the
  package root.

---

## 6. How to verify (every step)

```
uv run pytest -q                      # full suite (currently 664)
uv run ruff check src tests           # no new findings
npx -y pyright src                    # must stay at 0 errors
uv run pytest -q tests/test_agent_ring_isolation.py tests/test_actor_ring_isolation.py tests/test_assembly_ring_isolation.py tests/test_config_ring_isolation.py tests/test_no_protocol_shim.py
```

Commit in small, reversible diffs with conventional titles (`refactor(...)`, `docs(bep): ...`). Keep all
three gates + the ring guards green per commit.

---

## 7. Suggested next move

Track A, B1, and B2a are done. Continue **Track B — extract the remaining outer rings, inside → out** (§4):

1. **B2b — `bos.gateway`** (next). Audit it imports only inward (foundations + assembly ring + config, no
   `cli`/`runner`), then guard it — model on
   [test_config_ring_isolation.py](../../tests/test_config_ring_isolation.py). Watch for the same rule-4
   reaches (any underscore-private `bos.core`/foundation import) and decide whether the guard scans the
   whole `bos/gateway/**` tree (it has `core/`/`actors/`/`channels/` subpackages).
2. **B3 — `bos.cli` / `bos.runner`** (outermost) last.

Append a numbered subsection to BEP §3 per ring as you go, capturing decisions made *during* extraction.
