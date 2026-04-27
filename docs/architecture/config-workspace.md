# Config And Workspace

`bos.config.workspace.Workspace` is the configuration/bootstrap entry point.

It currently owns:

- locating the active `.bos` directory
- loading `config.toml`
- creating a configured `AgentHarness`
- bootstrapping platform extensions and registered agents

Initialization of a new workspace is handled separately by the explicit
`initialize_workspace(...)` helper. `Workspace` itself is read-oriented and does
not create fallback `.bos` directories as a side effect of config discovery.
It resolves config from either an ancestor `.bos/` or explicit `BOS_DIR`, and
raises when neither exists or when both sources disagree.

`Workspace.config` is the raw parsed manifest. Derived runtime-facing config is
resolved explicitly, rather than mutating the raw load in place.

## Why It Is Separate From `runner`

`Workspace` is about configuration and bootstrap.

`runner` is about runtime orchestration.

They are closely related, but they solve different problems:

- `Workspace` answers: "What is configured here?"
- `runner` answers: "How do we launch this configured system?"

## Current Weak Spot

The separation is valid, but not perfectly clean yet.

`runner.start()` still reads some raw nested config directly from `workspace.config`. The better direction is to move more of that raw config interpretation behind `Workspace` accessors or a small typed runtime config object.

That is an incremental improvement, not a structural emergency.

## External Agent Definitions

Agent definitions now have a bounded split mechanism:

- `.bos/config.toml` remains the only manifest
- `.bos/agents/` is auto-scanned by default
- override with `[platform] agent_dirs = ["agents", "../shared-agents", "~/.bos/agents"]`
- multiple directories are supported, scanned in list order
- relative paths are resolved against `.bos/`; absolute paths and `~` are supported
- supported external forms: `<dir>/<name>.toml` and `<dir>/<name>.md`
- Markdown agent frontmatter supplies agent settings; the Markdown body becomes `system_prompt`

Resolution rules:

- inline agents load first, in declaration order
- external agents load second, alphabetically within each directory, directories processed in list order
- exact-name duplicates are allowed and the later definition wins
- case-only collisions are errors
- if `name` is provided in the external file, it is used as-is
- if `name` is omitted, it is derived from the filename stem
