# CLAUDE.md

This file provides guidance to AI coding agent while working with code in this repository.

## Tooling

- Use `uv run ...` for all Python entrypoints. The project targets Python `>=3.13`; the system `python3` may be too old and fail on `tomllib`.
- Run the full test suite: `uv run pytest -q`
- Run a single test file: `uv run pytest -q tests/test_harness.py`
- Run a single test: `uv run pytest -q tests/test_harness.py -k test_name`
- Lint: `uv run ruff check src tests`
- CLI help: `uv run boscli --help`
- Prefer `uv run boscli ...` for local CLI invocation (not system `boscli`).

## Repo Layout

```
src/            - BOS source code
tests/          - pytest coverage mirroring the above
docs/BEP/       - BOS enhancement proposals. design decisions for BOS system.
pyproject.toml  - BOS project configuration
```

## Working Notes

- Prefer small, reversible diffs. Extend existing patterns before adding new abstractions.
- Pull request titles must follow semantic/conventional format (`feat(config): ...`, `fix(runner): ...`).
- `uv run ruff check src tests` is a useful signal but the repo may contain pre-existing lint findings.
- `src/bos/core/__init__.py` is the public API surface. Internal helpers with `_` prefix are exported for use by extensions but are not considered stable.

## BEP Process

BOS Enhancement Proposals live in `docs/BEP/` and capture accepted design direction before broad architectural changes.

### Authoring — a BEP must cover these (the parts agents most often miss)

- **End state, not just mechanism.** Describe the finished system through concrete flows for *every* audience it touches — end-user, admin/operator, and background/automated — not only the internal mechanics. If a flow has no story (e.g. how an operator inspects, triggers, or recovers), the design is incomplete.
- **Runtime shape.** Say what each new thing *is* at runtime — process / actor / job / service / function — where it runs, its lifecycle, and who invokes it. "An agent that does X" with no runtime form is a gap, not a design.
- **Ownership and source of truth, per concern.** For every piece of state, config, and write path, name the single owner. Separate generic platform mechanism from domain policy, and never let a domain BEP grow a private scheduler / queue / LLM-call / event-bus. If you need infra that isn't this BEP's subject, extract it into its own BEP and depend on it.
- **Layered, dependency-ordered plan.** Decompose into layers and sequence bottom-up so each step rests on something already built. Never presume a caller, trigger, store, config key, or service that does not yet exist — make each such prerequisite its own explicit step.
- **Ground every claim in the code.** Verify each referenced symbol, file, protocol method, config key, and schema against the actual repo (e.g. confirm `[exts.<ep>.<impl>]` keys resolve; confirm a method exists or is explicitly added). Assumed-but-absent APIs and wrong config keys are the most common defect.
- **Distinguish look-alikes; define interactions.** Avoid name collisions between similar concepts (e.g. a summarizer vs a writer). When several mechanisms overlap, state how they interact and at what point each applies.
- **Precise vocabulary; no overclaiming.** Use exact terms ("reliable with graceful drain" ≠ "crash-safe durable"). Every acceptance criterion must state its preconditions. Don't promise guarantees the design cannot keep.
- **Name breaking-change fallout.** When changing or removing a contract, enumerate affected call sites, tests, and external/third-party impact, and state the compatibility stance explicitly.
- **Honest, per-track readiness.** Declare cross-BEP dependencies up front and gate readiness on them; mark which tracks are implementable now vs blocked. Do not label a BEP "ready" when a dependency is still a stub.
- **Clean structure.** Consistent section numbering, an explicit Non-Goals section, and a revision history.

### Reviewing / updating

- When reviewing, first clarify scope, responsibility boundaries, source-of-truth decisions, and explicit non-goals. Resolve ambiguous ownership before implementation planning.
- Keep BEPs aligned with the intended end design, not transitional compatibility, unless compatibility is explicitly required.
- After discussion, update the BEP with concrete decisions, config/API shapes, lifecycle rules, and remaining open issues. Remove stale or contradictory text instead of leaving historical alternatives in place.
- Before implementing a BEP, make a short implementation plan and re-check it against the BEP so implementation agents do not infer behavior from outdated wording.


## Code Search

Use `uv run semble search` to find code by describing what it does or naming a symbol/identifier, instead of grep:

```bash
uv run semble search "authentication flow" ./my-project
uv run semble search "save_pretrained" ./my-project
uv run semble search "save model to disk" ./my-project --top-k 10
```

Use `uv run semble find-related` to discover code similar to a known location (pass `file_path` and `line` from a prior search result):

```bash
uv run semble find-related src/auth.py 42 ./my-project
```

`path` defaults to the current directory when omitted; git URLs are accepted.

## Workflow

1. Start with `uv run semble search` to find relevant chunks.
2. Inspect full files only when the returned chunk is not enough context.
3. Optionally use `uv run semble find-related` with a promising result's `file_path` and `line` to discover related implementations.
4. Use grep only when you need exhaustive literal matches or quick confirmation of an exact string.
