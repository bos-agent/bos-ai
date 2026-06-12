---
name: python
description: Run Python with uv and PEP 723 inline-metadata scripts. Load this skill before writing or executing ANY Python code — never invoke bare `python` or `pip` through shell tools.
---

# Running Python with uv

All Python execution goes through `uv`, which resolves each script's declared
dependencies into an isolated, cached environment per run. Never call bare
`python`, `python -c`, `pip`, or `pip install` — they use (and pollute) the
host process environment.

## 1. Check uv availability (once per session)

```bash
uv --version
```

If uv is not installed, do not install it silently. Tell the user it is
required and suggest the official installer:

- Linux/macOS: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Proceed with installation only if the user agrees or has standing instructions
allowing it.

## 2. Declare dependencies inline (PEP 723)

Every script starts with a PEP 723 metadata block, even when it has no
dependencies:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pandas"]
# ///
import pandas as pd

result = ...
print(result)  # only stdout/stderr comes back — print what you need to see
```

## 3. Run it

Short one-shot snippets — pipe inline through stdin:

```bash
uv run - <<'EOF'
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
print(40 + 2)
EOF
```

Longer or iterative work (more than ~20 lines, or a script you will refine
and re-run) — write a file in the workspace and run it:

```bash
uv run analysis.py
```

## Rules

- There is no persistent session: every run is a fresh process. Persist
  intermediate results to files (JSON/CSV/Parquet) instead of relying on
  variables from earlier runs.
- Keep scripts self-contained: take inputs from explicit file paths or argv,
  write outputs to files or stdout.
- The first run with new dependencies downloads packages; later runs hit
  uv's cache and start fast.
- This is process isolation, not a security sandbox: scripts run with your
  user's privileges. Apply the same care as with any shell command.
