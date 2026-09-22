# BEP 18: The Embedding SDK and Explicit Agent Selection

- **Status:** Draft
- **Depends on:** BEP 13 (concentric rings — `bos.sdk` becomes a ring and needs its own guard), BEP 16 (the embeddable-library direction and the embed contract this BEP makes real), BEP 6 (configuration architecture), BEP 7 (actors — this BEP stops the in-process paths from reaching into them)
- **Blocked by:** nothing.

---

## 1. Motivation

BOS is becoming an embeddable agent framework with two shapes, and both already work — what is missing is that neither is *named*:

1. **No gateway.** Everything in one process. The target is "build a Claude Code-like command-line agent on BOS." `boscli ask` is this shape today, in ten lines.
2. **Gateway.** The BEP 7 runtime with actors and channels, mounted in a host or run standalone. BEP 17 finished it.

Three concrete gaps stand between that and a framework someone else can build on.

**The bootstrap sequence is tribal knowledge, written out three times.** `resolve_agents()` → `bootstrap_platform()` → `harness()` appears at [`mount.py:275-278`](../../src/bos/runner/mount.py), [`cli/commands/agent.py:391-392`](../../src/bos/cli/commands/agent.py) (with the `async with` at `:416`), and again at `agent.py:515-516`. The order matters — `resolve_agents` must precede `bootstrap_platform`, which registers what it loaded — and nothing states it.

**There is no import path that *is* the contract.** BEP 16 §3.6 documents the supported surface as prose: some of `bos.core`, some of `bos.config`, and "everything `_`-prefixed is unstable". `bos.core.__init__` re-exports 27 underscore-prefixed helpers, so an embedder reading the module cannot tell a promise from an internal.

**The in-process paths reach through gateway concepts to find an agent.** `boscli ask` without `--agent` runs:

```python
runtime = ws.config.runtime
actors = runtime.actors if runtime else {}
actor_cfg = actors[ws.resolve_default_actor()]
agent_kind = actor_cfg.agent
```

`ask` never creates an actor — it binds no mailbox, takes no address, and does not enter `ActorManager`. It consults the actor table only to learn a name. A project that deliberately has no gateway is told `runtime.actors must define at least one actor for the gateway runtime.`, and `resolve_gateway_actors` imports `ResolvedActorConfig` from `bos.gateway.config`, so mode 1's default path drags in the gateway's config shapes.

### 1.1 What this BEP is *not* claiming

The SDK is thin, and mostly extraction rather than invention. The raw API is already five lines; §3.3 and §3.4 exist to remove ordering knowledge, de-duplicate three copies, and give the contract a name — not to hide `Agent`.

---

## 2. Goals and Non-Goals

### 2.1 Goals

1. One import path, `bos.sdk`, whose exports are the embedding contract. What is not exported is not promised.
2. The bootstrap sequence exists once, and `bos.cli` and `bos.runner` use the same copy an embedder does.
3. Agent selection never routes through the actor table. A project with no gateway never sees a gateway concept, in its config, its code path, or its error messages.
4. Where an actor's mediation *is* wanted, it is asked for explicitly and by that name.
5. A project no longer grows a `mailboxes/` directory it never delivers into.
6. `bos.sdk` is a ring with its own CI guard, like the seven before it.

### 2.2 Non-Goals

1. **A `BosApp.ask()` convenience.** `Agent.run` takes ten parameters (`interrupt`, `ctx_metadata`, `llm_args`, `event_sink`, `turn_id`, `commit_observer`, `schema`, `max_schema_retries`, plus `chat_id` and the content). A façade either mirrors all of them forever or sends users back down a layer the moment they want streaming or structured output. Worse, `create_agent` is async and meant to be called once and reused — [`examples/embed_fastapi.py`](../../examples/embed_fastapi.py) builds the agent in its lifespan — so an `ask()` that builds one per call would teach a pattern worse than the raw API. `BosApp` returns `Agent`; `Agent` keeps its full surface.
2. **An actor path in the SDK.** SDK users do not model actors. `Workspace.resolve_gateway_actors()` remains for anyone who does.
3. **Narrowing `bos.core.__init__`.** BEP 16 §2.2.4 declined this and the reasoning holds: `bos.sdk` is a *new* surface, not a removal from an existing one. The 27 underscore helpers stay exactly where they are.
4. **A pre-tool interception point.** Recorded in `docs/BACKLOG.md` §3; orthogonal and wants its own BEP.
5. **Changing what an actor is, or how the gateway resolves one.** BEP 7's model is untouched.
6. **Storage ports.** BEP 17 §2.2.1 settled this; §3.7 fixes one eager `mkdir`, not the storage model.

