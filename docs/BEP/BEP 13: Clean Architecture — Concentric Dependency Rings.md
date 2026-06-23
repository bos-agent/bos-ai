# BEP 13: Clean Architecture — Concentric Dependency Rings

Status: **living design** — authored on the `re-arch` branch and updated *along with* implementation,
not finalized up front. This is deliberate: the right boundary for each ring only becomes clear while
extracting it, so this BEP is graduated **ring by ring**. Per-ring readiness:

| Ring | Status |
|---|---|
| `agent` (innermost — **domain foundation**) | ✅ **done** — landed on `re-arch`; the reference shape (§1) |
| `actor` (innermost — **system foundation**) | ✅ **done** — landed on `re-arch` as a zero-dep peer of `agent` (§2) |
| `harness`, `config`, `extension (+defaults)` | ⬜ not yet authored — same approach, added when we get there |
| `gateway`, `cli`, … (outermost) | ⬜ not yet authored |

**Two foundations, not one chain.** `agent` (domain) and `actor` (system) are *independent* innermost
rings — each depends on nothing (not even `bos.protocol`). Everything else is built on top of them.
`AgentActor` is a *specialization* that composes both and lives in the harness ring (§2).

Motivation: BOS grew outward from a single agent loop into a harness, plugin system, gateway, channels,
and CLI. Dependencies accreted in both directions — inner code reaching out to outer modules, outer
modules reaching into inner privates. We are applying Robert C. Martin's **Clean Architecture** to make
the dependency graph a set of concentric rings with one rule, and to extract each ring so it is testable
and replaceable in isolation.

---

## Core Insight — The Dependency Rule

> **Source code dependencies point only inward.** An inner ring knows nothing about any outer ring. It
> names no class, function, variable, or module defined in an outer ring.

When an inner ring needs something an outer ring provides (an LLM, a chat store, a mailbox, a lifecycle
bus), the inner ring **defines the contract (a "port")** and the outer ring **implements it (an
"adapter")** and **injects** the implementation inward. Control flows outward at runtime; *source
dependencies* never do.

This gives each ring the property we want: it compiles, imports, and is unit-testable using only itself,
the rings inside it, and stub implementations of its own ports — with **no import of anything outside
it**. The `agent` ring already has this property (§1); the program of work is to give every ring the
same property, from the inside out.

---

## Vocabulary

| Term | Meaning |
|---|---|
| **Ring** | A layer in the dependency hierarchy. Inner rings are more abstract/stable; outer rings are more concrete/volatile. |
| **Port** | A contract (a `Protocol`, abstract data type, or `@dataclass`) *owned by* the ring that depends on it. Inner rings own the ports outer rings implement. |
| **Adapter** | A concrete implementation of a port, living in an outer ring and injected inward. |
| **Inward dependency** | A `from`/`import` whose target lives in the same ring or an inner one. The only legal direction. |
| **Outward dependency** | A `from`/`import` whose target lives in an outer ring. **A violation** — these are what this BEP removes, ring by ring. |
| **Re-export** | An outer ring may re-export an inner ring's symbol for back-compat (the import still points inward). Used to keep call sites stable while ownership moves inward. |

A note on look-alikes: a **port** is owned by the *consumer* (inner) ring; an **extension point**
(`ep_*` in `core.contract`) is the *registry/discovery* mechanism for adapters and is an **outer-ring**
concern. They are not the same thing. The agent depends on the `ToolSet` *port*; it knows nothing about
`ep_tool`/`ToolRegistry`, which resolve and register adapters (see §1.4).

---

## The Rings (target topology)

Two **independent zero-dependency foundations** at the center — one for the *domain*, one for the
*system* — with everything else built on top (the arrow reads "is depended on by"):

```
bos.core.agent   (DOMAIN foundation — Agent, ports, TurnEvent, MessageContent)
                   \
                    \                 bos.core.actor   (SYSTEM foundation — base Actor, MailBox,
                     \               /                   EventBus, Event, Envelope, MessageType)
                      \             /
                       bos.protocol   (downstream FACADE — owns nothing; re-exports both)
                              ↓
   AgentActor (composition) · harness · config · extension(+defaults)   →   gateway · cli · runner · …
```

