# Repository Guidance

This `AGENTS.md` applies to the entire repository.

## Toolchain

- Use `uv run ...` for Python entrypoints, tests, and linting.
- The project targets Python `>=3.13` via [pyproject.toml](/Users/jerry/Repo/workbench/bos-ai/pyproject.toml).
- Do not assume the system `python3` is suitable. On this machine it is older and can fail on `tomllib`.
- When you need the CLI locally, prefer `uv run boscli ...`.

## Common Commands

- Run the full test suite: `uv run pytest -q`
- Run a focused test file: `uv run pytest -q tests/test_workspace_runtime.py`
- Run lint: `uv run ruff check src tests`
- Run the CLI help: `uv run boscli --help`

## Repo Layout

- `src/bos/config`: workspace discovery and TOML-backed config loading
- `src/bos/core`: runtime primitives, contracts, harness, agent loop
- `src/bos/extensions`: channels, tools, stores, and other extension implementations
- `src/bos/runner`: runtime assembly and process/container lifecycle
- `src/bos/protocol`: shared message contracts
- `tests`: pytest coverage for config, runner, channels, harness, and CLI behavior

## Working Notes

- Keep package boundaries explicit. If you change config loading behavior, check whether `README.md` and `docs/architecture/*.md` also need updates.
- Prefer small, reversible diffs and extend existing patterns before adding new abstractions.
- If a change touches config/runtime behavior, add or update targeted pytest coverage in `tests/`.
- `uv run ruff check src tests` is a useful signal, but the repo may already contain unrelated lint findings. Do not assume a lint failure came from your change without checking the reported files.
- Pull request titles must follow semantic/conventional format, for example `feat(config): ...` or `fix(runner): ...`, because GitHub Actions validate the PR title.

## BEP Process

- BOS Enhancement Proposals live in `docs/BEP/` and capture accepted design direction before broad architectural changes.
- When reviewing a BEP, first clarify scope, responsibility boundaries, source-of-truth decisions, and explicit non-goals. Resolve ambiguous ownership before implementation planning.
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