---

## 3. Design

### 3.1 Runtime shape

| Artifact | Runtime form | Lifecycle | Who invokes it |
|---|---|---|---|
| `bos.sdk.open_harness(workspace)` | Async context manager | Per harness | `BosApp`, `boscli ask`, `GatewayMount` |
| `bos.sdk.BosApp` | Plain object held by the embedder | `async with`; caches agents for its lifetime | An embedding application |
| `Workspace.resolve_default_agent()` | Method, pure | Once per resolution | `BosApp`, `boscli ask` |
| The eighth ring guard | pytest test | Per CI run | CI |

No process, task, queue or scheduler is introduced.

### 3.2 Where `bos.sdk` sits

`bos.sdk` imports `bos.core` and `bos.config`. It must not import `bos.gateway`, `bos.runner`, `bos.cli`, `bos.extensions` or `bos.exts` — adapters arrive through `ep_*`, as everywhere else.

Read as rules rather than a picture, because that is how the guards enforce it:

- `bos.sdk` imports `bos.core` and `bos.config`, and nothing outward.
- `bos.runner` may import `bos.sdk`; `bos.cli` may import both.
- `bos.gateway` imports neither — it is inward of `bos.config` and knows nothing of either.


`bos.runner` may import `bos.sdk`: its guard forbids only `bos.cli`, `bos.extensions` and `bos.exts` ([`test_runner_ring_isolation.py:26`](../../tests/test_runner_ring_isolation.py)), so `GatewayMount` adopting the shared bootstrap needs no rule change.

**The sketch `core → sdk → {cli, gateway}` does not hold** and is recorded here so it is not re-proposed: `bos.gateway` may not import `bos.config` (its own module docstring says so, and [`test_gateway_ring_isolation.py`](../../tests/test_gateway_ring_isolation.py) enforces it), while an SDK needs `Workspace`. The gateway is pure runtime policy handed a resolved config by a composition root; it sits beside the SDK, not on it.

### 3.3 `open_harness` — the function layer

```python
@asynccontextmanager
async def open_harness(workspace: Workspace) -> AsyncIterator[AgentHarness]:
    """Resolve agents, bootstrap the platform, and open the harness."""
```

It performs, in this order: `workspace.resolve_agents()`, `workspace.bootstrap_platform()`, then yields from `workspace.harness()`. The order is the point — `bootstrap_platform` registers the agents `resolve_agents` loaded, and reversing them silently drops every agent file.

All three existing copies become calls to it: `cli/commands/agent.py`'s `ask` and gateway-start pre-flight, and `GatewayMount._bring_up_runtime`. `GatewayMount` keeps its own `AsyncExitStack` — it needs the harness to outlive a function scope — and enters this context manager into it.

### 3.4 `BosApp` — the object layer

```python
class BosApp:
    def __init__(self, config: dict | RootConfig | Workspace, *, bos_dir: str | Path | None = None) -> None: ...
    async def __aenter__(self) -> BosApp: ...
    async def __aexit__(self, *exc) -> None: ...
    def agent(self, kind: str | None = None) -> Agent: ...
    async def build_agent(self, kind: str) -> Agent: ...
    @property
    def harness(self) -> AgentHarness: ...
    @property
    def workspace(self) -> Workspace: ...
```

`agent()` returns a cached `Agent`, built on first request for that kind and reused after — which is the pattern `examples/embed_fastapi.py` already demonstrates by hand. With no argument it uses `workspace.resolve_default_agent()` (§3.5).

`agent()` is synchronous and `create_agent` is not, so the agents are built during `__aenter__`: every kind in `config.agents` plus the resolved default. A kind that exists only in `AgentRegistry` (an `@ep_agent` factory) and is not named in config is built on first `agent(kind)` call — which then needs an await. **Resolution:** `agent()` raises for an unbuilt kind and names `await app.build_agent(kind)`, rather than returning a coroutine from a method that usually returns an `Agent`. Two shapes from one method is worse than one extra method.