- **`bos.core.agent`** and **`bos.core.actor`** each import the stdlib and themselves *only* — not each
  other, not `bos.protocol`. Both are guard-enforced
  ([test_agent_ring_isolation.py](../../tests/test_agent_ring_isolation.py),
  [test_actor_ring_isolation.py](../../tests/test_actor_ring_isolation.py)). Either could be lifted out
  to build a different application.
- **`bos.protocol`** is a downstream **facade** that owns no types: it *lazily* re-exports
  `Envelope`/`MessageType` from `actor` and `MessageContent`/`TurnEvent` from `agent`
  ([protocol/__init__.py](../../src/bos/protocol/__init__.py)), so importing it never triggers
  `bos.core` at module-init time. In-tree `bos.core` code imports these from their owning foundation
  directly; the lazy re-export serves outer rings and back-compat call sites.
- **`AgentActor`** composes the two foundations (an `Actor` that drives an `Agent`) and lives in the
  harness ring, not in either foundation (§2).

---

## Non-Goals

1. **Behavior change.** This is a structural refactor. No turn semantics, config keys, wire protocol, or
   CLI behavior changes as a consequence of moving a ring. Behavior-affecting work rides separate BEPs.
2. **A new framework or DI container.** Injection is plain constructor parameters and protocol
   defaults, as the agent ring already does. No service locator beyond the existing `ep_*` registries.
3. **Renaming the public API.** `from bos.core import X` stays stable. Ownership moves inward; outer
   rings re-export to preserve call sites. Breaking changes, where unavoidable, are named explicitly.
4. **Reworking the extension/plugin model.** BEP 4 (micro-kernel) and BEP 6 (config) stand. This BEP
   only relocates *where contracts are owned* and *which way imports point*.

---

## 1. The Agent Ring — `bos.core.agent` (DONE; the reference shape)

This ring is complete and is the **template** every later ring follows. It documents "the shape we
want." Anything proposed for an outer ring is judged against the properties this ring already has.

### 1.1 What it is at runtime

`Agent` ([agent/agent.py](../../src/bos/core/agent/agent.py)) is a plain object, not a process or actor.
One instance drives one logical agent definition; `Agent.ask(chat_id, content, …)` executes a single
**turn**: load+compact history, build the system prompt once, then loop LLM-call → tool-calls until a
final response, persisting the turn to the `ChatStore` at the end. It is synchronous-to-its-caller
(an `async` coroutine) and owns no concurrency, no sessions, and no mailbox — those belong to the actor
ring (§2). It is instantiated by the harness's `create_agent()` and is also called directly, off any
actor, by the CLI and by the subagent runtime.

### 1.2 Boundary — what it imports

`bos.core.agent` is a package that **imports stdlib only**, plus its own package-internal leaves
(`._content`, `._utils`). It imports nothing from `core.contract`, `core.harness`, `bos.protocol`,
`bos.gateway`, or any extension. **This is a firm invariant: the agent core is the absolute innermost
ring and depends on *nothing* — not even `bos.protocol`.** The direction is the reverse: `bos.protocol`
re-exports `TurnEvent`/`MessageContent` *from* the agent core ([turn_events.py](../../src/bos/protocol/turn_events.py),
[protocol/__init__.py](../../src/bos/protocol/__init__.py)). This keeps the agent core a standalone
library that can be lifted out to build other agent applications with no actor/mailbox/harness baggage.

Enforced two ways: by the module docstring (*"This package imports stdlib only …"*) and by an automated
guard — [tests/test_agent_ring_isolation.py](../../tests/test_agent_ring_isolation.py) statically asserts
every import under `bos/core/agent/` resolves to stdlib or the package itself, so a regression fails CI.

### 1.3 Ports it owns

