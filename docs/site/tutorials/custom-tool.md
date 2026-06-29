# Tutorial 3 — Add a custom tool

Tools are the functions your agent can call during a turn. BOS discovers them
automatically when you drop a `.py` file into `.bos/extensions/` and decorate
a function with `@ep_tool`. No restart of the process is needed during
development — just restart the gateway after adding a new file.

This tutorial assumes you have the `my-agent/` project from
[Tutorial 2](create-a-project.md).

---

## The generated starter tool

`boscli init` already placed an example tool in `.bos/extensions/project_tools.py`:

```python
from bos.core import ep_tool

@ep_tool(
    name="WordCount",
    description="Count the words in a text.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to count."}},
        "required": ["text"],
    },
)
async def word_count(text: str) -> str:
    return f"{len(text.split())} words"
```

Ask the agent to use it:

```bash
boscli ask "How many words are in 'the quick brown fox'? Use the WordCount tool." \
  --model openai/gpt-4o
```

---

## Anatomy of `@ep_tool`

Every parameter of the decorator is described below. The only mandatory one is
`parameters`.

```python
@ep_tool(
    name="ToolName",          # (str) identifier — must be unique across all registered tools
    description="...",        # (str) short description shown in the OpenAI tool schema
    parameters={              # (dict) JSON Schema for the tool's arguments — REQUIRED
        "type": "object",
        "properties": {
            "arg": {"type": "string", "description": "What this arg means."},
        },
        "required": ["arg"],
    },
    usage="...",              # (str, optional) longer guidance surfaced in the system prompt
    parallel_safe=True,       # (bool, optional) allow concurrent calls with other parallel-safe tools
    result_serializer="auto", # "auto" | "json" | "str"
)
async def my_tool(arg: str) -> str:
    ...
```

### Key points

- **`name`** — the identifier the LLM uses when it decides to call the tool.
  Duplicate names across the registry crash startup, so be specific.
- **`description`** — keep it short; it appears in the schema the model sees.
- **`parameters`** — a JSON Schema `object` whose property names must match the
  function's parameters (unless the function accepts `**kwargs`).
- **`usage`** — longer, richer guidance (when to use the tool, edge cases,
  examples). Shown in the agent's system prompt. Use it to reduce misuse.
- **`result_serializer`** — controls how the return value becomes the string
  the model sees: `"auto"` uses `json.dumps` for dict/list/int/float and
  `str()` otherwise; `"json"` always serialises; `"str"` always calls `str()`.
- **Async preferred** — blocking/CPU work should go through
  `asyncio.to_thread`. Sync functions also work; BOS awaits as needed.

---

## Add a second, richer tool

Open `.bos/extensions/project_tools.py` and append:

```python
import asyncio
import pathlib

@ep_tool(
    name="CountLines",
    description="Count the lines in a file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or workspace-relative path to the file.",
            },
        },
        "required": ["path"],
    },
    usage=(
        "Use CountLines when the user asks how long a file is, wants to know "
        "its line count, or needs a quick size estimate before reading it. "
        "Pass an absolute path or a path relative to the workspace root."
    ),
    parallel_safe=True,
)
async def count_lines(path: str) -> str:
    p = pathlib.Path(path)
    if not p.exists():
        return f"File not found: {path}"
    lines = await asyncio.to_thread(lambda: len(p.read_text(encoding="utf-8").splitlines()))
    return f"{lines} lines in {p.name}"
```

!!! note "Blocking I/O"
    `pathlib.Path.read_text` is blocking. Wrapping it in `asyncio.to_thread`
    keeps the event loop responsive — important when the agent is running
    multiple tools in parallel.

---

## Enable and disable tools per agent

By default `enabled = ["*"]` gives every agent access to all registered tools.
You can narrow that per agent in `config.toml`:

```toml
[agents.main.tools]
enabled = ["WordCount", "CountLines", "ReadFile"]
# disabled = ["WriteFile"]  # alternative: start from "*" and exclude
```

You can also override the `usage` text shown in the system prompt without
touching the Python source:

```toml
[agents.main.tools]
enabled = ["*"]

[agents.main.tools.usages]
WordCount = "Only use WordCount when explicitly asked to count words — not for general length questions."
```

---

## Per-tool runtime configuration

Any key under `[exts.ep_tool.<Name>]` is merged into the tool's defaults and
passed as keyword arguments when BOS calls it. This lets you externalise
constants without touching the source:

```toml
# .bos/config.toml
[exts.ep_tool.CountLines]
max_size_mb = 10
```

Then accept it in the function:

```python
@ep_tool(
    name="CountLines",
    ...
)
async def count_lines(path: str, max_size_mb: int = 5) -> str:
    p = pathlib.Path(path)
    if p.stat().st_size > max_size_mb * 1_000_000:
        return f"File too large (limit {max_size_mb} MB)."
    ...
```

---

## Restart and verify

After editing `project_tools.py`, restart the gateway so it picks up the
changes:

```bash
boscli gateway stop
boscli gateway start
```

Then ask the agent to use the new tool:

```bash
boscli ask "How many lines does .bos/config.toml have? Use CountLines." \
  --model openai/gpt-4o
```

---

## What you learned

- Drop a `.py` file into `.bos/extensions/` and decorate functions with
  `@ep_tool` to register custom tools — no config edit required.
- `name`, `description`, and `parameters` (JSON Schema) are the core fields;
  `usage` adds richer system-prompt guidance.
- Enable or disable tools per agent with `[agents.<name>.tools].enabled /
  .disabled`; override guidance with `.usages`.
- Configure tool behaviour externally via `[exts.ep_tool.<Name>]`.
- Wrap blocking I/O in `asyncio.to_thread` to keep the event loop free.

**Next:** [Delegate to sub-agents](subagents.md) — create specialist agents
and let the main agent delegate tasks to them.
