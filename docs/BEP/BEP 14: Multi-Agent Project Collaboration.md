# BEP 14: Multi-Agent Project Collaboration

Status: **design — forward-looking draft.** Not slated for near-term implementation. This BEP captures
the *intended end-state* for multiple agents collaborating on a goal, so that incremental work (and the
simpler subagent-first path) does not paint the system into a corner.

Builds on: Named Actors (BEP 2), actor/mailbox foundation (BEP 13), async-task layer (BEP 11),
ChatStore (BEP 5).

---

## 0. Motivation

A BOS deployment can run several named actors in one process — e.g. a Telegram team: Ryan (PM), Bob
(architecture), Carlos (developer), Ann (test). Today every conversation is strictly **human ↔ one
agent**: an `AgentActor` reacts to an inbound envelope and replies to `env.sender`
([agent_actor.py:144](../../src/bos/gateway/actors/agent_actor.py#L144),
[:342](../../src/bos/gateway/actors/agent_actor.py#L342)). The agents cannot collaborate.

The goal: the human states a **goal** ("implement OAuth login"), and the agents **collaborate over time**
to achieve it, while the human can **watch and steer** the collaboration without reading every
inter-agent message.

The trap to avoid: do **not** make the collaboration happen *inside the channel conversation*. A chat
thread is a linear message stream; collaboration is long-lived, concurrent, and stateful. Forcing it
through the chat requires loop bounds, fan-out spam control, and quiescence heuristics — accidental
complexity that signals the wrong abstraction.

The right decomposition separates three things the channel currently conflates:

- **transport** (the channel — an edge adapter to Telegram),
- **coordination state** (a board — shared task state),
- **execution** (agents — actors and/or subagents).

---

## 1. Guiding principle: subagent-first, escalate only when forced

> **Resolve a collaboration need with the simplest topology that works, and escalate only when that
> topology genuinely cannot.**

The ladder, cheapest first:

1. **One agent.** A single capable agent. Most software tasks end here.
2. **One agent + subagents.** The agent delegates bounded subtasks to ephemeral `AskSubagent` children
   ([subagent.py:148](../../src/bos/plugins/subagent.py#L148)) and synthesizes. No new infra; works
   today. *This is the default for "a PM that decomposes work."*
3. **Standing actors + board** (this BEP). Only when subagents provably cannot meet a need — see the
   escalation triggers in §5.

This principle matters because multi-agent collaboration is **not free**: it fragments context, lets
agents step on each other, adds coordination overhead, and is far harder to debug. A "team of personas"
is intuitive to humans but does not automatically produce better output than one strong agent with
subagents. So the burden of proof is on escalation, and this BEP is the destination *if and when* the
lower rungs are exhausted — not a default to reach for.

---

## 2. Scope and Non-Goals

### In scope (the end-state)

1. A **Project**: a first-class, long-lived domain object scoping a goal, its board, and its history —
   independent of any channel connection.
2. A **Board**: shared task state (cards with status / owner / dependencies / artifacts) that is both the
   **source of truth** for the work and the **human's dashboard**.
3. A **Dispatcher**: wakes the right agent when a card transitions to a state it owns (event → enqueue →
   execute), reusing BEP 11 — **not** a new scheduler.
4. **Agents as workers** that read and write the board, mixing standing actors (roles) and subagents
   (scratch helpers) per §5.
5. **Channel demoted to a view/control surface** onto a project (summaries, push notices, commands) —
   one of potentially several views.

### Non-Goals

- A general workflow/DAG/BPMN engine. The board is a kanban of tasks with simple state transitions, not a
  declarative process language.
- Automatic goal decomposition as platform logic. *Which* cards exist and *who* owns them is a planner
  **agent's** reasoning; the platform provides the board and the wiring, not the plan.
- Message-passing as the primary coordination mechanism. Agents coordinate **through the board** (shared
  state), not by addressing each other. Direct messaging may exist as a secondary affordance but is not
  the design's backbone (§4.4 explains why).
- Cross-process / multi-host distribution (inherits BEP 11's single-process v1 stance).
- A new LLM-call primitive, scheduler, or queue. Reuse BEP 11 (`JobRunner`/`EventBus`/`BackgroundLLM`)
  and BEP 13 (`MailRoute`/`MailBox`). A domain BEP must not grow private infra (CLAUDE.md BEP rules).
- Replacing `AskSubagent`. It remains the rung-2 tool and an actor's internal helper (§5).

---

## 3. Vocabulary (and look-alike disambiguation)

| Term | Meaning |
|---|---|
| **Project** | Long-lived domain object: a goal, its board, participants, artifacts, history. Source of truth for the work. Exists with or without a channel attached. |
| **Board** | The project's shared task state — cards (status, owner, deps, artifacts). The coordination substrate (blackboard) and the human's dashboard. |
| **Card** | One unit of work with a lifecycle state (e.g. `backlog → ready → in-progress → review → blocked → done`), an owner (role/actor), and links/artifacts. |
| **Dispatcher** | The component that, on a card transition, enqueues work for the responsible agent. A consumer of BEP 11 (`EventBus` → `JobRunner`), not new infra. |
| **Actor** | A standing, addressable `AgentActor` at `agent@<name>` (BEP 2): own mailbox, own scoped memory/persona, long-lived. A *role* on the team. |
| **Subagent** | An ephemeral child agent run **synchronously inside one turn** (`AskSubagent`). No mailbox, no address, no standing identity. A *scratch helper*, not a team member. |
| **Channel** | Edge adapter bridging an external client (Telegram). A **view/control surface** onto a project — transport, never the source of truth. |

The four distinctions that keep this design honest:

- **Project ≠ Channel** — domain state vs transport. (BEP 13 rings: domain vs edge adapter.)
- **Board ≠ chat transcript** — structured state the human watches vs a linear message stream.
- **Actor ≠ Subagent** — persistent role vs transient helper (§5).
- **Board coordination ≠ message-passing** — agents react to shared state, not to each other's DMs.

---

## 4. Decomposition

### 4.1 Project as a first-class domain object

A `Project` owns: the goal, the board, the participant set (which roles/agents), produced artifacts, and a
history of board events. It is **not** owned by a channel; a channel *attaches* to a project as a view.
This is what lets multiple views (Telegram summary, TUI kanban, web board) observe one project without any
of them being the source of truth, and lets the project outlive any connection.

This is precisely the "ongoing work, goals, constraints not derivable from the code" that the BEP process
says must have an explicit owner and store — so the Project/Board is a **new domain store**, not a
projection of chat history.

### 4.2 Board as the coordination primitive (a blackboard)

Two ways agents can coordinate:

- **Message-passing**: agents DM each other. Needs addressing, loop prevention, termination detection;
  the human must read the cross-talk to follow along.
- **Blackboard / board** (this design): agents coordinate through shared state. Carlos does not message
  Ann — Ann moves a card to `blocked:needs-fix`, Carlos observes the transition and picks it up.

The board is chosen because it makes the three hardest problems of multi-agent coordination disappear:

1. **Termination is structural** — the workflow is done when all cards are `done`. No quiescence heuristic.
2. **Visibility is structural** — the board *is* the dashboard. The human watches state, not a firehose.
   No fan-out / spam problem.
3. **Decoupling is structural** — agents need no addresses or routing; they react to card transitions.
   The A→B→A loop problem largely evaporates because coordination is mediated by state, not messages.

What the board does **not** give for free is the **trigger** — something must wake the responsible agent
on a relevant transition. That is the dispatcher (§4.3), and it is the one place real machinery is needed.

### 4.3 Dispatcher as a BEP 11 consumer

A card transition is an event; waking the owning agent is enqueued work; the agent's turn is the
execution. That is exactly BEP 11's shape — **trigger → enqueue → execute** — so the dispatcher is a
*consumer* of `EventBus` + `JobRunner` ([contract.py:181](../../src/bos/core/contract.py#L181)), not a new
scheduler. A "card moved to `ready` for role=developer" event binds (via `bind_trigger`) to a job that
delivers the card to the developer actor's mailbox (or spins one up — §5 lazy pool).

> Readiness note: these services are declared on the harness but currently initialized to `None`
> ([harness.py:271-273](../../src/bos/core/harness.py#L271)) — BEP 11 is contract-defined, not yet wired.
> The dispatcher is therefore **blocked on BEP 11 landing** (§12).

### 4.4 Why not primary message-passing

Agents *may* still send each other notes (e.g. an actor leaving a comment on a card, or pinging a peer).
But messaging is a **secondary affordance layered on the board**, never the backbone. If messaging were
primary the system would inherit loop bounds, termination detection, and fan-out spam — the very
complexity the board removes. Keeping the board primary is what keeps the design simple.

### 4.5 Channel as a view/control surface

The channel collapses to a thin adapter onto a project:

- **Out:** board summaries and push notices ("Ann blocked card 4", "Carlos finished build, in review").
  Not every inter-agent event — a digest the human actually wants.
- **In:** control commands (`/board`, `/approve 3`, `@ryan add a task: …`) and goal statements.

The existing per-turn status-message mechanism ([telegram.py:559](../../src/bos/extensions/channels/telegram.py#L559))
already shows the channel is a presentation layer; this extends that role to "render a project," not "be
the project."

---

## 5. Topology decision: subagent vs actor

Mental model: **actors are the org chart; subagents are an actor's private scratch helpers.** They
compose hierarchically — actors are peers on the board; each actor uses subagents internally for bounded
exploration.

| | `AskSubagent` (ephemeral child) | `AgentActor` (standing peer) |
|---|---|---|
| Lifetime | One turn, then gone | Long-lived |
| Identity / address | None | `agent@carlos`, own mailbox |
| Memory | Throwaway child chat | Own scoped persona memory (BEP 2) |
| Concurrency | Parent blocks on it | Independent, concurrent |
| Human-addressable | No | Yes |
| Lifecycle cost | On-demand, free when idle | Resident slot + context |
| Good for | Bounded subtask; parent synthesizes | A persistent role that accumulates context and can be addressed |

**Decision criterion: transient task vs persistent role.** Per §1, default to subagents and escalate a
role to a standing actor **only** when one of these is genuinely required (the escalation triggers):

- **Concurrency** — the role must run *while* another role runs, over a long span (Carlos coding *while*
  Ann tests). Subagents block their parent; actors don't. (And one shared `chat_id` serializes turns
  anyway — `ChatCoordinator` permits one active turn per chat,
  [chat_coordinator.py:279](../../src/bos/gateway/core/chat_coordinator.py#L279) — so concurrency means
  separate sessions, i.e. separate actors.)
- **Persistence / identity** — the role must accumulate role-specific memory across many turns and tasks
  (BEP 2 scoped memory), not start fresh each delegation.
- **Human-addressability** — the human must be able to talk to the role directly mid-flight
  (`@carlos hold the refactor`). Subagents have no address.

If none of these hold, a subagent is the correct, cheaper choice — *resolve with the subagent topology
until it cannot.*

**Lazy actor pool (the middle path).** Even when roles are actors, they need not all be resident. The
dispatcher can **start** an actor when the board first assigns it a card and **retire** it when the
project idles — `ActorManager` already supports start/stop/restart
([actor_manager.py:75](../../src/bos/gateway/actors/actor_manager.py#L75),
[:81](../../src/bos/gateway/actors/actor_manager.py#L81)). This avoids paying for four resident agents to
ship one feature, while preserving identity/memory when summoned.

---

## 6. End-state flows

### 6.1 End-user (human)

```
Human → @ryan: "implement OAuth login"
Ryan (planner turn): creates Project "oauth-login", seeds the board:
   card#1 Design auth flow        → owner: bob      state: ready
   card#2 Implement OAuth         → owner: carlos   state: backlog (dep: #1)
   card#3 Integration test plan   → owner: ann      state: ready
Dispatcher wakes Bob (#1) and Ann (#3) concurrently.
Bob completes #1 → moves to done → unblocks #2 → dispatcher wakes Carlos.
Carlos builds #2 → moves to review.
Ann finds an edge case → opens card#4 (blocked:needs-fix, owner carlos) → dispatcher re-wakes Carlos.
…board reaches all-done…
Channel pushes the human a digest: "OAuth login complete. 4 cards done. Summary + branch link."
```

The human watches the **board** (via `/board` or a TUI), not the inter-agent traffic, and can interject
at any time (reassign a card, comment, `@carlos …`).

### 6.2 Operator / admin

- **Inspect:** `boscli project status <id>` → board snapshot, per-card owner/state, in-flight dispatcher
  jobs (`JobRunner.list`), active turns (`chat_coordinator.active_turns_status()`), resident actors.
- **Recover:** a stuck card's work is a `JobRunner` job → `retry`/`cancel`. A wedged role → `retire_session`
  ([actor_manager.py:67](../../src/bos/gateway/actors/actor_manager.py#L67)) and re-dispatch. The board is
  the durable record, so recovery is "re-run the card," not "replay a chat."

### 6.3 Background / automated

Card transitions emit events; the dispatcher binds them to jobs (BEP 11). No daemon of its own — the
`JobRunner` loop already lives in the runtime TaskGroup (BEP 11 §4). A project with no ready cards and no
active turns is quiescent; the dispatcher does nothing until the next transition (human input, or a card
freed by a completed dependency).

---

## 7. Runtime shape (what each thing *is*)

| Thing | Runtime form | Lives where | Lifecycle |
|---|---|---|---|
| Project | Domain aggregate + store record | new domain ring module + a `ProjectStore` | created on goal; persists until archived |
| Board | State within the Project aggregate | same store | mutated by agent tool calls |
| Dispatcher | A service subscribed to the `EventBus`, submitting to `JobRunner` | gateway/runtime ring | runs for the process lifetime in the runtime TaskGroup |
| Card-tool surface | Agent tools (`CreateCard`, `UpdateCard`, `ClaimCard`, `CommentCard`) registered by a `ProjectPlugin` | plugin (mirrors `SubagentPlugin`) | per-turn |
| Actor (role) | `AgentActor` (BEP 2), possibly lazily pooled | gateway actors ring | start on assignment / retire on idle |
| Channel view | Existing `Channel` adapter, project-aware | extensions | per-connection |

The only genuinely new long-lived runtime component is the **Dispatcher**; everything else is a store, a
plugin tool surface, or existing actors/channels.

---

## 8. Ownership / source of truth

| Concern | Owner |
|---|---|
| Goal, board state, cards, artifacts, project history | **Project / `ProjectStore`** (new domain store) |
| Card transitions → waking agents | **Dispatcher** (consumes BEP 11 `EventBus` + `JobRunner`) |
| Job execution / retry / cancel / status | `JobRunner` (BEP 11) |
| What cards exist, who owns them, when done | the **planner agent** (policy, not platform) |
| Per-actor turn execution + concurrency | `AgentActor` + `ChatCoordinator` (unchanged invariant) |
| Role identity / scoped memory | `AgentActor` + BEP 2 ScopedMemory |
| Per-turn conversation history | `ChatStore` (BEP 5), per actor working session |
| Human presentation / control | Channel view (and/or TUI/web), never source of truth |

Note the deliberate split: **mechanism** (board store, dispatcher, jobs) is platform; **policy** (the
plan: which cards, which owners, when done) is the planner agent's reasoning. The platform never authors
the plan.

---

## 9. Staged, dependency-ordered plan

Bottom-up; each stage is independently valuable and gates the next. This is also the **escalation ladder**
of §1 — do not start a later stage until the earlier one is proven insufficient.

- **Stage 0 — Validate the premise (no new infra; buildable today).** A single planner actor (Ryan) that
  decomposes a goal and uses `AskSubagent` for dev/test/arch work, synthesizing the result. Measures
  whether multi-role collaboration *adds value* over one strong agent before any platform investment. If
  it does not, stop here.
- **Stage 1 — Project + Board domain object.** `ProjectStore`, the card model + lifecycle, and a
  `ProjectPlugin` exposing board tools to agents. Channel gains a `/board` view + digest push. Still
  subagent-executed (no standing worker actors yet) — the board organizes a *single* orchestrator's work
  and makes it visible. Proves the board abstraction with minimal moving parts.
- **Stage 2 — Dispatcher + standing actors (the full end-state).** Bind card-transition events to
  `JobRunner` jobs that wake role actors (lazily pooled, §5). Promote the roles that hit an escalation
  trigger (§5) from subagents to actors. This is the only stage that requires BEP 11 to be **landed**, not
  just contract-defined.

Each stage ships as small PRs with tests (CLAUDE.md: small reversible diffs).

---

## 10. Breaking changes / fallout

The design is **additive** — it leaves the existing single-agent channel path untouched:

- **No change** to `Envelope`, `MessageType`, `MailRoute`, or the `AgentActor` reply path. Collaboration
  is mediated by the board, not by re-routing agent replies or relaxing the turn coordinator.
- **New domain store** (`ProjectStore`) and a new domain ring module — net new, no existing call sites.
- **New `ProjectPlugin`** tool surface — additive, mirrors `SubagentPlugin`.
- **Dispatcher** subscribes to the existing `EventBus`; depends on BEP 11 being wired (`events`/`jobs` are
  `None` today, [harness.py:271-273](../../src/bos/core/harness.py#L271)).
- **Channel** gains project-view rendering — additive; non-project channels unchanged.
- Topology relaxations from BEP 2 (`agent@` channel targets) remain sufficient; no new ref types needed.

---

## 11. Configuration (illustrative; forward-looking)

```toml
[main.projects.oauth-login]
goal_owner = "ryan"                 # the planner/orchestrator agent
roles = ["bob", "carlos", "ann"]    # candidate worker roles (actor or lazily-pooled)
board = "kanban"                    # board flavor
lazy_actors = true                  # start a role actor on first assignment, retire on idle (§5)

[plugins.ProjectPlugin]
enabled = ["ryan", "bob", "carlos", "ann"]   # who may use board tools

[[main.channels]]
name = "TelegramChannel"
bind_address = "channel@telegram-team"
target_project = "oauth-login"      # channel attaches to a project as a view
```

Backwards compatible: no `[main.projects]` / no `target_project` → today's single-actor-per-channel
behavior is unchanged.

---

## 12. Cross-BEP dependencies and readiness

| Dependency | Needed for | Status |
|---|---|---|
| BEP 11 `JobRunner` + `EventBus` (`services.jobs` / `services.events`) | Stage 2 dispatcher | **Contract-defined but NOT wired** — `None` on the harness ([harness.py:271-273](../../src/bos/core/harness.py#L271)). Hard gate for Stage 2. |
| BEP 2 Named Actors + `ActorResolver` | Stage 2 role actors | Implemented. Ready. |
| BEP 2 ScopedMemory | role identity/memory | Design in BEP 2; confirm implementation before Stage 2. |
| BEP 13 actor/mailbox foundation | delivery | Implemented. Ready. |
| BEP 5 ChatStore | per-actor working sessions | Implemented. Ready. |
| `AskSubagent` / `SubagentRuntime` | Stages 0–1 | Implemented ([subagent.py](../../src/bos/plugins/subagent.py)). Ready. |

**Readiness:** Stage 0 is buildable now (and should be done first regardless). Stage 1 needs only the new
`ProjectStore`/plugin — no BEP 11. Stage 2 is **blocked on BEP 11 being wired**, not merely defined. Do
not mark this BEP "ready" for Stage 2 until `harness.jobs`/`harness.events` are real.

---

## 13. Open questions

1. **Board granularity** — fixed lifecycle states, or per-project configurable columns? Start fixed.
2. **Card concurrency** — can two agents hold the same card? Likely single-owner with explicit handoff
   (move to another owner) to keep coordination unambiguous.
3. **Planner re-entry** — does the planner agent re-run on every board change (expensive) or only on
   defined triggers (card blocked, all-done)? Lean triggered.
4. **Artifact storage** — where do produced artifacts (branches, files, docs) live and how does a card
   reference them? Likely links/refs, not blobs in the store.
5. **Human-as-participant** — is the human a board owner (cards assignable to the human, e.g. "approve")?
   Probably yes; it models the approval gate cleanly.
6. **Multi-project actors** — if one actor serves several projects, working sessions key by
   `(actor, project)`. Defer unless needed.

---

## 14. Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-24 | Initial draft | Define the intended end-state for multi-agent collaboration: separate transport (channel) from coordination state (board) from execution (agents); adopt blackboard coordination over message-passing; establish the subagent-first escalation principle and the actor-vs-subagent decision criteria. Forward-looking draft; dispatcher gated on BEP 11 wiring. |