Every contract the Agent defines or depends on lives in
[agent/contract.py](../../src/bos/core/agent/contract.py) and is owned by this ring:

- **Capabilities the agent calls outward** (implemented by adapters, injected in): `LLM`, `ChatStore`,
  `Consolidator`, `ToolSet`, `TurnInterceptor`, `PromptProvider`, `EventSink` (being renamed →
  `TurnEventSink`; see §2.10).
- **Data the agent passes across the boundary**: `TurnContext`, `TurnEvent`, `Message`,
  `ContextResult`, `ChatCommit`, `ChatMeta`, `LLMResponse`, `ToolCallRequest`, `ToolContext`,
  `ToolAttributes`, `MessageContent`, and the event-vocabulary namespaces
  (`AgentEventType`/`TurnEventPhase`/`TurnEventStage`/`TurnEventDetail`).

Crucially these are `Protocol`s and dataclasses — the agent depends on the *shape*, never on a concrete
adapter or on the registry that produces adapters.

### 1.4 How outer rings inject

The Agent's constructor takes every dependency as a parameter, each with a do-nothing inner default so
the ring is usable and testable standalone ([agent/agent.py](../../src/bos/core/agent/agent.py#L189)):

- `llm: LLM`, `chat_store: ChatStore`, `consolidator: Consolidator` — required capability adapters.
- `tools: ToolSet | None` — defaults to `_EmptyToolSet`. **Tool resolution** (merge of global + plugin
  tools, include/exclude filtering) is the *harness's* job: the harness builds a `ResolvedToolSet` and
  injects it ([harness.py](../../src/bos/core/harness.py#L89), commit `73aa773`). The agent never sees
  `ToolRegistry`/`ep_tool`.
- `interceptor: TurnInterceptor | None` — defaults to `_NoopInterceptor`. The harness composes plugin +
  configured interceptors into one and injects it (commit `a5f4eda`).
- `prompt_provider: PromptProvider | None` — defaults to `_NoopPromptProvider`. The harness builds a
  `_PluginPromptProvider` from the agent's plugins and injects it; the system prompt is built **once per
  turn** (commit `9688a82`).

The result: the agent's `ask()` loop reads as pure orchestration over injected ports. The harness holds
all knowledge of registries, globals, plugins, and config.

### 1.5 The leaf re-export pattern (no import cycle)

`bos.protocol` must stay importable without dragging in `bos.core`. It owns `Envelope`/`MessageType`
outright and **lazily** re-exports `MessageContent`/`TurnEvent`/`MessageContentPart` from
`bos.core.agent` via module `__getattr__`, so the inner symbols are owned by the agent ring while the
historical `from bos.protocol import TurnEvent` keeps working
([protocol/__init__.py](../../src/bos/protocol/__init__.py),
[protocol/turn_events.py](../../src/bos/protocol/turn_events.py)). `core.contract` does the analogous
thing for the rest of the agent's contracts ([contract.py](../../src/bos/core/contract.py#L17-L45)):
it re-exports them inward so `from bos.core.contract import ChatStore` still resolves. **This is the
mechanism every later ring uses to move ownership inward without breaking call sites.**

### 1.6 The rules distilled (the checklist for every later ring)

A ring is "done" when:

1. It imports only stdlib, inner rings, and `bos.protocol` — **zero outward imports** (grep-verifiable).
   (Exception: the agent core itself imports *neither* — stdlib + its own leaves only, since
   `bos.protocol` depends on it, not the reverse; see §1.2.)
2. It **owns** the ports its inner-facing dependencies are expressed as; outer rings implement and inject.
3. Each port has an inner default (noop/empty) so the ring is standalone-testable.
4. It reaches into **no inner ring's privates** — only published API.
5. Symbols that moved inward are **re-exported** by the outer rings that used to own them; public call
   sites (`from bos.core import …`) are unchanged.
6. `pytest`, `ruff`, and `pyright` are green (per CLAUDE.md gate).

---

## 2. The Actor Ring — `bos.core.actor` (DONE; the system foundation)

The actor ring landed on `re-arch` as a **zero-dependency system foundation** — a peer of the agent ring,
not an outer ring. It is the domain-agnostic runtime for long-lived, mailbox-bound components: any system
of long-lived actors could build on it without the agent/conversation domain. (§2.4–§2.10 below record the
design journey and the still-open follow-ups; this summary is the as-built state.)

### 2.1 What it is — package layout

```
core/actor/__init__.py      # public surface (Actor, MailBox, EventBus, Event, Envelope, MessageType)
core/actor/base.py          # Actor — the domain-agnostic runtime (pump, lifecycle, emit)
core/actor/mailbox.py       # MailBox[ContentT = Any]  (point-to-point messaging endpoint)
core/actor/envelope.py      # Envelope[ContentT = Any] (message in transit)
core/actor/message_types.py # MessageType
core/actor/event_bus.py     # Event (marker) + EventBus (type-keyed pub/sub)
```

`bos.core.agent_actor.AgentActor` — the `Actor` that drives an `Agent` — is a **specialization that lives
in the harness ring**, not in this foundation (it imports both `bos.core.agent` and `bos.core.actor`).
The gateway subclasses it as `CoordinatedActor`, overriding `_on_turn_*` / `_build_host_sink` (a clean
inward dependency, kept).

### 2.2 Boundary — zero outward imports (guard-enforced)

`bos.core.actor` imports the **stdlib and itself only** — not `bos.protocol`, not `bos.core.agent`, not the
harness ring. The dependency points the other way: `bos.protocol` re-exports `Envelope`/`MessageType`
*from* this foundation. Enforced by
[tests/test_actor_ring_isolation.py](../../tests/test_actor_ring_isolation.py) (resolves relative imports,
so an escaping `from ..contract import` fails CI), mirroring the agent guard (§1.2).

### 2.3 What it owns — generic, domain-agnostic primitives

So the foundation never names the agent's `MessageContent`, the messaging primitives are **generic** (PEP
696 defaults, so bare `Envelope`/`MailBox` still mean `[Any]` — zero call-site churn; the harness ring
annotates `[MessageContent]` for precision):

- **`Envelope[ContentT = Any]`** — moved out of `bos.protocol`. Drops the agent-specific content
  validation (kept the generic "non-MESSAGE → str" invariant); the agent core validates message content
  as it processes a turn.
- **`MailBox[ContentT = Any]`** — `MailRoute.bind()` yields a bare `MailBox`; `AgentActor` can annotate
  `MailBox[MessageContent]`.
- **`Event`** (marker) + **`EventBus`** — the bus is **type-keyed**: `subscribe(event_type, handler)` +
  `emit(Event)`, dispatching over the event's MRO. Domain-agnostic, so `SessionEvent` (the harness-ring
  vocabulary) is *not* here — it subclasses `Event` in `core/contract.py`, and consumers subscribe to
  `SessionEvent` and discriminate on `.kind`. (Mechanism in the foundation; vocabulary in the harness
  ring — §2.10. This retired the type-keying deferral.)
- **`Actor`** ([base.py](../../src/bos/core/actor/base.py)) — the runtime: the `run()` pump
  (idle-tick hook → poll `receive_nowait` → `handle(env)`), `_spawn`/`aclose` task lifecycle, and
  `emit(Event)`. It knows nothing about turns/sessions/interrupts; `AgentActor` adds those via `handle()`
  + `_on_idle_tick()` + its `SessionEvent` emission.

`core/contract.py` (harness ring) imports all of these inward and **re-exports** them, so
`from bos.core.contract import MailBox` / `from bos.core import …` call sites are unchanged. `MailRoute` /
`Channel` / `ep_mail_route` stay in the harness ring and reference the foundation's `MailBox` inward —
the **[OPEN-B] → B2** decision (own the port in the foundation + re-export, not a separate messaging leaf);
**[OPEN-F]** (EventBus home) resolves the same way.

### 2.4 `CURRENT_MAILBOX` disposition — RESOLVED (B: deleted)

`CURRENT_MAILBOX` was **set/reset in the actor but read nowhere** — no `.get()` existed anywhere in `src`
or `tests`, pure dead plumbing. **[OPEN-A] resolved to option B: deleted as dead code.** Removed the
definition (`harness.py`), the actor's set/reset (and the now-unused `contextvars` import + `token`
plumbing + empty `finally`), and the `core/__init__.py` export. This also drops the actor's *only*
`.harness` import, eliminating that §2.2 violation outright. It is a breaking change to the public API
for any out-of-tree tool that read it — accepted, since nothing in-tree did and the value was never
populated for readers anyway. Gates green (665 tests, ruff, pyright). No re-export/alias kept.

### 2.5 The Agent public-API boundary — RESOLVED (by OPEN-D + a public method)

The actor used to reach into `Agent` privates from its command subsystem:
`_cmd_chats` → `agent._chat_store.list_chats()`, `_cmd_prompt` → `agent._build_system_prompt(ctx)`.

**OPEN-D dissolved most of this:** the command subsystem moved out of the actor entirely (§2.6), so the
`_chat_store` reach is gone — the gateway `CommandHandler` holds its *own injected* `ChatStore`, not the
agent's. The `chat_store`-injection-vs-property question (**[OPEN-C]**) is therefore moot; no actor
reaches the agent's chat store. The only residual private reach was `cli/commands/debug.py` introspecting
the prompt, fixed by promoting `Agent._build_system_prompt` → public **`Agent.build_system_prompt(ctx)`**.
No outer ring now touches `Agent` privates.

### 2.6 [OPEN-D] Does command/session handling belong in the actor? — RESOLVED (control plane extracted)

`AgentActor` bundled turn execution + slash-command dispatch + chat-cursor state (`ChatState`). Commands
(`/new`, `/resume`, `/chats`) manipulate *client cursors* — a session/transport concern, not an actor
one — and `ChatState` duplicated the gateway's `ChatCoordinator` cursor store.

**Resolved beyond the original D1/D2 framing: the control plane moved out of the actor entirely.** A
mailbox-free gateway `CommandHandler` (`core/command_handler.py`) runs `/new`/`/resume`/`/chats` against
the `ChatCoordinator` (the single cursor authority) + `ActorManager.retire_session`. Each channel
(ws/telegram/lark) detects a slash-command on inbound and calls the handler directly — commands never
become `COMMAND` envelopes and never reach the actor. `AgentActor` is now a **pure data-plane actor**
(pump → MESSAGE/INTERRUPT → run turns → emit `SessionEvent`s, plus `retire_session`); `ChatState`,
`_handle_command`, `_cmd_*`, and `current_chat_id`/`reset_chat` are deleted. Aliases and `/prompt` were
dropped as unneeded (the latter also kept OPEN-C out of scope). See the OPEN-D step commits on `re-arch`.

### 2.7 [OPEN-B] Is messaging its own ring? — RESOLVED (B2)

**Decided: the actor foundation owns the messaging primitives** (`Envelope`, `MessageType`, `MailBox`)
and the event primitives (`Event`, `EventBus`); `bos.protocol` re-exports the wire types downward.
`MailRoute`/`Channel` stay in the harness ring and reference the foundation's `MailBox` inward (a legal
dependency). A separate messaging *leaf* was the alternative, rejected because the actor foundation *is*
the home of these primitives in the two-foundation model — but it remains the escape hatch if messaging
later grows consumers for which "routing depends on an actor-foundation type" becomes wrong. **[OPEN-F]**
(EventBus home) resolved identically — the foundation.

