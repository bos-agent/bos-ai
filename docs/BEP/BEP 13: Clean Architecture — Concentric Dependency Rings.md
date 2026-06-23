# BEP 13: Clean Architecture — Concentric Dependency Rings

Status: **living design** — graduated **ring by ring**, because the right boundary for each ring only
becomes clear while extracting it. The two innermost rings are done and documented here as the reference
shape; the outer rings adopt the same rules when they are extracted.

| Ring | Status |
|---|---|
| `bos.core.agent` — **domain foundation** | ✅ done — the reference shape (§1) |
| `bos.core.actor` — **system foundation** | ✅ done — a zero-dep peer of `agent` (§2) |
| `bos.protocol` — **migration shim** | ⏳ transitional — re-exports foundation types for legacy call sites; **to be eliminated** (not a ring; see topology) |
| `harness` · `config` · `extension (+defaults)` — assembly ring | ⬜ not yet formally extracted |
| `gateway`, then `cli` · `runner` — outermost | ⬜ not yet formally extracted (already hosts `AgentActor` + the control plane, §2.4–§2.5) |

**Motivation.** BOS grew outward from a single agent loop into a harness, plugin system, gateway,
channels, and CLI. Dependencies accreted in both directions — inner code reaching out to outer modules,
outer modules reaching into inner privates. This BEP applies Robert C. Martin's **Clean Architecture**:
make the dependency graph a set of concentric rings governed by one rule, and extract each ring so it is
testable and replaceable in isolation.

---

## The Dependency Rule

> **Source-code dependencies point only inward.** An inner ring knows nothing about any outer ring — it
> names no class, function, variable, or module defined further out.

When an inner ring needs something an outer ring provides (an LLM, a chat store, a mailbox), the inner
ring **defines the contract (a "port")** and the outer ring **implements it (an "adapter")** and
**injects** the implementation inward. Control flows outward at runtime; *source* dependencies never do.

This gives each ring the property we want: it imports and is unit-testable using only itself, the rings
inside it, and stub implementations of its own ports — with **no import of anything outside it**. Both
foundations have this property today; the program of work is to give every ring the same property, from
the inside out.

---

## Vocabulary

| Term | Meaning |
|---|---|
| **Ring** | A layer in the dependency hierarchy. Inner rings are more abstract/stable; outer rings more concrete/volatile. |
| **Port** | A contract (`Protocol`, abstract data type, or `@dataclass`) *owned by* the ring that depends on it. Inner rings own the ports outer rings implement. |
| **Adapter** | A concrete implementation of a port, living in an outer ring and injected inward. |
| **Inward dependency** | An import whose target is in the same ring or an inner one. The only legal direction. |
| **Outward dependency** | An import whose target is in an outer ring — **a violation**, the thing this BEP removes. |
| **Re-export** | An outer module re-exporting an inner symbol (the import still points inward), so ownership can move inward without churning call sites. The migration shim (`bos.protocol`) is built from this. |

A **port** is owned by the *consumer* (inner) ring; an **extension point** (`ep_*` in `core.contract`) is
the *registry/discovery* mechanism for adapters and is an **outer-ring** concern. The agent depends on
the `ToolSet` *port*; it knows nothing about `ep_tool`/`ToolRegistry`, which resolve and register adapters.

---

## The Rings (topology)

Two **independent zero-dependency foundations** at the center — one for the *domain*, one for the
*system* — with everything else built outward, each ring importing only inward via the hierarchy. The
arrow reads "is depended on by":

```
            ┌── bos.core.agent  (DOMAIN foundation) ──┐
            │     Agent · ports · TurnEvent · MessageContent
  center →  │                                          │   two zero-dep peers
            │     base Actor · MailBox · Envelope · EventBus · Event · MessageType
            └── bos.core.actor  (SYSTEM foundation) ──┘
                              ↓   (every outer ring imports the foundations directly, via the hierarchy)
            bos.core:   harness · config · extension (+defaults)        (assembly ring)
                              ↓
            bos.gateway    (AgentActor · channels · CommandHandler · ChatCoordinator)
                              ↓
            bos.cli · bos.runner    (process entrypoints — outermost)

  bos.protocol — MIGRATION SHIM, not a ring. Re-exports the foundations' wire types so legacy
  `from bos.protocol import …` call sites keep working. To be eliminated: internal code imports
  the owning foundation directly. (A shared facade that *everyone* imported would itself become a
  universal innermost dependency — exactly the coupling the two-foundation model removes.)
```

