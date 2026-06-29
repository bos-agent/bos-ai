# Packaging & Entry Points

When you want to distribute extensions as an installable Python package, BOS reads three entry-point groups at startup.  Declaring them means users install your package and extensions are available immediately — no config edits required.

---

## The three entry-point groups

| Group | What it registers | Value must be |
|-------|------------------|---------------|
| `bos.exts` | Extensions (tools, channels, plugins, providers, …) | A Python module; its import side-effects run the `@ep_*` decorators |
| `bos.skills` | Skill directories contributed by a package | A Python package; its directory is scanned for skill subdirs |
| `boscli.commands` | Extra CLI commands added under `boscli` | A `click.Command` or `click.Group` |

---

## Full `pyproject.toml` example

```toml
[project]
name = "bos-weather-tools"
version = "0.1.0"
dependencies = ["bos-ai"]

[project.entry-points."bos.exts"]
# Importing this module fires the @ep_tool / @ep_channel / @ep_plugin decorators.
weather = "bos_weather_tools.extensions"

[project.entry-points."bos.skills"]
# The directory containing this package holds skill subdirs (each with SKILL.md).
weather = "bos_weather_tools.skills"

[project.entry-points."boscli.commands"]
# A click.Command or click.Group added as a subcommand of boscli.
weather = "bos_weather_tools.cli:commands"
```

```
bos_weather_tools/
├── extensions.py          # @ep_tool(name="GetWeather", ...) etc.
├── skills/
│   └── weather-report/
│       └── SKILL.md       # name: weather-report / description: ...
└── cli.py                 # commands = click.group()(...)
```

---

## Loading mechanics

### `bos.exts` — extensions

When `bos.exts` is imported (the first entry in the default `[platform].extensions` list), it calls `entry_points(group="bos.exts")` and loads each entry point module via `ep.load()`.  A module that fails to import is logged and skipped — it does not abort startup.

The loaded module's import side-effects do all the work: the `@ep_tool(...)`, `@ep_channel(...)`, and other decorators fire and register the implementations.

```python
# bos_weather_tools/extensions.py
from bos.core import ep_tool

@ep_tool(
    name="GetWeather",
    description="Get current weather for a location.",
    parameters={
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    },
)
async def get_weather(location: str) -> str:
    # call your weather API
    return f"Sunny, 22 °C in {location}"
```

### `bos.skills` — skill directories

`SkillsPlugin`'s filesystem loader calls `_contributed_skill_dirs()`, which loads each `bos.skills` entry point and collects the package directory.  These contributed directories slot in at the `__builtin__` position in `skill_dirs`, before any workspace-local `skills/` directory.

This means workspace skills (in `.bos/skills/`) **override** package-contributed skills on name clash — local always wins.

Each skill is a subdirectory containing `SKILL.md`:

```
bos_weather_tools/skills/
└── weather-report/
    └── SKILL.md
```

```markdown
---
name: weather-report
description: Step-by-step guide for producing a weather report. Load before any weather task.
---
## Producing a weather report
1. Call GetWeather for each location.
...
```

### `boscli.commands` — CLI commands

`boscli`'s lazy group loader (`_LazyGroup`) reads each `boscli.commands` entry point and attaches it as a subcommand.  The value must be a `click.Command` or `click.Group`.

- **Group-vs-group**: if a plugin group and the built-in group share the same name, their subcommands are merged (built-in wins on inner collisions).
- **Collision with a built-in non-group command**: the plugin command is skipped with a warning.

```python
# bos_weather_tools/cli.py
import click

@click.group()
def commands() -> None:
    """Weather tools CLI commands."""

@commands.command()
@click.argument("location")
def forecast(location: str) -> None:
    """Print a quick weather forecast."""
    click.echo(f"Fetching forecast for {location}…")
```

After installation: `boscli weather forecast London`.

---

## Zero-packaging alternative

You do not need a package to load extensions.  Two lighter approaches work for project-local code:

### Drop files in `./extensions`

Place `.py` files in the `extensions/` directory inside your `bos_dir` (`.bos/extensions/`).  BOS scans and imports them automatically (this path is in the default `[platform].extensions` list):

```
my-agent/.bos/extensions/
└── my_tools.py    # @ep_tool decorators here are auto-discovered
```

### List the module in `[platform].extensions`

If your extension is an importable module in your project venv, add it to the list:

```toml
[platform]
extensions = ["bos.exts", "./extensions", "my_package.bos_extensions"]
```

Then run via the project venv so the module is importable:

```bash
uv run boscli gateway start
uv run boscli ask "What is the weather in Paris?"
```

---

## Running via the project venv

Whether you use entry points or the module-name approach, run BOS through the venv that has your package installed:

```bash
# With uv (recommended)
uv run boscli ask "..."

# Or activate the venv first
source .venv/bin/activate
boscli ask "..."
```

---

## See also

- [Tools](tools.md) — `@ep_tool` decorator details
- [Plugins](plugins.md) — `@ep_plugin` decorator details
- [Channels](channels.md) — `@ep_channel` decorator details
- Tutorial: packaging an extension — `../tutorials/package-extension.md`