`harness` and `workspace` are exposed deliberately: dropping to the lower layer must not be a different path, it must be the same objects `BosApp` is using.

### 3.5 Default agent resolution

```python
def resolve_default_agent(self) -> str:
    """The agent kind to use when the caller names none. Never consults actors."""
```

In order:

1. `default_agent`, if the config sets it
2. exactly one entry in `config.agents` → that one
3. `"main"` in `config.agents` → `"main"`
4. otherwise raise, listing the available kinds and naming the three ways out: `default_agent`, `--agent`, `--actor`

`default_agent` is a **top-level** key, not `[agent] default`. `[agent.defaults]` already exists and means something entirely different — the defaults merged into *every* agent. `agent.default` beside `agent.defaults` is one letter apart with unrelated meanings, which is the look-alike collision this repo's process exists to avoid. `RootConfig` is `extra="allow"` ([`schema.py:202`](../../src/bos/config/schema.py)), so adding the field is additive and older configs are unaffected.

**The actor table is not in this list, at any position.** That is the whole point: a mode-1 project never touches a gateway concept, and a gateway project's actor mediation is asked for by name (§3.6).

### 3.6 Three explicit paths in `boscli ask`

| Invocation | Resolution |
|---|---|
| `ask "…"` | `resolve_default_agent()` — plain agent, no actor |
| `ask --agent X "…"` | agent kind `X`, plain |
| `ask --actor Y "…"` | `actors[Y].agent`, with `actors[Y].agent_cfg` applied |

`--agent` and `--actor` are mutually exclusive. `--agent` keeps its current name and meaning: it already names an agent kind and validates against `AgentRegistry`, and renaming it to `--actor` would be *more* misleading, because `ask` creates no actor.

`--actor` is new, and it is the only place the in-process CLI reaches the actor table. It exists because an actor can override its agent's whole configuration: `ActorConfig.agent_cfg` is a full `AgentConfig` ([`schema.py:137`](../../src/bos/config/schema.py)), the reference template teaches it (`config/template.toml:139-141`), and `ActorManager` applies it to every actor it builds ([`actor_manager.py:130-133`](../../src/bos/gateway/actors/actor_manager.py)). So an actor in a real deployment can run something no agent kind alone reproduces. An operator debugging a gateway wants exactly what that actor runs; `--actor` gives it, and says so in its name.

### 3.7 A project no longer grows an unused `mailboxes/`

`AgentHarness` always constructs a mail route, and [`JsonlMailRoute.__init__`](../../src/bos/core/defaults/jsonl_mailbox.py) calls `self._dir.mkdir(parents=True, exist_ok=True)` eagerly. Mode 1 never delivers an envelope, so every such project grows a directory it never writes into. The `mkdir` moves to the first write. Same category as the scaffolded `WordCount` tool removed for the same reason: do not create artifacts nobody asked for.

### 3.8 What `bos.sdk` exports — and what that promises

`bos.sdk.__all__` is the embedding contract. BEP 16 §3.6 stated this surface in prose; this makes it importable:

- `BosApp`, `open_harness`
- `Agent`, `AgentHarness`, `AgentResult`, `Message`, `TurnContext`
- the ports: `LLM`, `ChatStore`, `Consolidator`, `ToolSet`, `TurnInterceptor`, `PromptProvider`, `TurnEventSink`
- the extension points: `ep_tool`, `ep_provider`, `ep_agent`, `ep_chat_store`, `ep_mail_route`, `ep_consolidator`, `ep_turn_interceptor`, `ep_channel`, `ep_plugin`
- `Workspace`, `RootConfig`, `validate_config`

Everything else — including every `_`-prefixed helper `bos.core` re-exports — is available and explicitly unstable. `bos.sdk` re-exports; it does not redefine, so there is one class and one `isinstance` answer per name.

### 3.9 The eighth ring guard

