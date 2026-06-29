# Writing Tools

A **tool** is an async Python function the LLM can call during a turn.  Tools are registered at the `ep_tool` extension point using the `@ep_tool(...)` decorator.

---

## Minimal example

```python
# .bos/extensions/word_count.py
from bos.core import ep_tool

@ep_tool(
    name="WordCount",
    description="Count the words in a text.",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to count."},
        },
        "required": ["text"],
    },
)
async def word_count(text: str) -> str:
    return f"{len(text.split())} words"
```

Drop this file in `.bos/extensions/` (or list its module in `[platform].extensions`) and the tool is available to all agents.

---

## Full decorator anatomy

```python
@ep_tool(
    name="WordCount",            # (1) registered name — must be unique across all tools
    description="...",           # (2) short schema description shown to the model
    parameters={...},            # (3) JSON schema — REQUIRED
    usage="...",                 # (4) longer guidance surfaced in the system prompt
    parallel_safe=True,          # (5) may run concurrently with other parallel-safe tools
    result_serializer="auto",    # (6) auto | json | str
)
async def word_count(text: str) -> str: ...
```

### (1) `name`

The string the model uses when calling the tool, and the key used in `[…tools].enabled` / `[…tools].disabled`.  A duplicate name at registration time raises immediately.

### (2) `description`

A short sentence.  This goes directly into the OpenAI-style tool schema the model receives.

### (3) `parameters`

A JSON schema object describing the tool's inputs.  **This field is mandatory.**  The property names declared here must be a subset of the Python function's parameter names (unless the function accepts `**kwargs`).

```python
parameters={
    "type": "object",
    "properties": {
        "path":  {"type": "string"},
        "limit": {"type": "integer", "default": 500},
    },
    "required": ["path"],
}
```

### (4) `usage`

Longer guidance (a paragraph or more) that the agent surfaces in the system prompt to tell the model *when* and *how* to use this tool.  If omitted, `description` is used as a fallback.

### (5) `parallel_safe`

When `True`, the harness may execute this tool concurrently alongside other `parallel_safe` tools in the same LLM response.  Default is `False`.

### (6) `result_serializer`

Controls how the tool's return value is converted to the string the model sees:

| Value | Behaviour |
|-------|-----------|
| `"auto"` (default) | `json.dumps` for JSON-compatible types (`dict`, `list`, `int`, `float`, `bool`, `None`); `str()` for everything else |
| `"json"` | Always `json.dumps` |
| `"str"` | Always `str()` |

---

## Sync vs async

Async functions are preferred, but sync functions also work — `invoke` awaits either form.

**Blocking I/O** (file reads, subprocesses) should be offloaded to a thread so the event loop stays unblocked.  The built-in filesystem tools do this:

```python
import asyncio

@ep_tool(name="ReadConfig", description="Read a config file.", parameters={...})
async def read_config(path: str) -> str:
    return await asyncio.to_thread(_sync_read, path)   # keeps the loop free

def _sync_read(path: str) -> str:
    with open(path) as f:
        return f.read()
```

---

## `ToolContext` injection

Declare a parameter named `context` with type `ToolContext | None` and the harness injects a context object at call time.  The most common use is accessing `context.parent` to spawn nested agent turns.

```python
from bos.core.contract import ToolContext

@ep_tool(
    name="AskExpert",
    description="Ask a specialist agent.",
    parameters={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
)
async def ask_expert(question: str, context: ToolContext | None = None) -> str:
    if context is None:
        return "Error: no context available."
    # context.parent is a ParentTurn; pass it to a runner to nest the sub-turn
    ...
```

The `context` parameter is injected by the harness, not supplied by the model — so leave it out of `parameters`. The schema the model sees is exactly the `parameters` dict you declare (BOS does not strip anything), and validation only requires that every property in `parameters` map to a function argument, so an extra `context` argument is allowed precisely because it is absent from the schema.

---

## Per-tool config via `[exts.ep_tool.<Name>]`

Config in this table is deep-merged into the tool's defaults and passed as keyword arguments when the tool is called.  The function must accept those keyword arguments (or `**kwargs`).

```toml
# .bos/config.toml
[exts.ep_tool.GrepSearch]
extend_ignore = ["*.generated.py", "vendor/"]
```

```python
@ep_tool(
    name="GrepSearch",
    description="Search file contents.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)
async def grep_search(
    query: str,
    extend_ignore: list[str] | None = None,   # filled from config
) -> str: ...
```

This is exactly how `GrepSearch` and `GlobSearch` accept `replace_ignore` / `extend_ignore` / `remove_ignore` from config.

---

## Example: reading per-tool config

```python
from bos.core import ep_tool

@ep_tool(
    name="WordCount",
    description="Count words; optionally exclude stop-words.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
async def word_count(
    text: str,
    exclude_stopwords: bool = False,   # configurable via [exts.ep_tool.WordCount]
) -> str:
    words = text.split()
    if exclude_stopwords:
        stopwords = {"the", "a", "an", "is", "in", "of", "and"}
        words = [w for w in words if w.lower() not in stopwords]
    return f"{len(words)} words"
```

```toml
[exts.ep_tool.WordCount]
exclude_stopwords = true
```

---

## Enable / disable per agent

An agent sees only the tools explicitly included by its `enabled` list (or `"*"` for all) minus its `disabled` list.  You can also override the `usage` guidance per agent:

```toml
[agents.researcher.tools]
enabled = ["ReadFile", "GlobSearch", "GrepSearch", "WordCount"]

[agents.researcher.tools.usages]
WordCount = "Use this to estimate document size before deciding whether to summarise."
```

---

## Built-in filesystem tools

For reference, the built-in tools registered by `src/bos/extensions/tools/filesystem.py`:

| Name | Description |
|------|-------------|
| `ReadFile` | Read a text file with optional pagination (`line_offset`, `limit`) |
| `WriteFile` | Write full file content; refuses to overwrite unread files |
| `EditFile` | Surgical text replacement (`old_string` → `new_string`) |
| `GlobSearch` | Find files by glob pattern |
| `GrepSearch` | Search file contents with rg/grep (context-window-safe output) |

`GlobSearch` and `GrepSearch` accept `replace_ignore` / `extend_ignore` / `remove_ignore` via `[exts.ep_tool.<Name>]` to customise which directories are skipped.

---

## See also

- [Plugins](plugins.md) — bundle tools into a plugin with per-agent config and a shared `HarnessPlugin` instance
- [Packaging & entry points](entry-points.md) — ship tools in an installable package
