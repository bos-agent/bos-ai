# Backlog

Ideas and known gaps that are **not yet designed**. A BEP records accepted design
direction; this file records what has not earned one yet, so it stops living in
chat logs and scratch files.

An item leaves this file in one of two ways: it becomes a BEP, or it is dropped
with a reason. Nothing here is a commitment.

---

## 1. `bos.sdk` and the two embedding modes

**Status:** idea, vetted against the code, not designed.
**Blocks:** nothing. **Blocked by:** nothing. See §4 for a sequencing note.

BOS is becoming an embeddable agent framework with two distinct shapes, and they
should be stated as a product promise rather than left as an implementation detail:

1. **No-gateway mode** — everything in one process. The target is "build a
   Claude Code-like command-line agent on BOS."
2. **Gateway mode** — the BEP 7 runtime with actors and channels. The target is
   "build an OpenClaw/Hermes-like agent application on BOS."

### They already coexist

This is not a new architecture. `boscli ask` is mode 1 today — its own docstring
says "Runs the agent in this process (not via the gateway) … no gateway is left
running" — and it is roughly ten lines: `workspace → harness() → create_agent()
→ agent.run(event_sink=…)`. `boscli gateway start` + `boscli tui` is mode 2.
Both read one config, one set of agents, one set of tools/plugins/skills.

BEP 13's rings already encode the split and CI enforces it: `bos.core` (+
`bos.config`) is mode 1; `bos.gateway` is mode 2's addition, one-directional.

### The proposed layering, with one correction

The natural sketch is `core → sdk → {cli, gateway}`. **The `sdk → gateway` arrow
cannot hold.** `bos.gateway` may not import `bos.config` (ring guard, stated in
`gateway/config.py`'s own module docstring), and an SDK needs `Workspace`. The
gateway is pure runtime policy handed a resolved config by a composition root.

The shape that holds:

```
bos.core
  ├── bos.config ── bos.sdk ──────────┐
  └── bos.gateway ── bos.runner ──────┤     (bos.runner may import bos.sdk)
                                      └── bos.cli
```

Two siblings on core, joined at the CLI — not a chain.

### What would actually be in it

Thin, and mostly extraction rather than invention:

1. **The `workspace → bootstrapped harness → agent` boilerplate.** It exists
   twice today: in `boscli ask`, and in `GatewayMount._bring_up_runtime`, whose
   docstring already calls itself "the one place that knows how a `Gateway` is
   assembled." `bos.runner` importing `bos.sdk` is permitted by its ring guard
   and would leave one copy.
2. **Default-agent resolution without gateway concepts** — fixes §2 below.
3. **A named public surface.** BEP 16 §3.6 currently documents the embed contract
   as a prose list of `bos.core` names. Note BEP 16 §2.2.4 explicitly declined to
   narrow `bos.core.__init__` because "there is no external embedder yet"; two
   named use cases change that premise. The SDK should be a *new* surface, not a
   narrowing of `bos.core`'s exports.

It needs no dependency extra — `bos.core` + `bos.config` is the base install, so
mode 1 costs the base 14 MB and nothing more.

### What this is not

**Do not rewire `boscli tui` to skip the gateway.** `tui` is the only CLI command
that uses `GatewayClient` (`cli/commands/agent.py:762`), and its gateway
dependency is load-bearing: a TUI attached to a running gateway shares one chat
with Telegram/Lark users and gets BEP 7's shared chat resume, stale-client
protection and takeover. Mode 1 wants a *new* in-process interactive loop —
`ask` is already its single-turn form — not a rewiring of `tui`.

---

## 2. Mode 1 still has to declare gateway concepts

**Status:** two small, verified defects. Natural to fix alongside §1.

- **`ask` without `--agent` routes through gateway config.**
  `resolve_default_actor()` → `resolve_gateway_actors()` (`config/workspace.py:704`),
  which raises `ValueError("runtime.actors must define at least one actor for the
  gateway runtime.")` when `[runtime].actors` is absent, and imports
  `ResolvedActorConfig` from `bos.gateway.config`. A project that deliberately has
  no gateway is required to configure an actor, and is told off about a "gateway
  runtime" it never asked for.
- **Every project grows an unused `mailboxes/` directory.** `AgentHarness` always
  builds a `mail_route`, and `JsonlMailRoute.__init__` eagerly `mkdir`s
  (`core/defaults/jsonl_mailbox.py:84`). Mode 1 never delivers an envelope. Same
  category as the scaffolded `WordCount` tool that was removed for the same reason.

---

## 3. No interception point before a tool runs

**Status:** real capability gap. Wants its own BEP; orthogonal to BEP 17.

`InterceptorStage` (`core/agent/contract.py:22`) is:

```
"prepare", "before_llm", "after_llm", "after_tool", "final_response",
"max_iteration", "shutdown", "error"
```

There is no `before_tool`, and no approval/permission mechanism anywhere in
`bos.core`. A tool call can be observed after it runs, never gated before it.

That makes the defining interaction of a Claude Code-like agent — "may I run this
bash command?" — impossible to build on BOS today, which is a hard requirement
for §1's mode 1.

The design question is not the hook, it is the **veto semantics**: what the model
receives when a call is refused, whether a refusal ends the turn or feeds back as
a tool result, how it interacts with `parallel_safe` tools already in flight, and
whether the decision is per-call, per-tool or per-session. Several defensible
answers — hence a BEP rather than a patch.

---

## 4. Sequencing against BEP 17

§1–§3 do not conflict with BEP 17 Layer 2 (ASGI transport, dropping aiohttp):
Layer 2 lives entirely in `bos.gateway`, the transport and the extras, while §1
lives on `bos.core` + `bos.config`. Neither design depends on the other.

Two notes:

- **One file-level overlap, not a design conflict.** Layer 2 step 9 rewrites
  `runner.serve()` for uvicorn; §1 would change `GatewayMount._bring_up_runtime`
  to call the SDK. Different functions in one package — sequence them, do not
  interleave them.
- **Do §1 before BEP 17 Layer 3's documentation step (13).** That step writes an
  "Embedding the gateway" page. Written before the two-mode framing exists, it
  would have to be rewritten afterwards. Layer 3's code steps (11 and 12) are
  unaffected.

Suggested order: **BEP 17 Layer 2 → §1 (+§2) → BEP 17 Layer 3 → §3**, with §3
able to run in parallel with any of them.

---

## 5. BEP 17 Layer 3 prerequisites

**Status:** parked during the Layer 1 review, with rulings. Recorded here because
they become real defects the moment Layer 3 gives `restart()` a caller.

- **`GatewayMount.restart()`'s failure path wedges the mount.** It leaves
  `_gateway`/`_stack` non-`None` while reporting `standby`, so the
  `if self._gateway is not None: return False` re-entry guard refuses every later
  `acquire()`. This is the same defect that *was* fixed for the promotion path;
  the restart path kept it because `restart()` has no production caller yet.
  `POST /api/restart` is that caller. **The Layer 3 plan should open with this.**
- **`bos/runner/__main__.py` captures `gateway` once.** After a hot restart the
  first SIGTERM calls `request_shutdown()` on the replaced gateway and does
  nothing; the second escalates to a non-graceful stop. It must read
  `mount.gateway` at signal time.
- **`Gateway.status_snapshot()` hardcodes `"runtime": "process"`.**
  `GatewayMount` carries a `runtime_label`, but it never reaches `gateway.state`,
  so BEP 17 §3.4.4's stated reason for keeping that file — an embedded gateway
  being visible to `boscli gateway status` and `doctor` — is not yet delivered.
  Harmless in Layer 1, where the runtime genuinely is a process.