BEP 13's rule is that every ring is guard-enforced, and there are seven guard files, one per ring. `bos.sdk` gets `tests/test_sdk_ring_isolation.py`, forbidding `bos.gateway`, `bos.runner`, `bos.cli`, `bos.extensions` and `bos.exts`, and forbidding underscore-prefixed reaches into `bos.core` — the same two rules the config guard carries.

### 3.10 Look-alikes

| Name | What it is | Not |
|---|---|---|
| `default` **preset** | A shipped config file (`presets/default.toml`) whose main actor runs the `BOS` agent | An agent named `default` — there is none; `ep_agent` registers only `BOS` and `bos_config` |
| `default_agent` | A top-level config key naming the agent to use when the caller names none | `[agent.defaults]`, the config merged into every agent |
| **agent** | A definition: prompt, model, tools, plugins. Registered in `AgentRegistry`, selected by `--agent` | An **actor** |
| **actor** | A named, addressable runtime instance with a mailbox (`agent@main`) that *runs* an agent kind. A gateway concept. Selected by `--actor` | An agent |
| `BosApp` | The SDK's lifecycle object: harness plus an agent cache | `GatewayMount`, which owns a lock, a state machine and an ASGI app |

---

## 4. Audience flows (end state)

### 4.1 Embedder — building a CLI agent

```bash
pip install bos-ai[litellm]   # no extra beyond the base for the SDK itself
```

```python
from bos.sdk import BosApp

async with BosApp(CONFIG, bos_dir=".bos") as app:
    agent = app.agent()                       # the default; or app.agent("researcher")
    result = await agent.run("chat-1", "hello", event_sink=MySink())
```

Chat continuity is passing the same `chat_id`. Streaming, structured output and interrupts are `Agent.run`'s own parameters — nothing is hidden. Dropping to the lower layer is `app.harness` and `app.workspace`, the same objects.

### 4.2 End user — the CLI

`boscli ask "…"` runs the project's default agent. In a scaffolded project that is `[agents.main]`; in a preset it is the preset's `default_agent`. `--agent` and `--actor` are the two explicit overrides (§3.6).

### 4.3 Operator

Unchanged. `boscli gateway start/status/restart/stop` and the gateway's own actor resolution are untouched — §3.5's rule governs only the callers that name no agent, and the gateway always names one through its actor table.

### 4.4 Background / automated

Unchanged. No new periodic work.

---

## 5. Compatibility and fallout

### 5.1 Breaking: `boscli ask` no longer applies actor overrides by default

Today a bare `ask` in a project with actors reproduces what the main actor runs, including `agent_cfg`. It now runs the plain default agent; `--actor <name>` restores the old behaviour and says what it is doing.

No shipped config demonstrates this — the `team` preset did and was deleted (BEP 9 revision, 2026-09-22). It hits user projects that set `[runtime.actors.<name>.agent_cfg]`, which `config/template.toml:139-141` documents and which is the only way to give one actor a tool set, plugin binding or system prompt the bare agent kind does not have. Such a project's bare `ask` silently loses those overrides. The release note must state both halves — that a bare `ask` stopped applying actor overrides, and that `ask --actor <name>` is the invocation that behaves as before.

### 5.2 Breaking: a project with actors but no agents and no `default_agent` errors on a bare `ask`

`presets/default.toml` is exactly this shape — its `config.agents` is empty and the agent comes from `@ep_agent` through the actor table, so today the actor table is the *only* thing naming an agent there. It gains `default_agent = "BOS"`, so the shipped path keeps working. A user project shaped the same way gets §3.5's rule-4 error, which names the three ways out.

### 5.3 Not breaking

- Every `bos.core` and `bos.config` name keeps its current home and meaning; `bos.sdk` re-exports.
- `--agent` keeps its name, meaning and validation.
- Gateway behaviour, actor semantics, the wire protocol, and all other `boscli` commands.
- Scaffolded projects: they generate `[agents.main]`, so §3.5's rule 3 covers them with no config change.
- Ring topology. The seven existing guards pass unmodified; an eighth is added.

### 5.4 Third-party impact

None known. `bos.sdk` is additive.

---

## 6. Implementation plan (dependency-ordered)

