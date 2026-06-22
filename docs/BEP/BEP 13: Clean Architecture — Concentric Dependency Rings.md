# BEP 13: Clean Architecture — Concentric Dependency Rings

Status: **living design** — authored on the `re-arch` branch and updated *along with* implementation,
not finalized up front. This is deliberate: the right boundary for each ring only becomes clear while
extracting it, so this BEP is graduated **ring by ring**. Per-ring readiness:

| Ring | Status |
|---|---|
| `agent` (innermost) | ✅ **done** — landed on `re-arch` (commits `73aa773`…`2f8d21f`); documented here as the reference shape (§1) |
| `actor` | 🟡 **under design / debate** (§2) — proposal + open questions below |
| `harness`, `config`, `extension (+defaults)` | ⬜ not yet authored — same approach, added when we get there |
| `gateway`, `cli`, … (outermost) | ⬜ not yet authored |

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

The intended layering, innermost → outermost (the arrow reads "is depended on by"):

```
bos.core.agent  →  bos.core.actor  →  { harness, config, extension(+defaults) }  →  { gateway, cli, runner, … }
```

`bos.protocol` is a **leaf** that sits beside the agent ring: it owns the wire types (`Envelope`,
`MessageType`) and *lazily* re-exports agent-owned types (`MessageContent`, `TurnEvent`) so that
importing `bos.protocol` never triggers `bos.core` at module-init time
([protocol/__init__.py](../../src/bos/protocol/__init__.py)). Any ring may import `bos.protocol`.

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

## 2. The Actor Ring — `bos.core.actor` (UNDER DESIGN — to be debated/finalized)

> Everything in §2 is a proposal. Open questions are flagged **[OPEN]** and are the agenda for review.
> This section will be rewritten as decisions land.

### 2.1 What the actor is today

`AgentActor` ([actor.py](../../src/bos/core/actor.py)) is the concurrency/session shell around an
`Agent`. At runtime it is **driven by `run()`**, a loop that polls a bound `MailBox`, and it owns, per
`chat_id`: a task slot, an interrupt/abort buffer, a generation counter, and reply-routing state. It
also emits lifecycle events (`turn_complete`/`session_close`) on an injected `LifecycleBus`, wires the
per-turn `EventSink` (mailbox forwarder + host channel sink), and hosts a small **slash-command**
subsystem (`/chats`, `/prompt`, `/new`, `/resume`) backed by `ChatState` cursor/alias storage.

