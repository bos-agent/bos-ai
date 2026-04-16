# Config And Workspace

`bos.config.workspace.Workspace` is the configuration/bootstrap entry point.

It currently owns:

- locating the active `.bos` directory
- loading `config.toml`
- initializing a workspace with the template config
- creating a configured `AgentHarness`
- bootstrapping platform extensions and registered agents

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
