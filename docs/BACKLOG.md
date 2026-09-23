# Backlog

Ideas and known gaps that are **not yet designed**. A BEP records accepted design
direction; this file records what has not earned one yet, so it stops living in
chat logs and scratch files.

An item leaves this file in one of two ways: it becomes a BEP, or it is dropped
with a reason. Nothing here is a commitment.

---

<!-- §1 "bos.sdk and the two embedding modes" and §2 "Mode 1 still has to declare
     gateway concepts" left this file by becoming a BEP, which is one of the two
     exits described above. They are now:

       docs/BEP/BEP 18: The Embedding SDK and Explicit Agent Selection.md

     §3 keeps its number so BEP 18 §2.2.4's reference to it stays valid. -->

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

## 4. Nothing stops a `BosApp` and a mounted gateway sharing one process

**Status:** known hazard, documented but unguarded. Wants a BEP because the
obvious fix crosses a ring boundary.

`bos.sdk._app._ACTIVE` refuses a second `BosApp` while one is live, because
`bootstrap_platform()` writes `os.environ` and clears `AgentRegistry` — both
process-global. It guards `BosApp` against `BosApp` only. `open_harness` and
`bootstrap` never touch `_ACTIVE`, so `GatewayMount._bring_up_runtime` brings a
whole second runtime up beside a live `BosApp` without a word, and the reverse
holds too.

What breaks is quiet: agents already built keep working, but the registry now
describes the other side's workspace, so the next `create_agent` — a new actor,
`BosApp.build_agent()` — resolves against it. `POST /api/restart` re-triggers
`_bring_up_runtime`, so a long-lived host hits this on every restart, not only
at mount.

The obvious fix — one shared guard — is not obvious. `_ACTIVE` lives in
`bos.sdk`, which under BEP 13's rings imports only `bos.core` and `bos.config`;
`bos.runner` may import `bos.sdk` but not the other way round, so the guard
cannot simply be read from both sides where it is. Moving it inward to
`bos.core` or `bos.config` makes a process-lifecycle concern part of a layer
that has none today. That choice is the design question, along with whether the
answer is a refusal at all or a single owned bootstrap both modes go through.

Documented meanwhile in `docs/site/embedding/index.md` and `src/bos/llm-full.md`
§3: mount a gateway *or* hold a `BosApp`, never both.

---

## 5. Sequencing

BEP 17 and BEP 18 have both shipped in full. BEP 17's documentation step 13 was
deliberately deferred so the page could be written after the two-mode framing
existed rather than before it; BEP 18 §6 step 8 discharged it as
`docs/site/embedding/index.md`, which covers both modes together.

§3 and §4 are orthogonal to both and can run in parallel with either.