1. **`resolve_default_agent()` on `Workspace`, plus the `default_agent` field** (§3.5). Independent of the SDK; lands first because both `ask` and `BosApp` consume it. Tests: each of the four rules, including that the error names no gateway concept.
2. **Add `default_agent` to `presets/default.toml`**, and document the key in `config/template.toml` (§5.2). Without this, step 4 breaks `boscli ask` with no project.
3. **`bos.sdk` with `open_harness` and the ring guard** (§3.3, §3.9). Re-point all three existing bootstrap copies at it (`ask`, the gateway-start pre-flight, `GatewayMount._bring_up_runtime`) — behaviour-preserving, and the existing suites are the guard.
4. **`--actor`, and `ask`'s three paths** (§3.6). Only now does `ask` stop reaching into the actor table.
5. **`BosApp`** (§3.4), built on step 3's `open_harness`.
6. **`bos.sdk.__all__` and the contract** (§3.8), plus an `examples/embed_sdk.py` alongside the two existing examples, and its CI step.
7. **The mailbox `mkdir`** (§3.7). Independent of everything above; sequenced last because it is the smallest.
8. **Documentation**: the *Embedding* page BEP 16 §3.6 promised and BEP 17 §6 step 13 deferred, now written once against the two-mode framing; the release note for §5.1–5.2.

---

## 7. Acceptance criteria

1. Given a config with `[agents.assistant]` and no `[runtime]` section at all: `BosApp(config).agent()` returns an `Agent`, and nothing in the call imports `bos.gateway`.
2. Given the same config: `boscli ask "…"` succeeds, and the failure message for an ambiguous case mentions no actor, no gateway and no runtime.
3. Given a config whose `[runtime.actors.main]` carries an `agent_cfg` override: a bare `ask` runs the agent **without** the override, and `ask --actor main` runs it **with** the override. Both halves are asserted against the same fixture, so neither can pass by accident.
4. `--agent` and `--actor` together exit non-zero with a message naming both.
5. `grep -rn "resolve_agents()\|bootstrap_platform()" src/bos/cli src/bos/runner` returns nothing: the sequence exists once, in `bos/sdk/`, and every former copy calls it.
6. `tests/test_sdk_ring_isolation.py` passes, and the seven existing ring guards pass **unmodified**.
7. Given a mode-1 project run to completion: no `mailboxes/` directory exists under its `bos_dir`.
8. `import bos.sdk` on a base install (no extras) succeeds.
9. `uv run pytest -q`, `uv run ruff check src tests examples`, and `npx -y pyright src` are green, with pyright at zero errors.

---

## 8. Open questions

None outstanding.

- **Whether `BosApp` should offer `ask()`** — resolved as no (§2.2.1). `Agent.run`'s ten parameters and the per-call agent-construction trap are the reasons.
- **Whether `--agent` should be renamed `--actor`** — resolved as no (§3.6). `--agent` already names an agent kind; `ask` creates no actor, so the rename would add confusion rather than remove it. The ambiguity came from the *default* path, which §3.5 removes.
- **Where `default_agent` lives** — resolved as a top-level key, not `[agent] default`, to avoid colliding with `[agent.defaults]` (§3.5).

---

## 9. Revision history

- 2026-09-22 — Draft. Decisions: `bos.sdk` is a function layer plus a deliberately small object, not an object model — `BosApp` returns `Agent` and has no `ask()` (§2.2.1); agent selection never consults actors, and the actor path becomes the explicit `--actor` flag rather than a hidden default (§3.5, §3.6); `default_agent` is top-level to avoid the `[agent.defaults]` look-alike (§3.5); `bos.sdk` is a ring with its own guard, and the tempting `core → sdk → gateway` layering is recorded as impossible under BEP 13's rules (§3.2, §3.9). Grounded findings: the bootstrap sequence is written three times, not two (`mount.py:275-278`, `agent.py:391-392`+`:416`, `agent.py:515-516`); `presets/default.toml` has an empty `config.agents`, so the actor table is today the *only* thing naming an agent there, which is why §5.2 is a real break and why step 2 precedes step 4; no shipped config carries an actor override, because the one that did — the `team` preset — was deleted the same day (BEP 9 revision, 2026-09-22), so §5.1's breaking change is real but its worked example is a user project setting `[runtime.actors.<name>.agent_cfg]`, a shape `config/template.toml:139-141` teaches.