- **`bos.core.agent`** and **`bos.core.actor`** each import the stdlib and themselves *only* — not each
  other, not `bos.protocol`. Both are guard-enforced
  ([test_agent_ring_isolation.py](../../tests/test_agent_ring_isolation.py),
  [test_actor_ring_isolation.py](../../tests/test_actor_ring_isolation.py)). Either could be lifted out
  to build a different application.
- **`bos.protocol` is a transitional shim, not a permanent ring.** It owns no types; it *lazily*
  re-exports `Envelope`/`MessageType` from `actor` and `MessageContent`/`TurnEvent` from `agent` for
  call sites that still say `from bos.protocol import …`. The end state is that those call sites import
  the owning foundation directly and the shim is deleted. In-tree `bos.core` code already imports the
  foundations directly; the shim exists only to retire the remaining historical imports gradually. It is
  deliberately *not* in the dependency spine — nothing should depend on it as a shared inner ring.
- **`AgentActor`** composes *both* foundations (an `Actor` that drives an `Agent`). It is not part of
  either foundation — it lives in the gateway, the ring that actually consumes it (§2.4).
- **Gateway is outer to the harness ring**: it imports `bos.core`/`bos.config` (`AgentHarness`,
  `MailRoute`, contracts); nothing in `bos.core` imports the gateway. `cli`/`runner` are outer still.

---

## Non-Goals

1. **Behavior change.** This is a structural refactor. Moving a ring changes no turn semantics, config
   key, wire protocol, or CLI behavior. Behavior-affecting work rides separate BEPs.
2. **A new framework or DI container.** Injection is plain constructor parameters and protocol defaults.
   No service locator beyond the existing `ep_*` registries.
3. **Keeping `bos.protocol` as a shared facade.** It is a migration shim, not an architectural layer.
   The target has internal code importing foundations directly via the hierarchy.
4. **Reworking the extension/plugin model.** BEP 4 (micro-kernel) and BEP 6 (config) stand. This BEP only
   relocates *where contracts are owned* and *which way imports point*.

---

## 1. The Agent Ring — `bos.core.agent` (the reference shape)

This ring is complete and is the **template** every later ring follows. Anything proposed for an outer
ring is judged against the properties this ring already has.

### 1.1 What it is at runtime

`Agent` ([agent/agent.py](../../src/bos/core/agent/agent.py)) is a plain object, not a process or actor.
One instance drives one logical agent definition; `Agent.ask(chat_id, content, …)` executes a single
**turn**: load + compact history, build the system prompt once, then loop LLM-call → tool-calls until a
final response, persisting the turn to the `ChatStore` at the end. It owns no concurrency, no sessions,
and no mailbox — those belong to the actor ring (§2). It is instantiated by the harness's
`create_agent()` and is also called directly, off any actor, by the CLI and the subagent runtime.

### 1.2 Boundary — what it imports

`bos.core.agent` imports **stdlib only**, plus its own package-internal leaves (`._content`, `._utils`).
It imports nothing from `core.contract`, `core.harness`, `bos.protocol`, `bos.gateway`, or any extension.
**This is firm: the agent core is the absolute innermost ring and depends on *nothing* — not even
`bos.protocol`.** The direction is the reverse — the shim re-exports `TurnEvent`/`MessageContent` *from*
the agent core. This keeps the agent core a standalone library that can be lifted out to build other
agent applications with no actor/mailbox/harness baggage. An automated guard
([test_agent_ring_isolation.py](../../tests/test_agent_ring_isolation.py)) statically asserts every import
under `bos/core/agent/` resolves to stdlib or the package itself, so a regression fails CI.

### 1.3 Ports it owns

Every contract the Agent defines or depends on lives in
[agent/contract.py](../../src/bos/core/agent/contract.py) and is owned by this ring:

- **Capabilities the agent calls outward** (implemented by adapters, injected in): `LLM`, `ChatStore`,
  `Consolidator`, `ToolSet`, `TurnInterceptor`, `PromptProvider`, `TurnEventSink`.
- **Data crossing the boundary**: `TurnContext`, `TurnEvent`, `Message`, `ContextResult`, `ChatCommit`,
  `ChatMeta`, `LLMResponse`, `ToolCallRequest`, `ToolContext`, `ToolAttributes`, `MessageContent`, and the
  event-vocabulary namespaces (`AgentEventType`/`TurnEventPhase`/`TurnEventStage`/`TurnEventDetail`).

