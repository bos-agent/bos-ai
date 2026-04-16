# Runner

`bos.runner.runner.start(workspace)` assembles and launches an in-process runtime.

## Responsibilities

- open the configured harness
- create the main agent
- create the actor address
- create configured channels
- optionally insert a `BroadcastChannel`
- run actor and channel tasks together
- publish channel endpoint info to `agent.state`

## What `runner` Should Know

`runner` should know:

- runtime topology rules
- actor/channel wiring
- task orchestration

`runner` should not become the place that:

- discovers workspaces
- parses TOML files directly in many places
- owns business logic for agents or tools

## Broadcast Model

When `main.broadcast_address` is configured:

- channels target the broadcast address
- the broadcast channel forwards inbound traffic to the actor
- outbound actor replies fan back out to member channels

In the current single-user design, broadcast also owns the shared canonical conversation thread across those member channels.
