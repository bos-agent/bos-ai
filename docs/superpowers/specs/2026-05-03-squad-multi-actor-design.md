# Squad: multi-actor runtime

## Summary

Add a `src/bos/squad/` extension module that runs multiple actors simultaneously.
Users address specific actors via `@agentname` in message text; the frontend parses this
and sends `target_actor` metadata. A new `ActorRegistry` resolves actor names to
mailbox addresses. Channels use the registry to route messages to the correct actor.

Zero or minimal core changes. The extension inherits from core primitives where it needs
different behavior.

## Motivation

Today `bos` runs a single `AgentActor` at `agent@main`. Channels always target
`agent@main`. Users cannot route work to different agents from the same channel.

## Configuration

### `[main.actors]` section

Actors live under `[main]` because they are a runtime concern, alongside channels:

```toml
[main.actors.researcher]
agent = "researcher"

[main.actors.reviewer]
agent = "reviewer"

[main.actors.main]
agent = "main"
```

- Each key under `[main.actors]` is the actor's routing name (what users type after `@`).
- `agent` references a named agent defined in `platform.agents` or `agent_dirs`.
- The actor's mailbox address is `agent@<routing-name>` (e.g. `agent@researcher`).
- The actor with routing name `"main"` is the default fallback when no `target_actor`
  is specified. If omitted, the first registered actor is the default.
- If no `[main.actors]` section exists, behavior is unchanged — a single `main` actor
  runs using `main.agent` for backwards compatibility.

### Channel config

Channels remain under `[[main.channels]]`. `target_address` now accepts any `agent@*`
address and serves as the fallback when no `target_actor` metadata is present:

```toml
[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
host = "127.0.0.1"
port = 5920
```

## Architecture

### Package: `src/bos/squad/`

```
src/bos/squad/
  __init__.py       # public API surface
  registry.py       # ActorRegistry
  actor.py          # SquadActor(AgentActor) + SquadAgent(ReactAgent)
  runner.py         # start_squad() — replacement entry point
```

### ActorRegistry (`registry.py`)

Lightweight address resolver. Not a forwarding mailbox — no extra message hop.

```python
@dataclass
class ActorRecord:
    name: str
    address: str      # e.g. "agent@researcher"
    mailbox: MailBox
    is_default: bool

class ActorRegistry:
    def register(self, name: str, mailbox: MailBox, *, is_default: bool = False) -> None
    def resolve(self, target_actor: str | None) -> str           # → address
    def resolve_mailbox(self, target_actor: str | None) -> MailBox
    def list_actors(self) -> dict[str, ActorRecord]              # for diagnostics
```

### SquadActor (`actor.py`)

Inherits from `AgentActor`. Overrides:

- `_merge_pending_messages` — annotates merged messages with `target_actor`
  attribution so the agent sees which actor each message was addressed to.

Extension point for actor-to-actor messaging later.

### Runner (`runner.py`)

`start_squad(workspace: Workspace) -> None`:

1. Reads `[main.actors]` from `workspace.config`
2. Uses `workspace.harness()` as context manager (same as today)
3. Creates one `SquadActor` per actor entry, each with `MailBox` at `agent@<name>`
4. Builds `ActorRegistry`, registers all actors
5. Creates channels from `[[main.channels]]`, passing the registry
6. Launches all actors + channels in an `asyncio.TaskGroup`

## End-to-end flow

```
1. User types "@researcher find papers on X" in TUI
2. TUI parses "@researcher", strips it, builds:
   content = "find papers on X"
   metadata.target_actor = "researcher"
3. TUI sends via WebSocket to HttpChannel
4. HttpChannel reads metadata.target_actor
   → registry.resolve("researcher")
   → "agent@researcher"
5. HttpChannel calls mailbox.send("agent@researcher", content, metadata=...)
6. SquadActor at agent@researcher polls its mailbox, picks up the message
7. SquadActor drives SquadAgent.ask():
   a. Loads full chat history from MessageStore
   b. Filters tool-call intermediate noise
   c. Annotates user messages with target_actor attribution
   d. Builds system prompt, calls LLM
8. Response flows back via event_sink → channel → TUI
```

## Chat history with attribution

When actor `researcher` processes a message in chat `abc123`:

1. **Filter** — drop `tool_use`/`tool_result` blocks and `role: "tool"` messages.
   Only final assistant responses and user/attributed messages remain.

2. **Annotate** — each user message is labeled with which actor it was addressed to:

```
[user → @main]:       What do you think of this code?
[assistant]:          It looks solid overall.
[user → @researcher]: Find related papers on this approach
[assistant]:          Here are 3 papers...
[user → @reviewer]:   Review the approach against those papers
[assistant]:          The approach aligns with paper #2 but...
```

Implemented as `SquadAgent(ReactAgent)` override — filters and annotates when
building the system prompt. Core `ReactAgent` and `MessageStore` are unchanged.

## Core changes

Exactly one core file change — `workspace.py:_validate_channel_topology`:

```python
# Relax: accept any agent@* address, not just agent@main
if not channel.target_address.startswith("agent@"):
    raise ValueError(...)
```

All other logic lives in `src/bos/squad/`.

## Error handling

| Scenario | Behavior |
|---|---|
| No `[main.actors]` section | Backwards compatible: single `main` actor |
| `target_actor` not in registry | Falls back to default actor |
| `target_actor` references undefined agent name | Raise at startup |
| Channel has no registry reference | Uses `target_address` from config directly |

## Actor-to-actor (future)

Not implemented now, but the design supports it:

- Actors get a reference to the `ActorRegistry`
- An actor calls `registry.resolve_mailbox("reviewer")` and sends a message
- The registry pattern works identically for channels and actors

## Tests

```
tests/squad/
  test_registry.py      # resolve, fallback, missing, list_actors
  test_actor.py         # SquadActor._merge_pending_messages attribution
  test_runner.py        # multi-actor startup, registry wiring, backwards compat
  test_history.py        # tool filter + attribution annotation
```

One core test update: channel validation test for the relaxed topology check.