These are `Protocol`s and dataclasses — the agent depends on the *shape*, never on a concrete adapter or
on the registry that produces adapters.

### 1.4 How outer rings inject

The Agent's constructor takes every dependency as a parameter, each with a do-nothing inner default so
the ring is usable and testable standalone ([agent/agent.py](../../src/bos/core/agent/agent.py)):

- `llm`, `chat_store`, `consolidator` — required capability adapters.
- `tools: ToolSet | None` — defaults to `_EmptyToolSet`. **Tool resolution** (merge of global + plugin
  tools, include/exclude filtering) is the *harness's* job; it injects the resolved set. The agent never
  sees `ToolRegistry`/`ep_tool`.
- `interceptor: TurnInterceptor | None` — defaults to `_NoopInterceptor`. The harness composes plugin +
  configured interceptors into one and injects it.
- `prompt_provider: PromptProvider | None` — defaults to `_NoopPromptProvider`. The harness builds the
  provider from the agent's plugins; the system prompt is built once per turn.

The agent's `ask()` loop reads as pure orchestration over injected ports; the harness holds all knowledge
of registries, globals, plugins, and config. (`Agent.build_system_prompt(ctx)` is public so the debug CLI
can introspect the prompt without reaching into privates.)

### 1.5 The re-export pattern (moving ownership inward without breaking call sites)

Ownership moves inward without churning call sites via re-export: an outer module re-exports an inner
symbol, so the import keeps resolving while the symbol's *home* moves in. `core.contract` does this for
the agent's contracts (`from bos.core.contract import ChatStore` still resolves to the agent-ring type),
and `bos.protocol` does it for the wire types as a **temporary shim** (lazy `__getattr__`, so importing
it never triggers `bos.core` at module-init time). Re-export is the mechanism every ring uses while
ownership migrates inward; for `bos.protocol` specifically the end state is *deletion* once the remaining
`from bos.protocol import …` call sites move to direct foundation imports.

### 1.6 The rules distilled (the checklist for every ring)

A ring is "done" when:

1. It imports only stdlib and inner rings — **zero outward imports** (guard-verifiable) — and does **not**
   depend on `bos.protocol` (the removable shim); it imports the owning foundation directly. (The two
   foundations import even less — stdlib + their own leaves only.)
2. It **owns** the ports its inner-facing dependencies are expressed as; outer rings implement and inject.
3. Each port has an inner default (noop/empty) so the ring is standalone-testable.
4. It reaches into **no inner ring's privates** — only published API.
5. Symbols whose home moved inward are **re-exported** by the modules that used to own them; public call
   sites are unchanged (and the shim is retired as call sites migrate to direct imports).
6. `pytest`, `ruff`, and `pyright` are green (per CLAUDE.md gate).

---

## 2. The Actor Ring — `bos.core.actor` (the system foundation)

The actor ring is the second zero-dependency foundation: the domain-agnostic runtime for long-lived,
mailbox-bound components. It is a **peer** of the agent ring, not an outer layer — any system of
long-lived actors could build on it without the agent/conversation domain. The agent foundation models
*the domain* (what a turn is); the actor foundation models *the system* (how a long-lived component
receives messages, runs, and emits events).

### 2.1 Package layout

```
core/actor/__init__.py      # public surface: Actor, MailBox, EventBus, Event, Envelope, MessageType
core/actor/base.py          # Actor — the domain-agnostic runtime (pump, lifecycle, emit)
core/actor/mailbox.py       # MailBox[ContentT = Any]  — point-to-point messaging endpoint
core/actor/envelope.py      # Envelope[ContentT = Any] — a message in transit
core/actor/message_types.py # MessageType
core/actor/event_bus.py     # Event (marker) + EventBus (type-keyed pub/sub)
```

### 2.2 Boundary — zero outward imports (guard-enforced)

`bos.core.actor` imports the **stdlib and itself only** — not `bos.protocol`, not `bos.core.agent`, not
the harness ring. The dependency points the other way: the shim re-exports `Envelope`/`MessageType`
*from* this foundation. Enforced by
[test_actor_ring_isolation.py](../../tests/test_actor_ring_isolation.py), which resolves relative imports
so an escaping `from ..contract import` fails CI, mirroring the agent guard (§1.2).