### 2.8 Breaking changes & blast radius (verified)

- **Public API:** unchanged if we re-export from `core/__init__.py` and `core/contract.py`. `from
  bos.core import AgentActor, MailBox, LifecycleEvent, …` keeps resolving. `bos.core.actor` becoming a
  package keeps `from bos.core.actor import AgentActor, ActorTurnContext, ActorTurnResult, SessionState`
  working (tests rely on all four, incl. `SessionState`).
- **Deep-import breakages (only two, both tests):** `bos.core.events`
  (`tests/test_host_channel_sink.py`) and `bos.core.chat_state` (`tests/test_actor_commands.py`).
  Fixed by a re-export shim or a one-line edit each.
- **`MailBox` import sites** are wide (gateway, channels, runner, defaults, extensions, tests) but go
  through `bos.core` / `bos.core.contract` — re-export covers them.
- **`Agent` API additions** (§2.5) are additive; no existing signature changes.

### 2.9 Readiness

The actor ring landed on `re-arch` as the system foundation (§2.1–§2.3). **All open questions are now
resolved:** [OPEN-A] CURRENT_MAILBOX deleted (§2.4); [OPEN-B] messaging owned by the actor foundation
(§2.7); [OPEN-C] agent-private reaches removed + `build_system_prompt` made public (§2.5); [OPEN-D]
control plane extracted to a gateway `CommandHandler`, actor is pure data plane (§2.6); [OPEN-F] EventBus
in the actor foundation (§2.7/§2.10). [OPEN-E] (naming) — kept `AgentActor` (now in the gateway ring).

