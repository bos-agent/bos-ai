# Runner

`bos.runner.runner.start(workspace)` assembles and launches an in-process runtime.

## Responsibilities

- open the configured harness
- resolve configured actor topology
- create one agent actor per configured actor
- create configured channels
- run actor and channel tasks together
- publish actor and channel endpoint info to `agent.state`

## What `runner` Should Know

`runner` should know:

- runtime topology rules
- actor/channel wiring
- task orchestration

`runner` should not become the place that:

- discovers workspaces
- parses TOML files directly in many places
- owns business logic for agents or tools

## Actor Topology

The topology always has exactly one coordinator: the actor named `main` at
`agent@main`. When `main.agent` is configured, it defines that coordinator and
`[[main.actors]]` contains only workers. When `main.agent` is omitted,
`[[main.actors]]` must include exactly one actor named `main`. Channels default
to the coordinator but may target any configured actor address.

The runner owns topology assembly only:

- resolve configured actors
- create the matching `AgentActor` instances
- bind each actor to its mailbox address
- start actors and channels in one task group

It does not own task business logic. Durable task records, chat-task bindings,
and recovery semantics live in the runtime task ledger.

## Chat Cursor Model

Channels normally target the coordinator actor directly. Cross-client
continuity is handled by server-side chat state rather than channel fanout:

- `chat_id` is the durable message-store thread and actor execution slot
- `client_id` maps to the client's current chat cursor
- aliases can point to chat ids for easier resume from another client

The runtime assumes one active client at a time for a thread; it does not fan
actor replies out to multiple channels in real time.

Task-owned chats are intentionally separate from ordinary client cursor state.
The task ledger owns bindings from `task_id` to one or more task chat ids, and
ordinary client resume must not adopt `task:` chat ids by accident.