### 2.3 What it owns — generic, domain-agnostic primitives

The foundation must never name the agent's `MessageContent`, so the messaging primitives are **generic**
(PEP 696 defaults, so bare `Envelope`/`MailBox` still mean `[Any]` — the harness ring annotates
`[MessageContent]` for precision, with zero call-site churn):

- **`Envelope[ContentT = Any]`** — a message in transit. Carries the generic "non-`MESSAGE` payload is a
  `str`" invariant; agent-specific content validation belongs to the agent core as it processes a turn.
- **`MailBox[ContentT = Any]`** — a point-to-point endpoint. `MailRoute.bind()` (harness ring) yields a
  bare `MailBox`; `AgentActor` annotates `MailBox[MessageContent]`.
- **`Event`** (a ring-neutral marker) + **`EventBus`** — a **type-keyed** pub/sub bus:
  `subscribe(event_type, handler)` + `emit(event)`, dispatching over the event's MRO. Domain-agnostic, so
  no domain vocabulary lives here — `SessionEvent` (§2.6) subclasses `Event` in the harness ring.
- **`Actor`** ([base.py](../../src/bos/core/actor/base.py)) — the runtime: the `run()` pump (idle-tick
  hook → poll `receive_nowait` → `handle(env)`), `_spawn`/`aclose` task lifecycle, and `emit(Event)`. It
  knows nothing about turns, sessions, or interrupts; those are a specialization's concern (§2.4).

The harness ring imports all of these inward and **re-exports** them, so `from bos.core import …` /
`from bos.core.contract import MailBox` call sites are unchanged. `MailRoute`/`Channel`/`ep_mail_route`
stay in the harness ring and reference the foundation's `MailBox` inward (a legal dependency): the
foundation is the *home* of these primitives in the two-foundation model.

### 2.4 `AgentActor` — the composition (in the gateway)

`AgentActor` is an `Actor` that drives an `Agent` — it composes both foundations and therefore belongs to
neither. It lives in the **gateway** ([gateway/actors/agent_actor.py](../../src/bos/gateway/actors/agent_actor.py)),
the only ring that consumes it. It is a **pure data-plane actor**: pump → `MESSAGE`/`INTERRUPT` → run a
turn via `Agent.ask()` → emit `SessionEvent`s, plus `retire_session`. Coordinator fencing (multi-actor
turn coordination) is folded in via an optional `chat_coordinator` dependency — the role the former
standalone `CoordinatedActor` played, now collapsed into `AgentActor`.

### 2.5 Data plane vs. control plane

The actor is the **data plane** (messages and turns). Slash-commands (`/new`, `/resume`, `/chats`)
manipulate *client cursors* — a session/transport concern, not an actor one — so they are the **control
plane** and live outside the actor entirely. A mailbox-free gateway `CommandHandler`
([gateway/core/command_handler.py](../../src/bos/gateway/core/command_handler.py)) runs them against the
`ChatCoordinator` (the single cursor authority) and `ActorManager.retire_session`. Each channel
(ws/telegram/lark) detects a leading `/` on inbound and calls the handler directly — commands never
become envelopes and never reach the actor. This keeps cursor state single-owned (the `ChatCoordinator`)
and the actor free of session bookkeeping.

### 2.6 The event model — two planes, two contracts

There are **two notification planes with deliberately different contracts**. They are not two instances
of one abstraction and must not be merged.

**Plane 1 — the turn stream (`TurnEventSink`, agent ring).** An **emit-only callback port**
(`emit(TurnEvent)`) named for its payload (`TurnEvent`) and granularity (intra-turn). It is *not* a bus:
the Agent is a pure emitter and never subscribes. Fan-out (`HostChannelSink`) and the mailbox forwarder
are concrete *consumers* wired by the actor, not part of the port. The contract is inner-ring, client-
facing, and ordered with the turn.

**Plane 2 — platform events (`EventBus` + `Event`/`SessionEvent`).** Best-effort, broadcast,
fire-and-forget notifications to anonymous background subscribers.