### 2.10 Event model — resolved (supersedes the event-port plan in §2.3)

Design review settled the event/notification model. There are **two planes with deliberately different
contracts** — they are not two instances of one abstraction, and must not be merged.

**Plane 1 — the turn stream (agent ring; settled).** Today's `EventSink` is renamed **`TurnEventSink`**
to name its payload (`TurnEvent`) and granularity (intra-turn). It is *not* a bus: it is an **emit-only
callback port** (`emit(TurnEvent)`); the Agent is a pure emitter and never subscribes. Pub/sub fan-out
(`HostChannelSink`) and the mailbox forwarder are concrete *consumers* wired by the actor, not part of
the port. This is an agent-ring rename (touches §1.3); `EventSink` is re-exported for back-compat. No
contract change.

**Plane 2 — platform events (outer rings).** `LifecycleBus`/`LifecycleEvent`/`LifecycleKind` are renamed
and generalized:

- `LifecycleEvent` → **`SessionEvent`**, `LifecycleKind` → **`SessionEventKind`**
  (`Literal["turn_complete", "session_close"]`). The subject is the *session* (every kind carries
  `chat_id`); "lifecycle" was an adjective with no noun — it never said *whose* life.
  - **Precondition (not a guarantee):** `session_close` is emitted **only on explicit client
    retirement** — `reset_chat`, `/new`, or `/resume` switching away — via the single emit site
    [`retire_session`](../../src/bos/core/actor.py#L205-L226). A merely *abandoned* session (client
    walks away, idle timeout, actor shutdown) emits **no** `session_close`; background work for those
    relies on the `idle` job trigger instead. So `session_close` is *"the session was deliberately
    closed,"* not *"every session eventually ends with this event."* Don't design subscribers that
    assume universal end-of-life delivery.
