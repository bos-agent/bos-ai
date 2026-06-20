# Named Actors: Multi-Actor Runtime Routing

Status: **design** — intended runtime design for addressable long-lived actors.

---

## Core Insight

Today `bos` can run one or more long-lived `AgentActor` instances in a single runtime. Named Actors is the runtime feature that makes those actors addressable by stable names:

- clients can route within one chat by typing a leading `@actor-name`
- channels can bind directly to a specific actor address such as `agent@investment`
- each actor can reuse an agent kind while keeping its own runtime identity and scoped memory view

This is **not** a swarm, team, or autonomous multi-agent orchestration layer. Named Actors only provides addressable actor instances and routing. Planning, delegation, task ownership, cancellation, relay, loop prevention, synthesis, and actor-to-actor orchestration belong in a separate layer that may target named actors later.

## Vocabulary

| Term | Meaning | Example |
|---|---|---|
| Actor name | Stable runtime identity, route name, and memory scope. It is the key under `[main.actors]`. | `bob`, `investment` |
| Agent kind | Reusable agent definition/blueprint loaded from `platform.agents` or `agent_dirs`. | `architect`, `assistant` |
| Actor address | Mailbox address derived from the actor name. | `agent@bob` |
| Display name | Human-facing label for UI rendering. Optional. | `Bob` |
| Display label | Rendered as display name plus agent kind. | `Bob (architect)` |

The actor name and agent kind are intentionally separate. Multiple actors may instantiate the same agent kind:

```toml
[main.actors.bob]
agent = "architect"
display_name = "Bob"

[main.actors.alice]
agent = "architect"
display_name = "Alice"
```

There is no separate `identity` or `memory_scope` field. The actor key is the canonical identity and memory scope. This avoids confusing states such as `[main.actors.sam] identity = "bob"`.

## Primary Use Cases

1. **Multiple long-lived named actors in one runtime**

   A client can route messages in one chat:

   ```text
   @bob review this architecture
   @alice find related design precedents
   ```

2. **Channel-to-actor binding**

   Different channels can target different actor addresses while sharing the same runtime and memory backend:

   ```toml
   [[main.channels]]
   name = "TelegramChannel"
   bind_address = "channel@telegram-investment"
   target_address = "agent@investment"

   [[main.channels]]
   name = "TelegramChannel"
   bind_address = "channel@telegram-learning"
   target_address = "agent@learning"
   ```

Additional valid uses:

- tool/capability separation by actor
- model/cost routing by actor
- domain personas over shared user memory
- one runtime serving multiple interfaces

## Package

The feature should live under a package name that matches the public concept:

```text
src/bos/named_actors/
  __init__.py
  registry.py       # ActorRegistry: @mention parsing + address resolution
  actor.py          # NamedActor + NamedAgent
  memory.py         # ScopedMemory wrapper
  runner.py         # start_named_actors()
```

## ActorRegistry

`ActorRegistry` owns leading `@mention` parsing and actor address resolution. It is not a forwarding mailbox, so routing does not add a message hop.

```python
@dataclass
class ActorRecord:
    name: str
    address: str      # e.g. "agent@bob"
    mailbox: MailBox
    is_default: bool
    display_name: str | None = None
    agent_kind: str | None = None

class ActorRegistry:
    def register(self, name: str, mailbox: MailBox, *, is_default: bool = False) -> None
    def resolve_address(self, target_actor: str | None) -> str
    def resolve_mailbox(self, target_actor: str | None) -> MailBox
    def list_actors(self) -> dict[str, ActorRecord]
    def route(self, content: str, metadata: dict | None = None) -> RouteResult:
        """Parse leading @actor-name, return routing target, cleaned content, and metadata."""
```

`route()` is the single place `@mention` syntax is parsed for user/channel ingress.

### @mention Syntax

Uses regex `@([\w][\w-]*)\s+` to extract leading mentions. The mention must be at the start of the message. Mentions mid-content are treated as literal text. Hyphenated names are supported, for example `@code-reviewer`.

Unknown `@mentions` are treated as literal text and routed to the default actor.

## NamedAgent / ScopedMemory

Named Actors should keep the current `MemoryExtension` protocol unchanged. Actor scoping belongs in a wrapper around the configured memory backend, not in the backend protocol itself.

