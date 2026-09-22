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

## 4. Sequencing

BEP 17 shipped in full (Layers 1–3), except its documentation step 13, which was
deliberately deferred: the *Embedding the gateway* page waits on BEP 18, or it
gets written before the two-mode framing exists and has to be rewritten. BEP 18
§6 step 8 owns that page now.

§3 is orthogonal to both and can run in parallel with either.