- The bus becomes one generic, **type-keyed `EventBus`** carrying a closed hierarchy rooted at a
  ring-neutral `Event` marker. Event **categories are Python types** (`SessionEvent`, a future
  `ActorEvent`, …); the per-category discriminator stays a typed `Literal` `kind`. Subscribe **by
  concrete type**:

  ```python
  @dataclass(frozen=True)
  class Event: ...                       # ring-neutral marker; depends on nothing

  @dataclass(frozen=True)
  class SessionEvent(Event):
      kind: SessionEventKind
      chat_id: str
      ...

  class EventBus(Protocol):                # end-state: subscribe-by-type
      def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None: ...
      async def emit(self, event: Event) -> None: ...
  ```

- **Landed (type-keyed):** the renames + the `Event` base marker shipped first as a kind-keyed
  rename-only step (with back-compat aliases); the **type-keyed bus then landed** when `Event`/`EventBus`
  moved into the actor foundation. Making the bus a *domain-agnostic foundation primitive* was the
  trigger that retired the deferral — a kind-keyed bus typed to `SessionEventKind` cannot live in a
  domain-agnostic foundation. So `EventBus` is now `subscribe(event_type, handler)` + `emit(Event)`,
  dispatching over `type(event).__mro__`; `SessionEvent` is harness-ring vocabulary and consumers
  discriminate on `.kind`.
