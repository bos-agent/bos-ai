# BEP 13: Clean Architecture — Concentric Dependency Rings

Status: **living design** — graduated **ring by ring**, because the right boundary for each ring only
becomes clear while extracting it. The two innermost rings are done and documented here as the reference
shape; the outer rings adopt the same rules when they are extracted.

| Ring | Status |
|---|---|
| `bos.core.agent` — **domain foundation** | ✅ done — the reference shape (§1) |
| `bos.core.actor` — **system foundation** | ✅ done — a zero-dep peer of `agent` (§2) |
| `bos.protocol` — **migration shim** | ✅ **retired** — deleted; every call site imports the owning foundation directly. A guard ([test_no_protocol_shim.py](../../tests/test_no_protocol_shim.py)) fails CI if it returns (see topology) |
| `bos.core` (harness · contract · registry · `defaults`) — assembly ring | ✅ done — zero outward imports, guard-enforced (§3.1) |
| `bos.gateway` — gateway ring (owns its config shapes) | ✅ done — imports only inward, guard-enforced (§3.3) |
| `bos.config` — config *loader* (outer to gateway; produces both rings' shapes) | ✅ done — imports only inward, guard-enforced (§3.2) |
| `cli` · `runner` — process entrypoints (outermost) | ⬜ not yet formally extracted (§3.4) |

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
            bos.core:   harness · contract · registry · sinks · llm · defaults     (assembly ring)
                              ↓
            bos.gateway    (AgentActor · channels · CommandHandler · ChatCoordinator;
                            owns its config shapes: GatewayRuntimeConfig + Resolved*)
                              ↓
            bos.config     (the config *loader*: reads storage → produces the inner
                            rings' config shapes; builds AgentHarness. imports core + gateway)
                              ↓
            bos.cli · bos.runner    (process entrypoints / composition roots — outermost)

  bos.extensions — adapters (chat stores · mailboxes · providers · channels), injected inward via ep_*.
  bos.exts — composition root: importing it registers all built-in adapters/plugins. Both are outer to
  bos.core (they import it; it never imports them).

  bos.protocol — RETIRED. Was a migration shim re-exporting the foundations' wire types for legacy
  `from bos.protocol import …` call sites; now deleted, with every call site importing the owning
  foundation directly. (A shared facade that *everyone* imported would itself become a universal
  innermost dependency — exactly the coupling the two-foundation model removes.) Its non-wire surface
  moved to real homes: the content helpers to the agent ring (§1.3), the WS-takeover constants to the
  gateway (a WS-transport concern).
```

- **`bos.core.agent`** and **`bos.core.actor`** each import the stdlib and themselves *only* — not each
  other, not `bos.protocol`. Both are guard-enforced
  ([test_agent_ring_isolation.py](../../tests/test_agent_ring_isolation.py),
  [test_actor_ring_isolation.py](../../tests/test_actor_ring_isolation.py)). Either could be lifted out
  to build a different application.
- **`bos.protocol` was a transitional shim and is now deleted.** It owned no types — it re-exported
  `Envelope`/`MessageType` from `actor` and `MessageContent`/`TurnEvent` from `agent` for legacy
  `from bos.protocol import …` call sites. Every call site now imports the owning foundation directly,
  so the package is gone; [test_no_protocol_shim.py](../../tests/test_no_protocol_shim.py) fails CI if it
  (or an import of it) reappears. It was deliberately *never* in the dependency spine — nothing depended
  on it as a shared inner ring, which is exactly why it could be removed without restructuring.
- **`AgentActor`** composes *both* foundations (an `Actor` that drives an `Agent`). It is not part of
  either foundation — it lives in the gateway, the ring that actually consumes it (§2.4).
- **Gateway is outer to the assembly ring, but inner to `bos.config`.** It imports `bos.core`
  (`AgentHarness`, `MailRoute`, contracts) and **owns the shape of its own configuration**
  (`GatewayRuntimeConfig` + the `Resolved*` value objects). It does **not** import `bos.config`: the
  config *loader* is an outer ring that depends inward on the gateway's shapes to produce them, and the
  composition root injects a `GatewayRuntimeConfig` via `Gateway(runtime=…)` (§3.3). `config`, then
  `cli`/`runner`, are outer still.

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
It imports nothing from `core.contract`, `core.harness`, `bos.gateway`, or any extension.
**This is firm: the agent core is the absolute innermost ring and depends on *nothing*.** The direction
is the reverse — outer rings import `TurnEvent`/`MessageContent` *from* the agent core. This keeps the
agent core a standalone library that can be lifted out to build other agent applications with no
actor/mailbox/harness baggage. An automated guard
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
- **Content helpers over `MessageContent`** (in the `._content` leaf): `validate_message_content`,
  `content_as_parts`, and the rendering/encoding helpers `content_to_plain_text`, `content_preview`,
  `content_length`, `image_source_to_model_url`. These operate on the agent's content shape, so they are
  owned here and imported inward by chat stores (previews) and providers (image encoding) — the home the
  retired shim's `content.py` collapsed into.

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
the agent's contracts (`from bos.core.contract import ChatStore` still resolves to the agent-ring type).
`bos.protocol` was a second use of this mechanism — a **temporary shim** re-exporting the wire types —
and has now served its purpose and been deleted: every call site imports the owning foundation directly.
Re-export remains the mechanism every ring uses while ownership migrates inward; the shim's deletion is
the worked example of its intended end state (re-export only to bridge a migration, then retire it).

### 1.6 The rules distilled (the checklist for every ring)

A ring is "done" when:

1. It imports only stdlib and inner rings — **zero outward imports** (guard-verifiable) — importing the
   owning foundation directly (the `bos.protocol` shim is gone, so there is no longer any indirection to
   route through). (The two foundations import even less — stdlib + their own leaves only.)
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

`bos.core.actor` imports the **stdlib and itself only** — not `bos.core.agent`, not the harness ring.
The dependency points the other way: outer rings import `Envelope`/`MessageType` *from* this foundation.
Enforced by
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

## 3. Outer Rings — the assembly ring, then `gateway` · `config` · `cli` · `runner`

These adopt the same approach as each is formally extracted: identify outward imports and inner-privacy
reaches, move owned ports inward, re-export for compatibility, give each port an inner default, and verify
zero outward imports with a guard. (The `bos.protocol` shim is already retired, so new ring work imports
the owning foundation directly.) Each ring gets its own numbered subsection when reached, so decisions
made during extraction are captured here rather than guessed up front.

### 3.1 The assembly ring — `bos.core` (harness · contract · registry · sinks · llm · `defaults`)

The assembly ring wires the two foundations into a runnable agent: the `AgentHarness` that builds an
`Agent` from config, the `contract` that owns the outer-facing ports, the `ep_*` registry, and the
shipped `_default` adapters. Unlike the foundations it is **not** third-party-free (it imports `litellm`,
`jsonschema`, …); what makes it a ring is that every source dependency points **inward**.

**Boundary (what's in, what's out).** The ring is everything under `bos/core/` *except* the two
foundation subpackages (`core/agent`, `core/actor`), which keep their own stricter zero-dep guards. Three
neighbours are deliberately **outside** it:

- **`bos.extensions` — outer (adapters).** The built-in adapter implementations (chat stores, mailboxes,
  providers, channels). They import `bos.core` inward and register via `@ep_*`; nothing in `bos.core`
  imports them. They are injected inward at runtime, never named at import time.
- **`bos.config` — outer (a consumer of the harness).** `bos.config` imports `bos.core` (`AgentHarness`,
  `ep_agent`, `ExtensionPoint`, contract types); `bos.core` imports **nothing** from `bos.config`. By the
  dependency rule it is therefore a ring *outward* of the assembly ring, not part of it — a correction to
  the earlier "harness · config · extension" grouping, which the import direction does not support.
- **`bos.exts` — outer (the composition root).** A single module whose import triggers registration of the
  built-in *extensions* (`import bos.extensions.channels.*`, etc.). It sits at the very outside, wiring
  optional adapters into the registry the inner rings expose. (It no longer registers the core `_default`
  adapters — the assembly ring does that itself; see below.)

**`defaults` is *in* the ring (verified), and the ring registers its own defaults.** `core/defaults` ships
the ring's default adapters, and the harness has a genuine compile-time dependency on them, so it is part
of the ring, not a separate adapter layer. The harness reaches them two ways, and in both the assembly ring
is self-contained — it does **not** rely on an outer composition root to register its own defaults:

- **Registry-resolved** (`@ep_*(name="_default")`): `jsonl_chat_store`, `litellm_provider`, `consolidator`,
  `jobs`, `jsonl_mailbox`. The harness resolves these *by name* through the `ep_*` registry. It guarantees
  their registration by importing `bos.core.defaults` at the top of `AgentHarness.__aenter__` (idempotent,
  deferred to open-time to avoid import-order coupling during package init) — so opening a harness always
  has its defaults, with no dependency on `bos.exts`.
- **Imported directly** by the harness as concrete fallback wiring: `DefaultEventBus` and
  `DefaultBackgroundLLM` ([harness.py](../../src/bos/core/harness.py)). Both are intra-ring imports, so
  neither breaks ring purity — they are the ring's inner defaults for the `EventBus` / background-LLM ports.

**Ports it owns (rule 2).** `contract.py` owns the outer-facing ports the adapter ring implements —
`MailRoute`, `Channel`/`BaseChannel`, `AgentPlugin`, the `ep_*` extension points — and re-exports the
foundations' contracts inward so `from bos.core import ChatStore` / `MailBox` keep resolving.

**Foundation access is package-API-only (rule 4).** The assembly ring imports the foundations through
their package surface (`bos.core.agent` / `bos.core.actor`), never a private submodule. The one prior
exception — `core/_utils` importing `from .agent._utils import …` — was resolved by **publishing** those
shared `_`-prefixed helpers from `bos.core.agent`'s `__init__` (consistent with the convention that
`_`-prefixed helpers are an exported-but-unstable surface) and re-pointing the re-export to the package.

**Guard.** [test_assembly_ring_isolation.py](../../tests/test_assembly_ring_isolation.py) AST-scans every
assembly-ring file (excluding the foundation subpackages, resolving relative imports to absolute) and
asserts two things: (1) **no outward import** to `bos.gateway`/`bos.cli`/`bos.runner`/`bos.extensions`/
`bos.config`/`bos.exts`; (2) **no foundation-private reach** (`bos.core.agent.<sub>` /
`bos.core.actor.<sub>`). It allows stdlib + third-party + `bos.core.*` — the ring is defined by import
*direction*, not by a zero-third-party rule.

### 3.2 The gateway ring — `bos.gateway`

`bos.gateway` is the actor/channel runtime that drives the agent (`AgentActor`, `ChannelManager`,
`ChatCoordinator`, the `CommandHandler` control plane, §2.4–§2.5), organized into `core/`/`actors/`/
`channels/`. It is *policy*, so it owns the **shape of the configuration it consumes** and depends on no
configuration machinery.

**The config-shape inversion (the key decision).** Configuration loading is a *detail* (I/O from TOML/env);
the gateway is *policy*. By the dependency rule, policy must not depend on the detail's types — so the
gateway **owns** its config shapes and the loader depends inward on them:

- `gateway/config.py` defines `ResolvedGatewayConfig`, `ResolvedActorConfig`, `ResolvedGatewayChannelConfig`,
  and the aggregate **`GatewayRuntimeConfig`** — frozen value objects, stdlib-only.
- `bos.config` (the loader, §3.3) imports these and *produces* them (`config → gateway`).
- The composition root builds a `GatewayRuntimeConfig` from a `Workspace` and injects it:
  `Gateway(runtime=…)`. The gateway no longer takes a `Workspace`, and `ActorManager` takes a
  `dict[str, ResolvedActorConfig]` — so `bos.gateway` has **zero** `bos.config` imports.
  (This mirrors what `bos.core` already did: it owns `ReasoningEffort`/`ToolNoiseFilter` and the loader
  fills them. The gateway was the lone inverted case; this aligns it.)

**Boundary (guard-enforced).** (1) No import to `bos.config` (the loader), `bos.runner`/`bos.cli`
(entrypoints), `bos.extensions` (adapters, injected via `ep_*`), or `bos.exts`. (2) No underscore-private
`bos.core` reach (rule 4). `WS_TAKEOVER_*` already live here (Track A).
**Guard:** [test_gateway_ring_isolation.py](../../tests/test_gateway_ring_isolation.py).

### 3.3 The config loader — `bos.config`

`bos.config` (workspace loading, the TOML `schema`, the `default_agent_spec`) is the **configuration
loader**: it reads storage and *produces* the typed config the inner rings consume. It imports `bos.core`
(`AgentHarness`, `ep_agent`, contract types like `ReasoningEffort`/`ToolNoiseFilter`, published helpers)
**and `bos.gateway`** (the gateway-owned shapes it fills, §3.2); neither imports it back. It is therefore a
ring *outward* of both — just inside the process entrypoints — not, as an earlier draft of this BEP placed
it, *inner* to the gateway. It owns no ports; it reads/validates config, builds an `AgentHarness`, and hands
a `GatewayRuntimeConfig` outward.

**Boundary (guard-enforced).**

- **No outward imports.** Nothing under `bos/config/` imports `bos.cli`/`bos.runner`/`bos.extensions`/
  `bos.exts`. Config *names* `"bos.exts"` and `"./extensions"` as **string defaults** for the extension
  loader — data it resolves at runtime, not import-time dependencies. Importing `bos.core` and `bos.gateway`
  is the legal inward direction. (To keep `import bos.config` light — the gateway package pulls aiohttp —
  the gateway shapes are imported *lazily inside* the `resolve_*` methods, with a `TYPE_CHECKING` import for
  annotations; the AST guard still records the `config → gateway` edge.)
- **Published-API only (rule 4).** It imports `bos.core`/`bos.gateway` through their public surface, never an
  underscore-prefixed private module. The one prior reach — `workspace.py` importing
  `from bos.core._utils import _deep_merge, _get_bos_home, _resolve_path` — was resolved by publishing
  `_resolve_path` from `bos.core`'s `__init__` and re-pointing to `from bos.core import …`.

**Guard.** [test_config_ring_isolation.py](../../tests/test_config_ring_isolation.py): no import to
`cli`/`runner`/`extensions`/`exts`; no underscore-private reach into `bos.core`/`bos.gateway`.

### 3.4 `cli` · `runner`

Reserved. The outermost process entrypoints and composition roots: they load a `Workspace` (via
`bos.config`), open the harness, build a `GatewayRuntimeConfig`, and inject it into the gateway. Their ring
boundary (importing inward only; no `cli ↔ runner` cycle) has not yet been audited to the §1.6 checklist.
Each gets its own subsection when reached.

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
| 2026-06-23 | **Track A complete — `bos.protocol` shim retired and deleted.** Re-pointed every wire-type call site (`Envelope`/`MessageType` → `bos.core.actor`; `MessageContent`/`MessageContentPart`/`TurnEvent` → `bos.core.agent`) across src + tests; moved the shim's `content.py` helpers into the agent ring's `._content` leaf (§1.3) and the `WS_TAKEOVER_*` constants into the gateway's WS channel (re-exported from `bos.gateway`); added [test_no_protocol_shim.py](../../tests/test_no_protocol_shim.py) to fail CI on reintroduction. |
| 2026-06-23 | **Track B / B1 — assembly ring (`bos.core`) extracted (§3.1).** Confirmed zero outward imports and added [test_assembly_ring_isolation.py](../../tests/test_assembly_ring_isolation.py) (forbids imports to `gateway`/`cli`/`runner`/`extensions`/`config`/`exts` and foundation-private reaches). Settled the boundary: `defaults` is *in* the ring; `extensions` (adapters) and `exts` (composition root) are out; **corrected the topology** — `bos.config` is a *consumer* of the harness (it imports `bos.core`, not the reverse), so it is a ring *outward* of the assembly ring, not part of it. Resolved the one rule-4 reach by publishing the shared `_`-helpers from `bos.core.agent`'s `__init__`. |
| 2026-06-23 | **Track B / B2a — config ring (`bos.config`) extracted (§3.2).** Added [test_config_ring_isolation.py](../../tests/test_config_ring_isolation.py) (no imports to `gateway`/`cli`/`runner`/`extensions`/`exts`; no underscore-private `bos.core` reach). Resolved its one rule-4 reach by publishing `_resolve_path` from `bos.core` and re-pointing `workspace.py` off `bos.core._utils`. Also tidied two non-ring items en route: renamed `core/events.py`→`core/sinks.py` and `core/defaults/lifecycle.py`→`core/defaults/eventbus.py`, dropped the `Lifecycle*` back-compat aliases, and removed the `bos.core.llm` re-export of `LLMResponse`/`ToolCallRequest` (consumers now import the agent ring directly). |
| 2026-06-23 | **Track B / B2b — gateway ring (`bos.gateway`) extracted, with the config-shape inversion (§3.2/§3.3).** The gateway now **owns** its config shapes (`gateway/config.py`: `Resolved*` + `GatewayRuntimeConfig`); `bos.config` imports and *produces* them (`config → gateway`), reversing the former `gateway → config` edge. `Gateway(runtime=…)` and `ActorManager(actors=…)` replace the `Workspace` parameter; the composition root injects via `workspace.resolve_gateway_runtime()`. Added [test_gateway_ring_isolation.py](../../tests/test_gateway_ring_isolation.py) and updated the config guard (config→gateway now legal; private-reach check covers both inner rings). **Reordered the topology:** `core < gateway < config < cli/runner` — config is the *loader* (a detail), outer to the gateway it configures, correcting B2a's placement. |