- Event **categories are Python types** rooted at the ring-neutral `Event` marker; the per-category
  discriminator is a typed `Literal` `kind`. `SessionEvent(Event)` carries `chat_id` and
  `kind: SessionEventKind = Literal["turn_complete", "session_close"]`. Subscribers subscribe **by
  concrete type** and discriminate on `.kind`.

  ```python
  @dataclass(frozen=True)
  class Event: ...                       # ring-neutral marker; depends on nothing

  @dataclass(frozen=True)
  class SessionEvent(Event):
      kind: SessionEventKind
      chat_id: str

  class EventBus(Protocol):
      def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None: ...
      async def emit(self, event: Event) -> None: ...
  ```

- **One bus, one mechanism.** A single `EventBus` instance is created and owned by the harness and
  injected into actors. A new event category is a new `Event` subclass + an `emit` call — **never a new
  bus type** — because `SessionEvent`, a future `ActorEvent`, etc. all share one delivery contract.
  *Same delivery contract → one type; different contract → different type* — which is also why
  `TurnEventSink` stays a separate port (its contract differs).
- **`session_close` is a precondition, not a guarantee.** It is emitted **only on explicit client
  retirement** (`reset_chat`, or `/new`/`/resume` switching away), via the single `retire_session` emit
  site. A merely *abandoned* session (client walks away, idle timeout, shutdown) emits **no**
  `session_close`; background work for those relies on the `idle` job trigger instead. Do not design
  subscribers that assume universal end-of-life delivery.

**Messages vs. events (the routing rule).** The actor produces two categorically different outputs:

| | **Message** (the reply) | **Event** |
|---|---|---|
| Addressing | point-to-point to `reply_recipient` | broadcast, anonymous |
| Reliability | must arrive | may have zero subscribers; may drop |
| Carrier | `MailBox` | `EventBus` |

The reply is the `Agent.ask()` return value, sent via `mailbox.send(...)` **before** `turn_complete` is
emitted — it is **never** routed through the bus (correlation, reliability, and ordering all forbid it).
Membership rule: *best-effort broadcast notification → `EventBus`; anything needing a delivery guarantee,
ordering, or persistence is a message (mailbox) or a job (runner), not an event.* Ownership: `EventBus` +
base `Event` are a ring-neutral *mechanism* in the foundation; per-category *vocabularies* are owned by
the ring that emits them (`SessionEvent` with the session/actor layer).

---

## 3. Outer Rings — `harness` · `config` · `extension`, then `gateway` · `cli` · `runner`

Reserved. These adopt the same approach as each is formally extracted: identify outward imports and
inner-privacy reaches, move owned ports inward, re-export for compatibility, retire the `bos.protocol`
shim as call sites switch to direct foundation imports, give each port an inner default, and verify zero
outward imports with a guard. The gateway already hosts `AgentActor` and the control plane (§2.4–§2.5),
with its internals organized into `core/` (coordinator, resolver, command handler, channel context),
`actors/`, and `channels/` subpackages — but its ring boundary has not yet been audited to the §1.6
checklist. Each ring gets its own numbered section when reached, so decisions made during extraction are
captured here rather than guessed up front.

---

## Revision History

| Date | Change |
|---|---|
| 2026-06-22 | Establish the dependency-ring model; document the completed `agent` ring as the reference shape (§1). |
| 2026-06-22 | Settle the event model (§2.6): `EventSink` → `TurnEventSink` (emit-only callback, not a bus); generalize the lifecycle bus into a single type-keyed `EventBus` over an `Event`/`SessionEvent` hierarchy; codify the messages-vs-events routing rule. |
| 2026-06-22 | **Two-foundation pivot.** Establish `actor` as a *system foundation* — a zero-dep peer of `agent`. Recast `bos.protocol` as a removable migration shim (not a ring). The foundations own the messaging (`Envelope`/`MailBox`/`MessageType`) and event (`Event`/`EventBus`) primitives; the bus becomes type-keyed. Strict isolation guards added for both foundations. |
| 2026-06-23 | Seat `AgentActor` in the gateway (its only consumer), folding in `CoordinatedActor`; organize gateway internals into `core/`/`actors/`/`channels/`. |
| 2026-06-23 | Extract the slash-command **control plane** out of the actor into a mailbox-free gateway `CommandHandler` over the `ChatCoordinator` (§2.5); `AgentActor` is now pure data plane. Remove the last agent-private reach by making `Agent.build_system_prompt` public. |
| 2026-06-23 | Consolidate this BEP into a design doc: correct the topology (gateway is outer to the harness ring; `bos.protocol` is a removable shim, not a layer), present the actor ring as finished design, and remove implementation footage. |
