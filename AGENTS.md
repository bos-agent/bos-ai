# Repository Guidance

This `AGENTS.md` applies to the entire repository.

## Toolchain

- Use `uv run ...` for Python entrypoints, tests, and linting.
- The project targets Python `>=3.13` via [pyproject.toml](/Users/jerry/Repo/workbench/bos-ai/pyproject.toml).
- Do not assume the system `python3` is suitable. On this machine it is older and can fail on `tomllib`.
- When you need the CLI locally, prefer `uv run bos ...`.

## Common Commands

- Run the full test suite: `uv run pytest -q`
- Run a focused test file: `uv run pytest -q tests/test_workspace_runtime.py`
- Run lint: `uv run ruff check src tests`
- Run the CLI help: `uv run bos --help`

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
