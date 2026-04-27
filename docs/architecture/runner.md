# Runner

`bos.runner.runner.start(workspace)` assembles and launches an in-process runtime.

## Responsibilities

- open the configured harness
- create the main agent
- create the actor address
- create configured channels
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

## Chat Cursor Model

Channels target the main actor directly. Cross-client continuity is handled by
server-side chat state rather than channel fanout:

- `chat_id` is the durable message-store thread and actor execution slot
- `client_id` maps to the client's current chat cursor
- aliases can point to chat ids for easier resume from another client

The runtime assumes one active client at a time for a thread; it does not fan
actor replies out to multiple channels in real time.
