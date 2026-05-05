# Squad: Multi-Actor Runtime

Status: **implemented** — core module complete, merged in PR #13.

---

## Core Insight

Today `bos` runs a single `AgentActor` at `agent@main`. All channels target `agent@main` and users cannot route work to different agents from the same channel.

The Squad extension adds concurrent multi-actor execution with `@agentname` mention-based routing. A new `ActorRegistry` parses `@mentions` from raw message content, resolves actor names to mailbox addresses, and sets routing metadata. Channels delegate routing to the registry — no frontend changes needed.

The same `@mention` parsing serves actor-to-actor relay in the future.

## Package: `src/bos/squad/`

```
src/bos/squad/
  __init__.py       # public API surface
  registry.py       # ActorRegistry — @mention parsing + address resolution
  actor.py          # SquadActor(AgentActor) + SquadAgent(ReactAgent)
  runner.py         # start_squad() — replacement entry point
```

Zero or minimal core changes. The extension inherits from core primitives where it needs different behavior.

## ActorRegistry (`registry.py`)

Owns @mention parsing and actor address resolution. Not a forwarding mailbox — no extra message hop.

```python
@dataclass
class ActorRecord:
    name: str
    address: str      # e.g. "agent@researcher"
    mailbox: MailBox
    is_default: bool

class ActorRegistry:
    def register(self, name: str, mailbox: MailBox, *, is_default: bool = False) -> None
    def resolve_address(self, target_actor: str | None) -> str
    def resolve_mailbox(self, target_actor: str | None) -> MailBox
    def list_actors(self) -> dict[str, ActorRecord]
    def route(self, content: str, metadata: dict | None = None) -> RouteResult:
        """Parse @agentname from content, return routing target and cleaned content.
        
        - Extracts leading @agentname (e.g. "@researcher do X" → "do X" + target="researcher")
        - Falls back to metadata.target_actor if no @mention in content
        - Falls back to the default actor if neither is present
        - Unknown @mentions are treated as literal text (fallback to default)
        """
```

`route()` is the single place `@mention` syntax is parsed. Every incoming message passes through this method.

### @mention syntax

Uses regex `@([\w][\w-]*)\s+` to extract leading mentions. The mention must be at the start of the message. Mentions mid-content are treated as literal text. Hyphenated names are supported (`@code-reviewer`).

## SquadAgent (`actor.py`)

`SquadAgent(ReactAgent)` overrides `_get_chat_history` to filter tool-call noise from shared chat history:

1. Drops `role: "tool"` messages
2. Strips `tool_calls` from assistant messages that also have content
3. Drops assistant messages that have only tool_calls (no content)

This keeps the shared history readable when multiple actors participate in the same chat.

## SquadActor (`actor.py`)

Composition wrapper around `AgentActor` using `__getattr__` delegation. Overrides `_merge_pending_messages` to add `[user → @agentname]:` attribution so each actor sees which actor a message was addressed to.

Example history view for the `researcher` actor:
```
[user → @main]:       What do you think of this code?
[assistant]:          It looks solid overall.
[user → @researcher]: Find related papers on this approach
[assistant]:          Here are 3 papers...
```

## Runner (`runner.py`)

`start_squad(workspace: Workspace) -> None`:

1. Reads `[main.actors]` from `workspace.config`
2. Uses `workspace.harness()` as context manager
3. Creates one `SquadActor` per actor entry, each with `MailBox` at `agent@<name>`
4. Builds `ActorRegistry`, registers all actors (first `"main"` actor is default)
5. Creates channels from `[[main.channels]]`, passing the registry
6. Writes channel endpoint info to `agent.state` for TUI discovery
7. Launches all actors + channels in an `asyncio.TaskGroup`

### Backwards compatibility

When no `[main.actors]` section is configured, falls back to a single `AgentActor` with `ReactAgent` — identical behavior to the old single-actor runner.

## Configuration

### `[main.actors]` section

Actors live under `[main]` because they are a runtime concern, alongside channels:

```toml
[main.actors.main]
agent = "main"
tools = ["AskSubAgent"]
subagents = ["researcher", "reviewer"]

[main.actors.researcher]
agent = "researcher"
description = "Research assistant for finding and summarizing information."
tools = ["WebSearch", "ReadFile"]
skills = ["brainstorming"]

[main.actors.reviewer]
agent = "reviewer"
description = "Code reviewer for quality and correctness checks."
```

- Each key under `[main.actors]` is the actor's routing name (what users type after `@`).
- `agent` references a named agent defined in `platform.agents` or `agent_dirs`.
- The actor's mailbox address is `agent@<routing-name>` (e.g. `agent@researcher`).
- The actor with routing name `"main"` is the default fallback when no `target_actor` is specified.
- If no `[main.actors]` section exists, behavior is unchanged — a single `main` actor runs.

### Channel config

Channels remain under `[[main.channels]]`. `target_address` now accepts any `agent@*` address and serves as the fallback when no `target_actor` metadata is present:

```toml
[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
host = "127.0.0.1"
port = 5920
```

## End-to-end flow

```
1. User types "@researcher find papers on X" in TUI
2. TUI sends raw content via WebSocket to HttpChannel (no parsing)
3. HttpChannel hands envelope to ActorRegistry.route()
4. route() parses "@researcher", strips it:
   content  = "find papers on X"
   target   = "researcher"
   → registry.resolve_address("researcher") → "agent@researcher"
5. HttpChannel calls mailbox.send("agent@researcher", content, metadata=...)
6. SquadActor at agent@researcher polls its mailbox, picks up the message
7. SquadActor drives SquadAgent.ask():
   a. Loads full chat history from MessageStore
   b. Filters tool-call intermediate noise
   c. Annotates user messages with target_actor attribution
   d. Builds system prompt, calls LLM
8. Response flows back via event_sink → channel → TUI
```

## Core changes

Exactly one core file change — `workspace.py:_validate_channel_topology`:

```python
# Before:
if channel.target_address.startswith("agent@"):
    if channel.target_address != actor_address:
        raise ValueError(...)
    continue

# After:
if channel.target_address.startswith("agent@"):
    continue
```

All other logic lives in `src/bos/squad/`.

## HttpChannel integration

HttpChannel gets an optional `actor_registry` parameter. Both `_send_handler` (REST) and `_ws_handler` (WebSocket) check for a registry and call `registry.route()` before sending to the mailbox:

```python
recipient = env.recipient
content = env.content
if registry is not None:
    route_result = registry.route(
        str(content) if isinstance(content, str) else content,
        metadata=metadata,
    )
    recipient = route_result.target_address
    content = route_result.content
```

## Error handling

| Scenario | Behavior |
|---|---|
| No `[main.actors]` section | Backwards compatible: single `main` actor |
| `@mention` not in registry | Treated as literal text, falls back to default actor |
| No `target_actor` and no default | `KeyError` raised |
| Channel has no registry reference | Uses `target_address` from config directly |
| Agent spec has extra keys (e.g. `description`) | Filtered via `_apply` (signature inspection) |

## Actor-to-actor (future)

Not implemented now, but the design supports three relay forms:

**1. Synchronous delegation** — actor calls another actor like a tool, blocks, gets result. Mirrors `AskSubAgent` but routed through `registry.resolve_mailbox()`.

**2. Async fire-and-forget** — actor sends to peer mailbox, continues without waiting. Peer response goes back to the channel independently.

**3. Mention-based relay** — an actor's response includes `@reviewer do X`. The same `ActorRegistry.route()` that parses user messages also parses actor responses on the return path.

All three use `registry.resolve_mailbox(name)` under the hood. The registry pattern works identically whether the sender is a channel or an actor.

## Tests

```
tests/squad/
  test_registry.py       # resolve, fallback, missing, list_actors, @mention parsing (13 tests)
  test_history.py        # tool filter + SquadAgent._get_chat_history (6 tests)
  test_actor.py          # SquadActor._merge_pending_messages attribution (2 tests)
  test_runner.py         # config parsing + backwards compat (7 tests)
  test_http_routing.py   # HttpChannel registry routing integration (1 test)
```

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-03 | Initial design drafted | Capture multi-actor runtime vision |
| 2026-05-04 | Converted to BEP 2; implementation merged | Document as-implemented state |