Conceptual wrapper:

```python
class ScopedMemory:
    def __init__(self, inner: MemoryExtension, scope: str) -> None:
        self._inner = inner
        self._scope = scope

    def _maxim_key(self, key: str) -> str:
        if key == "user":
            return "user"
        return f"actors:{self._scope}:{key}"

    async def get_maxim(self, key: str) -> str:
        return await self._inner.get_maxim(self._maxim_key(key))

    async def set_maxim(self, key: str, content: str) -> None:
        await self._inner.set_maxim(self._maxim_key(key), content)

    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str:
        scoped_tags = [*(tags or []), f"scope:{self._scope}"]
        return await self._inner.ingest_memory(content, tags=scoped_tags)

    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        entries = await self._inner.search_memories(query, top_k=max(top_k * 4, 20))
        return [entry for entry in entries if self._is_visible(entry)][:top_k]

    async def get_memory(self, entry_id: str) -> MemoryEntry | None:
        entry = await self._inner.get_memory(entry_id)
        return entry if entry is not None and self._is_visible(entry) else None

    async def forget_memory(self, entry_id: str) -> None:
        entry = await self.get_memory(entry_id)
        if entry is not None:
            await self._inner.forget_memory(entry_id)

    def _is_visible(self, entry: MemoryEntry) -> bool:
        tags = set(entry.tags)
        return f"scope:{self._scope}" in tags or "scope:global" in tags
```

The wrapper uses delimiter-safe maxim keys such as `actors:bob:self`, not
path-like keys such as `actors/bob/self`. The current default
`MarkdownMemoryExtension` maps a maxim key directly to `maxims/<key>.md`; slash
characters would create nested paths and require parent-directory creation plus
path-safety rules. Colon-delimited keys work with the current protocol and
default backend without changing the memory extension.

Memory behavior:

- `user` is the only shared maxim.
- `self`, `rules`, and all custom maxims are actor-scoped.
- The LLM still sees ordinary maxim keys such as `user`, `self`, and `rules`.
- `Remember`, `Recall`, and `Forget` should appear unscoped to the LLM; the wrapper maps calls to actor-scoped keys/tags internally.
- Search should over-fetch before filtering so actor-scoped results are not accidentally dropped by a backend's `top_k` limit.

This preserves backend compatibility while allowing actors using the same agent kind to have different identity, rules, and durable memories.

## Chat History Attribution

In one shared chat, an actor should be able to see relevant request/response history involving other actors, but the history must retain routing attribution.

Stored messages should preserve metadata rather than relying only on text prefixes:

```python
{
    "speaker_type": "user",
    "from": "user",
    "to_actor": "bob",
    "to_address": "agent@bob",
    "channel": "channel@http",
}
```

Actor responses should similarly record the actor source:

```python
{
    "speaker_type": "actor",
    "from_actor": "bob",
    "from_address": "agent@bob",
    "to": "user",
    "channel": "channel@http",
}
```

When preparing model context, the agent can render metadata into readable labels:

```text
[user -> Bob (architect)]: Can you review this design?
[Bob (architect) -> user]: The boundaries are plausible, but...
[user -> Alice (architect)]: Find related prior art.
[Alice (architect) -> user]: I found three relevant precedents...
```

The source of truth should be `Message.metadata`; rendered labels are only a model-context view.

## Runner

`start_named_actors(workspace: Workspace) -> None`:

1. Reads `[main.actors]` from `workspace.config`.
2. Uses `workspace.harness()` as context manager.
3. Creates one actor per actor entry, each with `MailBox` at `agent@<actor-name>`.
4. Builds `ActorRegistry`, registers all actors, and uses the `"main"` actor as default when present.
5. Creates channels from `[[main.channels]]`, passing the registry to channels that support mention routing.
6. Writes channel endpoint info to `agent.state` for TUI discovery.
7. Launches all actors and channels in an `asyncio.TaskGroup`.

### Backwards Compatibility

When no `[main.actors]` section is configured, behavior is unchanged: one `AgentActor` runs at `agent@main`.

## Configuration

Actors live under `[main]` because they are runtime instances, alongside channels.

