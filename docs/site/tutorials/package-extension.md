# Tutorial 6 — Package & share extensions

So far your tools live in `.bos/extensions/` — local to one project. When you
want to reuse them across multiple projects, or share them with others, the
right move is to package them as a standard Python package with BOS entry
points. Once installed, the package registers its tools, skills, and CLI
commands automatically — no `extensions` config edit required.

---

## Two approaches side by side

| | Workspace extensions | Package extensions |
|--|---------------------|--------------------|
| Where code lives | `.bos/extensions/*.py` | `src/<pkg>/tools.py` |
| Discovery | `"./extensions"` in `[platform].extensions` | `bos.exts` entry point in `pyproject.toml` |
| Sharing | Copy files manually | `pip install <pkg>` |
| Works without install | Yes | No (must be on `sys.path`) |
| Recommended for | Project-local tools | Reusable / shareable tools |

---

## Scaffold a package project

`boscli init` supports a `package` archetype that creates the full `src/`
layout for you. In a fresh directory:

```bash
mkdir my-tools && cd my-tools
boscli init
# When asked for archetype, choose: package
```

The scaffolder creates:

```
my-tools/
├── pyproject.toml
├── .bos/
│   ├── config.toml
│   └── .env
└── src/
    └── my_tools/
        ├── __init__.py
        ├── tools.py          ← put your @ep_tool functions here
        └── skills/           ← put skill dirs here (each with SKILL.md)
            └── __init__.py
```

---

## The `pyproject.toml` entry points

The key section is `[project.entry-points]`:

```toml
[project]
name = "my-tools"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["bos-ai"]

# 1) Extensions — importing this module runs its @ep_tool / @ep_channel / …
#    decorators, registering them in any BOS project that has this package installed.
[project.entry-points."bos.exts"]
my_tools = "my_tools.tools"

# 2) Skills — the target is a package whose directory holds skill subdirs
#    (each one a directory containing a SKILL.md file).
[project.entry-points."bos.skills"]
my_tools = "my_tools.skills"

# 3) CLI commands (optional) — a click.Command or click.Group added under boscli.
# [project.entry-points."boscli.commands"]
# my_tools = "my_tools.cli:commands"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/my_tools"]
```

### What each entry point does

**`bos.exts`** — When `bos.exts` loads (the default value of
`[platform].extensions`), it iterates the `bos.exts` entry-point group and
imports each target module. The import side effects — the `@ep_tool`,
`@ep_channel`, `@ep_plugin`, … decorators — register the extensions with the
BOS runtime. This happens in every project that has your package installed and
lists `"bos.exts"` in `extensions`.

**`bos.skills`** — BOS resolves the `"__builtin__"` sentinel in `skill_dirs`
by collecting package directories from the `bos.skills` entry-point group. Your
package's `skills/` directory is added to that list, so its skills appear
alongside built-ins (and can be overridden by the workspace's own `skills/`
directory).

**`boscli.commands`** (optional) — Your `click.Command` or `click.Group` is
merged under the `boscli` root. If it is a group with the same name as an
existing group, its subcommands are merged in (built-in wins on collision).

---

## Add tools to `tools.py`

Edit `src/my_tools/tools.py`:

```python
"""BOS extensions registered by this package.

Importing this module (via the bos.exts entry point) registers all decorators
below. Add @ep_tool, @ep_channel, @ep_provider, or @ep_plugin as needed.
"""

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

The module name (`my_tools.tools`) matches the entry-point target, so BOS
imports it at startup and the decorator fires.

---

## Install and run

Install the package in editable mode into your development environment:

```bash
uv pip install -e .
# or: pip install -e .
```

!!! warning "Always run through the project venv"
    The package must be importable when BOS starts. Use `uv run boscli ...`
    (which activates the project's venv) rather than a system `boscli`:

    ```bash
    uv run boscli gateway start
    uv run boscli tui
    ```

    If you use a system-wide `boscli` that lives outside the venv, it will not
    see your package and the tools will not register.

Verify the tool is visible:

```bash
uv run boscli ask "Count the words in 'hello world'. Use WordCount." \
  --model openai/gpt-4o
```

---

## The `config.toml` for a package project

The scaffolder generates a slightly different config for the package archetype:

```toml
[platform]
envfile = ".env"
extensions = ["bos.exts"]   # no "./extensions" — discovery is via the entry point
agent_dirs = ["./agents"]
```

Because the package self-registers via `bos.exts`, there is no need to list
`"./extensions"` or the module name explicitly.

### Zero-packaging alternative

If you want to use the module without publishing a package — for example,
during development in a monorepo — list it directly in `extensions`:

```toml
[platform]
extensions = ["bos.exts", "my_tools"]
```

As long as `my_tools` is importable (i.e. in `sys.path`), BOS will import it
and the decorators will fire. This removes the entry-point indirection, at
the cost of requiring a manual config edit in each consuming project.

---

## Shipping skills with your package

Create a skill directory under `src/my_tools/skills/`:

```
src/my_tools/skills/
└── word-analysis/
    └── SKILL.md
```

```markdown
---
name: word-analysis
description: Procedures for analysing word frequency and distribution in text.
---

## When to use this skill

Load this skill before any task that involves counting, ranking, or comparing
word frequencies in a body of text.

## Steps

1. Use WordCount to get the total word count.
2. ...
```

The `bos.skills` entry point points BOS at `src/my_tools/skills/`. The
`word-analysis/` directory becomes a skill named `word-analysis`, available to
any project that installs the package. Workspace skills (under `.bos/skills/`)
override package skills on name collision.

---

## What you learned

- The `package` archetype (`boscli init` → package) creates an installable
  `src/<pkg>/` layout with `pyproject.toml` entry points.
- `[project.entry-points."bos.exts"]` makes tools self-register when the
  package is installed — no `extensions` config edit needed in consuming
  projects.
- `[project.entry-points."bos.skills"]` ships skills alongside the package.
- `[project.entry-points."boscli.commands"]` (optional) extends the `boscli`
  CLI with your own commands.
- Always run BOS through the project venv (`uv run boscli ...`) so the package
  is importable.
- As an alternative to entry points, list the module in `extensions` directly.

---

You have completed the tutorial track. From here:

- **[Configuration reference](../configuration/index.md)** — every config key
  explained in depth.
- **[Extending BOS — Tools](../extending/tools.md)** — full tool authoring
  guide including `ToolContext`, result serialisers, and parallel execution.
- **[Concepts — Runtime](../concepts/runtime.md)** — actors, mailboxes,
  channels, and the gateway process in detail.