- **One bus instance**, created and owned by the harness, injected into actors (the wiring is unchanged
  from today — only the type generalizes when the 2nd category lands). A new event category is a new
  `Event` subclass + an `emit` call — **never a new bus type**.
- **Why one mechanism, not a bus-per-category:** `SessionEvent`, `ActorEvent`, etc. share one delivery
  contract — best-effort, broadcast, fire-and-forget to anonymous background subscribers. *Same contract
  → same mechanism.* (Contrast `TurnEventSink`: a different contract — emit-only, inner-ring,
  client-facing — so it stays a separate port. The deciding principle throughout: same delivery contract
  → one type; different contract → different type.)

**Messages vs. events (the routing rule).** The actor produces two categorically different outputs:

| | **Message** (the reply) | **Event** |
|---|---|---|
| Addressing | point-to-point to `reply_recipient` | broadcast, anonymous |
| Reliability | must arrive | may have zero subscribers; may drop |
| Carrier | `MailBox` | `EventBus` |

The reply is the `Agent.ask()` return value, sent via `mailbox.send(...)` **before** `turn_complete` is
emitted — it is **never** routed through the bus (correlation, reliability, and ordering all forbid it;
see today's order at [actor.py:384-407](../../src/bos/core/actor.py#L384-L407)). Membership rule:
*best-effort broadcast notification → `EventBus`; anything needing a delivery guarantee, ordering, or
persistence is a message (mailbox) or a job (runner), not an event.*

**Port ownership.** `EventBus` + base `Event` are a small ring-neutral *mechanism*; per-category event
*vocabularies* are owned by the ring that emits them (`SessionEvent` with the actor). The harness
provides the concrete `DefaultEventBus` and the singleton instance. This **revises §2.3**: the line that
moved `LifecycleBus`/`LifecycleEvent`/`LifecycleKind` verbatim now reads `EventBus` + `Event` /
`SessionEvent`; the inward-move + re-export mechanics are unchanged, and the old names are re-exported
(deprecated) for back-compat.

**[OPEN-F]** Does the generic `EventBus` + `Event` marker live in the actor-ring contract, or in a shared
inner leaf beside `bos.protocol` (the same ring-neutral-mechanism question as [OPEN-B] for messaging)?
Lean: give it the **same home as the messaging decision** — keep the two ring-neutral mechanisms
together. Non-blocking for the D1 ring-move.

---

## 3. Outer Rings — `harness` / `config` / `extension(+defaults)`, then `gateway` / `cli` (not yet authored)

Reserved. These follow the same approach once the actor ring lands: identify outward imports and
inner-privacy reaches, move owned ports inward, re-export for compat, give the ring an inner default per
port, and verify zero outward imports. Each will be added as its own numbered section when we reach it,
so decisions made during extraction are captured here rather than guessed up front.

---

## Open Questions

- ~~**[OPEN-A]** `CURRENT_MAILBOX`: move-and-keep or delete?~~ **RESOLVED → B (deleted); §2.4.**
- ~~**[OPEN-B]** Does messaging become its own leaf ring, or live in the actor ring?~~ **RESOLVED → actor foundation owns it + re-export; §2.7.**
- ~~**[OPEN-C]** Give the actor `chat_store` by injection or via a public property?~~ **RESOLVED → moot (OPEN-D removed the reach); `Agent.build_system_prompt` made public for the debug CLI; §2.5.**
- ~~**[OPEN-D]** Also extract the command/session subsystem from `AgentActor`?~~ **RESOLVED → control plane moved out to a gateway `CommandHandler`; actor is pure data plane; §2.6.**
- ~~**[OPEN-E]** Naming: keep `AgentActor`/`CoordinatedActor`?~~ **RESOLVED → kept `AgentActor` (moved to the gateway ring); `CoordinatedActor` folded into it.**
- ~~**[OPEN-F]** Where does `EventBus` + `Event` live?~~ **RESOLVED → actor foundation; §2.7/§2.10.**

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-22 | Initial authoring | Establish the dependency-ring model; document the completed `agent` ring as the reference shape (§1); open the `actor` ring for design with proposal + open questions (§2) |
| 2026-06-22 | Resolve event model (§2.10) | Rename `EventSink`→`TurnEventSink` (emit-only callback, not a bus); rename+generalize `LifecycleBus`/`LifecycleEvent`/`LifecycleKind` → a single type-keyed `EventBus` over a `SessionEvent` hierarchy rooted at `Event`; codify the messages-vs-events routing rule; add [OPEN-F] |
| 2026-06-22 | Land the rename-only step | Implemented the §2.10 renames + `Event` base marker on `re-arch` with old names kept as back-compat aliases (subscription left kind-keyed; type-keying deferred to the 2nd event category). Gates green: 665 tests pass, ruff clean, pyright 0 errors. Tests untouched — proves the aliases hold |
| 2026-06-22 | Resolve [OPEN-A] (§2.4) | Deleted `CURRENT_MAILBOX` as dead code (set/reset, never read): removed from `harness.py`, the actor (incl. the now-unused `contextvars`/`token`/`finally` plumbing — dropping the actor's only `.harness` import), and `core/__init__.py`. Eliminates one §2.2 outward violation. Gates green: 665 tests, ruff, pyright |
| 2026-06-22 | **Two-foundation pivot** | Decided `actor` is a *system foundation* — a zero-dep peer of `agent`, below `bos.protocol` (which becomes a downstream facade). `agent` is the *domain foundation* and depends on nothing (guard added, §1.2). `AgentActor` becomes a harness-ring composition. Resolves [OPEN-B]/[OPEN-F] → foundation owns messaging + event primitives |
| 2026-06-22 | Land the actor foundation (§2) | Built `bos.core.actor` as a zero-dep foundation over six green increments: relocate `AgentActor`→`agent_actor.py`; move `Envelope`(generic)/`MessageType`/`MailBox`(generic) out of `bos.protocol`/`contract` into the foundation; move `Event`/`EventBus` in and make the bus type-keyed; extract the domain-agnostic base `Actor` and reseat `AgentActor` on it. `bos.protocol` lazily re-exports; strict isolation guard added. Gates green throughout: 668 tests, ruff, pyright 0 |
| 2026-06-23 | Move `AgentActor` to the gateway; fold `CoordinatedActor` | `AgentActor`'s only consumer is the gateway, so it moved to `bos.gateway` and absorbed `CoordinatedActor` (optional `chat_coordinator`); dropped from `bos.core`'s public surface. Then consolidated the gateway internals into `core/` (coordinator, resolver, command_handler, channel_context) / `actors/` / `channels/` subpackages |
| 2026-06-23 | Resolve [OPEN-D] (§2.6) | Extracted the slash-command control plane out of the actor into a mailbox-free gateway `CommandHandler` over the `ChatCoordinator`; rewired ws/telegram/lark to call it on inbound `/`; stripped the actor's command subsystem + deleted `ChatState`. `AgentActor` is now pure data plane. Dropped aliases + `/prompt`. Gates green: 658 tests, ruff, pyright 0; both ring guards green |
| 2026-06-23 | Resolve [OPEN-C]/[OPEN-E] (§2.5) | OPEN-D removed the actor's agent-private reaches, mooting the chat_store question; promoted `Agent._build_system_prompt` → public `build_system_prompt` for the debug CLI (the last private reach). [OPEN-E]: kept `AgentActor` (gateway ring), folded away `CoordinatedActor`. **All §2 open questions resolved.** 658 tests, ruff, pyright 0 |