```toml
[main.actors.main]
agent = "assistant"
display_name = "Main"

[main.actors.bob]
agent = "architect"
display_name = "Bob"
tools = ["ReadFile", "AskSubagent"]

[main.actors.investment]
agent = "assistant"
display_name = "Investment"
tools = ["WebSearch"]
```

- Each key under `[main.actors]` is the actor name, routing name, canonical identity, and memory scope.
- `agent` references a reusable agent kind defined in `platform.agents` or `agent_dirs`.
- The actor's mailbox address is `agent@<actor-name>`.
- `display_name` is optional and only affects user-facing labels.
- The role label is derived from `agent`; there is no separate `role_label`.
- The actor with routing name `"main"` is the default fallback when no `target_actor` is specified.
- If no `[main.actors]` section exists, behavior is unchanged.

### Channel Config

Channels remain under `[[main.channels]]`. `target_address` can point at any `agent@*` address:

```toml
[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
host = "127.0.0.1"
port = 5920
```

Mention routing is channel-specific. `HttpChannel` supports registry-backed `@actor` routing. A channel can still bind to a specific actor via `target_address` even if it does not support mention routing.

## End-to-End Flow

```text
1. User types "@bob review this design" in TUI.
2. TUI sends raw content via WebSocket to HttpChannel.
3. HttpChannel calls ActorRegistry.route().
4. route() parses "@bob", strips it:
   content  = "review this design"
   target   = "bob"
   address  = "agent@bob"
   metadata = {"target_actor": "bob", ...}
5. HttpChannel sends content to "agent@bob" with route_result.metadata.
6. Named actor at agent@bob receives the message.
7. NamedAgent asks the underlying ReActAgent with:
   - shared chat history rendered with actor attribution
   - scoped memory wrapper for maxims and memory tools
   - actor address/name in turn metadata
8. Response flows back via event_sink -> channel -> client.
```

## Core Changes

Named Actors requires one core topology relaxation in `workspace.py:_validate_channel_topology`:

```python
if channel.target_address.startswith("agent@"):
    continue
```

All actor routing logic otherwise lives outside the core actor/mailbox primitives.

## HttpChannel Integration

`HttpChannel` gets an optional `actor_registry` parameter. `_send_handler` and `_ws_handler` call `registry.route()` before sending to the mailbox:

```python
recipient = env.recipient
content = env.content
metadata = env.metadata
if registry is not None:
    route_result = registry.route(
        str(content) if isinstance(content, str) else content,
        metadata=metadata,
    )
    recipient = route_result.target_address
    content = route_result.content
    metadata = route_result.metadata
```

The routed metadata must be propagated into `mailbox.send(...)`; otherwise `target_actor` attribution can be lost.

## Error Handling

| Scenario | Behavior |
|---|---|
| No `[main.actors]` section | Backwards compatible: single `main` actor |
| `@mention` not in registry | Treated as literal text, falls back to default actor |
| No `target_actor` and no default | `KeyError` raised |
| Channel has no registry reference | Uses `target_address` from config directly |
| Agent spec has extra keys (e.g. `display_name`) | Runtime-specific actor keys are filtered before agent construction |

## Explicit Non-Goals

Named Actors does not implement:

- autonomous swarm behavior
- actor-to-actor relay
- task planning or decomposition
- delegation lifecycle management
- cancellation across delegated work
- result synthesis across actors
- loop prevention between actors

Future orchestration systems may target named actors as execution endpoints, but orchestration is a separate layer above this feature.

## Tests

Required coverage:

- registry resolve, fallback, missing default, actor listing, and `@mention` parsing
- named actor runner config parsing and backwards compatibility
- channel-to-actor binding via `target_address`
- `HttpChannel` registry routing integration
- actor key is the canonical identity/memory scope
- one agent kind can instantiate multiple named actors
- `ScopedMemory` maps `user` to shared maxim storage
- `ScopedMemory` maps all other maxims, including custom maxims, to actor-scoped keys
- `Remember`, `Recall`, and `Forget` stay unscoped in the model-facing tool surface
- routed HTTP metadata is propagated into `mailbox.send`
- chat history rendering uses `Message.metadata` attribution

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-05-03 | Initial design drafted | Capture multi-actor runtime vision |
| 2026-05-05 | Reframed as Named Actors | Scope feature to addressable long-lived actors, clarify identity/memory semantics, and move orchestration out of scope |