Its sole in-core consumer is the gateway, which **subclasses** it as `CoordinatedActor`
([actor_manager.py:23](../../src/bos/gateway/actor_manager.py#L23)), overriding the protected hooks
`_on_turn_started` / `_on_turn_finished` / `_build_host_sink`. That subclass-via-protected-hooks surface
is already a clean inward dependency (gateway → actor) and is **kept**.

### 2.2 Current dependency violations

The actor's imports ([actor.py:11-17](../../src/bos/core/actor.py#L11-L17)) classify as:

| Import | Target ring | Verdict |
|---|---|---|
| `bos.protocol` (Envelope, MessageType, MessageContent, TurnEvent) | leaf / inner | ✅ legal |
| `.agent` (Agent, TurnContext, AbortTurn) | inner (agent) | ✅ legal |
| `.chat_state` (ChatState, ChatStateError) | sibling module, stdlib-only | ⚠️ relocate into the ring |
| `.events` (HostChannelSink, MailboxEventSink, CLIENT_TURN_EVENT_TYPES) | sibling module | ⚠️ relocate into the ring |
| `.contract` (MailBox, EventBus, SessionEvent, SessionEventKind) | **outer (harness ring)** | ❌ **outward** |
| ~~`.harness` (CURRENT_MAILBOX)~~ | — | ✅ **resolved** — `CURRENT_MAILBOX` deleted as dead code ([OPEN-A]→B); the actor no longer imports `.harness` at all |

The `.contract` import still breaks the rule. The actor depends on ports defined in the *harness-ring* contract
([contract.py](../../src/bos/core/contract.py)). (The former outward dependency on `CURRENT_MAILBOX` in
`harness.py` is gone — see §2.4.)

### 2.3 Proposed end state

Promote `core/actor.py` to a package `core/actor/`, mirroring the agent extraction:

```
core/actor/__init__.py     # public surface: AgentActor, ActorTurnContext, ActorTurnResult,
                           #   SessionState, + re-export of the ports it now owns
core/actor/actor.py        # AgentActor
core/actor/contract.py     # ports the actor owns: MailBox, EventBus + Event/SessionEvent (see §2.10)
core/actor/events.py       # HostChannelSink, MailboxEventSink, derive_event_sink, CLIENT_TURN_EVENT_TYPES
core/actor/chat_state.py   # ChatState, ChatStateError
```

**Port ownership moves inward.** `MailBox`, `LifecycleBus`, `LifecycleEvent`, `LifecycleKind` move from
`core/contract.py` into `core/actor/contract.py`. The harness-ring `core/contract.py` then **re-exports**
them inward (exactly the §1.5 pattern), so `PluginServices` (references `LifecycleBus`), `JobRunner.bind_trigger`
(references `LifecycleEvent`), `MailRoute.bind() -> MailBox`, and `Channel.run(mailbox)` keep compiling
unchanged. After the move the actor imports **none** of these from `core.contract`. *(The three
`Lifecycle*` names are renamed & generalized to `EventBus` / `Event` / `SessionEvent` per §2.10; the
inward-move + re-export mechanics here are unchanged, and the old names are re-exported for back-compat.)*

**What stays outer.** `MailRoute`, `ep_mail_route`, and `Channel` remain in `core/contract.py` — the
actor never touches them (it only consumes an already-bound `MailBox`). They depend inward on the
actor-owned `MailBox`. *(See [OPEN-B] on whether messaging deserves its own ring.)*

**`events.py` consumers.** The harness imports `derive_event_sink` from here
([harness.py:40](../../src/bos/core/harness.py#L40)) for its subagent runtime; that becomes a legal
inward import (harness → actor). `bos.core.events` is imported directly by one test
(`tests/test_host_channel_sink.py`); `bos.core.chat_state` by one test (`tests/test_actor_commands.py`).
These either get a thin re-export shim at the old path or a one-line import update.

### 2.4 `CURRENT_MAILBOX` disposition — RESOLVED (B: deleted)

`CURRENT_MAILBOX` was **set/reset in the actor but read nowhere** — no `.get()` existed anywhere in `src`
or `tests`, pure dead plumbing. **[OPEN-A] resolved to option B: deleted as dead code.** Removed the
definition (`harness.py`), the actor's set/reset (and the now-unused `contextvars` import + `token`
plumbing + empty `finally`), and the `core/__init__.py` export. This also drops the actor's *only*
`.harness` import, eliminating that §2.2 violation outright. It is a breaking change to the public API
for any out-of-tree tool that read it — accepted, since nothing in-tree did and the value was never
populated for readers anyway. Gates green (665 tests, ruff, pyright). No re-export/alias kept.

### 2.5 The Agent public-API boundary (the real design issue)

The actor currently reaches into **`Agent` privates**, which is an inner-ring privacy violation even
though Python allows it:

- `_cmd_chats` → `self._agent._chat_store.list_chats()` ([actor.py:558](../../src/bos/core/actor.py#L558))
- `_cmd_prompt` → `self._agent._build_system_prompt(ctx)` ([actor.py:579](../../src/bos/core/actor.py#L579))

Per rule §1.6.4, an outer ring talks to an inner ring only through published API. Proposed fix (small,
additive to the *agent* ring):

- Inject `chat_store: ChatStore` into `AgentActor` directly (it is available at construction — the
  harness already holds it), so commands stop borrowing the agent's. This is symmetric with the existing
  optional `chat_state` parameter. *Alternative:* add a public `Agent.chat_store` property.
- Promote `Agent._build_system_prompt` to public `Agent.build_system_prompt(ctx)` (drop the underscore).

**[OPEN-C]** Inject `chat_store` into the actor vs. expose it as an `Agent` property — which is the
cleaner ownership story?

### 2.6 [OPEN-D] Single-responsibility: does command/session handling belong in the actor?

`AgentActor` today bundles five concerns: (1) turn execution & concurrency — its true job; (2) slash-command
dispatch; (3) chat-cursor/alias state (`ChatState`); (4) lifecycle emission; (5) host event-sink wiring.
Commands like `/new` and `/resume` manipulate *client cursors* — arguably a **session/transport**
concern, not an actor concern. Two paths:

- **D1 — Pure ring-move:** keep all five in `AgentActor`; only fix import direction and privacy. Smallest
  diff, fastest to green. The seams become visible but unaddressed.
- **D2 — Also split:** extract the command/session subsystem (commands + `ChatState`) into its own
  collaborator the actor delegates to. Cleaner SRP, larger diff, and it raises a follow-on question of
  whether that collaborator is part of the actor ring or the harness ring.

Recommendation: **D1 now**, and capture D2 as a named follow-up so the ring extraction isn't blocked on
an SRP debate. To be decided in review.

### 2.7 [OPEN-B] Is messaging its own ring?

`Envelope`/`MailBox`/`MailRoute` are used by channels and the gateway with **no actor involved**.
Folding `MailBox` under the *actor* ring works (the actor is the in-core consumer), but a defensible
alternative is a separate inner **messaging/transport** leaf (beside `bos.protocol`) that both the actor
ring and the gateway depend on. This affects where `MailBox`/`MailRoute` are owned. Decide before moving
the ports, since it changes the target module.

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

The pure ring-move (§2.3–§2.5, path **D1**) depends on nothing outside this branch and is implementable
now. It is smaller than the agent extraction was. **Gate:** [OPEN-A] (CURRENT_MAILBOX) ✅ resolved (§2.4);
[OPEN-B] (messaging ring), [OPEN-C] (chat_store injection) should be resolved before the remaining code;
[OPEN-D] (SRP split) and [OPEN-F] (EventBus home) can be deferred without blocking.

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

- **Implemented now (rename-only step):** the renames + the `Event` base marker **landed** on
  `re-arch` (`EventSink`→`TurnEventSink`; `LifecycleBus`/`LifecycleEvent`/`LifecycleKind` →
  `EventBus`/`SessionEvent`/`SessionEventKind`; `DefaultLifecycleBus`→`DefaultEventBus`), with the old
  names kept as **back-compat aliases** (tests pass untouched, pyright/ruff green). The *subscription
  API is deliberately left kind-keyed* — `subscribe(kind: SessionEventKind, handler)` and `emit(event:
  SessionEvent)` — because subscribe-by-type buys nothing while `SessionEvent` is the only category.
  The switch to the type-keyed signature above (and `emit` dispatch over `type(event).__mro__`) is
  deferred to **when the second category (`ActorEvent`) actually lands** — rule-of-three, not
  speculation. The `E = TypeVar("E", bound=Event)` and `type[E]` machinery arrive then.
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

- ~~**[OPEN-A]** `CURRENT_MAILBOX`: move-and-keep (A) or delete as dead code (B)?~~ **RESOLVED → B (deleted); §2.4.**
- **[OPEN-B]** Does messaging (`Envelope`/`MailBox`/`MailRoute`) become its own inner leaf ring, or live
  in the actor ring?
- **[OPEN-C]** Give the actor `chat_store` by injection or via a public `Agent.chat_store` property?
- **[OPEN-D]** Pure ring-move now (D1) vs. also extract the command/session subsystem (D2)?
- **[OPEN-E]** Naming: keep `AgentActor`/`CoordinatedActor`, or rename as part of the move? (Lean: keep.)
- **[OPEN-F]** Does the generic `EventBus` + `Event` marker live in the actor-ring contract or a shared
  inner leaf beside `bos.protocol`? (Lean: same home as the [OPEN-B] messaging decision; §2.10.)

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-22 | Initial authoring | Establish the dependency-ring model; document the completed `agent` ring as the reference shape (§1); open the `actor` ring for design with proposal + open questions (§2) |
| 2026-06-22 | Resolve event model (§2.10) | Rename `EventSink`→`TurnEventSink` (emit-only callback, not a bus); rename+generalize `LifecycleBus`/`LifecycleEvent`/`LifecycleKind` → a single type-keyed `EventBus` over a `SessionEvent` hierarchy rooted at `Event`; codify the messages-vs-events routing rule; add [OPEN-F] |
| 2026-06-22 | Land the rename-only step | Implemented the §2.10 renames + `Event` base marker on `re-arch` with old names kept as back-compat aliases (subscription left kind-keyed; type-keying deferred to the 2nd event category). Gates green: 665 tests pass, ruff clean, pyright 0 errors. Tests untouched — proves the aliases hold |
| 2026-06-22 | Resolve [OPEN-A] (§2.4) | Deleted `CURRENT_MAILBOX` as dead code (set/reset, never read): removed from `harness.py`, the actor (incl. the now-unused `contextvars`/`token`/`finally` plumbing — dropping the actor's only `.harness` import), and `core/__init__.py`. Eliminates one §2.2 outward violation. Gates green: 665 tests, ruff, pyright |
