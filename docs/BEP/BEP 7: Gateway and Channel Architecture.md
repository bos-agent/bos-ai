# BEP 7: Gateway and Channel Architecture

Status: **design draft** — gateway/channel responsibility boundary and scope revised after review.

Entry strategy: go straight to the end-state design. The project is not in production use, so BEP 7 does not preserve legacy `HttpChannel`, `client_id`, or REST send compatibility.

---

## Core Insight

The current architecture conflates two concerns: the HTTP/WebSocket server is implemented as a channel (`HttpChannel`), and channels are treated as transport peers in a flat `TaskGroup` alongside the actor. This creates several problems:

1. **HttpChannel is not a channel** — it's infrastructure. The HTTP server provides a universal API for all clients (TUI, SDKs, web frontends). Real channels (Telegram, Slack, etc.) adapt external platforms. Conflating them makes the HTTP server hard to evolve independently.
2. **No process root** — the runner creates everything in one `TaskGroup`. If the actor crashes, everything goes down. There's no stable control plane for dynamic actor lifecycle management.
3. **Channels are underspecified** — they're thin wrappers around a mailbox binding. There's no stable channel identity beyond `bind_address`, no platform identity validation, and no notion of channels as persistent user interaction contexts.

BEP 7 introduces the **Gateway** as the process root and HTTP/runtime infrastructure, separates conversational transports into channels, and formalizes channels as named, first-class interaction contexts. The gateway creates and supervises channels, but conversational messages flow through channel mailboxes rather than through the gateway itself.

The reference architecture draws from two prior-art projects: **nanobot** (channel message bus and channel manager patterns) and **hermes-agent** (gateway as process root and factory-based adapter registration). BOS keeps a Protocol-first channel contract rather than adopting an ABC-first adapter API.

---

## Goals

1. **Single gateway runtime mode** — BOS has one runtime path: gateway + actor manager + channel manager. Single-agent mode is represented as one configured actor, not a separate runner mode.
2. **Gateway as process root** — the gateway owns application orchestration: actor manager, channel manager, HTTP server, dynamic WebSocket channel creation, and graceful shutdown. It is the stable spine that can keep HTTP/WebSocket infrastructure alive across actor restarts.
3. **Harness as service container** — the harness continues to own framework services such as `mail_route`, chat store, consolidator, and LLM client. The gateway uses these services; it does not replace them.
4. **HTTP server as infrastructure** — the HTTP server is gateway-owned. It provides authentication, status, actor-listing, upload endpoints, and WebSocket handshakes. It is not itself a conversational channel.
5. **Channels as named interaction contexts** — a channel is a user-facing access point (a "desk," a "device," a "purpose"), not just a transport adapter. Channels have implementation types, channel IDs, display names, default actors, and mailbox addresses.
6. **`channel_id` as channel identity** — one `channel_id` identifies one active channel instance/access point. Duplicate configured channel IDs are rejected; duplicate dynamic channel IDs are rejected unless explicit takeover is requested.
7. **Actor-aware routing via `ActorResolver`** — channels call an actor resolver to translate content/context/default actor into an `agent@...` address. The gateway does not proxy every message.
8. **Multi-channel chat portability with revision checks** — multiple channel conversations may attach to the same `chat_id`, but sends are server-guarded by chat revision so stale clients must rehydrate before adding new turns.

## Non-Goals

1. This BEP does **not** implement full dynamic actor lifecycle (spawn/stop actors at runtime via API). The gateway gains the actor manager structure needed for it, but the initial implementation starts with statically configured actors.
2. This BEP does **not** make channels external processes (Option B). Channels remain in-process with the gateway.
3. This BEP does **not** redesign the mail route internals. Channels still bind mailboxes; the gateway does not replace the mail route.
4. This BEP does **not** define a REST "send message to agent" API. Conversational sends enter through channels. REST agent-send/event-stream semantics belong in a future BEP if needed.
5. This BEP does **not** implement multi-user support. BOS remains single-user. Channels are multiple access points for one user.
6. This BEP does **not** define channel/actor streaming deltas. Streaming and event-stream agent-send APIs belong in a future BEP.
7. This BEP does **not** add channel-level plugin/hook support — that belongs in a future BEP.

---

## Architecture Overview

```
Runner
  └─ creates Harness
       └─ creates Gateway

Process
┌────────────────────────────────────────────────────────────────┐
│ Gateway                                                         │
│ (application/process root — lifecycle owner)                    │
│                                                                │
│  ┌───────────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │ HTTP Server       │  │ ActorManager │  │ ChannelManager  │ │
│  │ /api/status       │  │ actors       │  │ persistent + WS │ │
│  │ /api/actors       │  │ restart      │  │ registry/status │ │
│  │ /api/upload-image │  └──────┬───────┘  └────────┬────────┘ │
│  │ /ws               │         │                   │          │
│  └─────────┬─────────┘         │                   │          │
│            │ creates           │                   │          │
│            ▼                   ▼                   ▼          │
│    WSChannel(dynamic)       Actors              Channels      │
│    one per connection       agent@main          Telegram      │
│                             agent@coder         Slack         │
│                                                                │
│  ┌────────────────┐   ┌──────────────────┐                    │
│  │ ActorResolver  │   │ ChatCoordinator  │                    │
│  │ @actor →       │   │ revisions,       │                    │
│  │ agent@...      │   │ active turns,    │                    │
│  │                │   │ cursors          │                    │
│  └────────────────┘   └──────────────────┘                    │
│                                                                │
│  Uses Harness services:                                        │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ MailRoute · ChatStore · Consolidator · LLM Client        │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘

Message flow:
  TUI ──WS──▶ HTTP Server ──creates──▶ WSChannel
  WSChannel ◀──▶ MailRoute ◀──▶ Actor

  Telegram ◀──▶ TelegramChannel ◀──▶ MailRoute ◀──▶ Actor
  Slack    ◀──▶ SlackChannel   ◀──▶ MailRoute ◀──▶ Actor

HTTP API:
  SDK/CLI ──HTTP──▶ status / actors / upload-image only
  REST agent-send: future BEP
```

### Key architectural rules

1. **One runtime path.** The gateway runtime is the only BOS runtime mode. Named actors are the core actor model; a single-agent setup is just `[runtime.actors.main]`.
2. **Runner bootstraps; gateway runs.** The runner loads workspace/config, creates the harness, instantiates the gateway, and awaits `gateway.run()`. The runner no longer manually creates actors/channels.
3. **Harness owns framework services.** The harness creates and owns `mail_route`, chat store, consolidator, LLM client, and agent construction facilities. Gateway, actors, and channels use these services.
4. **Gateway is the application/process root.** It owns runtime orchestration: HTTP server, actor manager, channel manager, dynamic WS channel creation, status, and graceful shutdown.
5. **Gateway is not normally a mail participant.** Conversational messages enter through channel mailboxes. The gateway owns status, actor-listing, upload, and WebSocket handshake endpoints, but it does not sit in the response path for normal chat turns.
6. **HTTP Server is infrastructure.** It handles authentication, status endpoints, actor-listing, upload endpoints, and WebSocket handshakes. A successful WebSocket handshake creates a dynamic channel.
7. **Channels are mail route peers.** Each conversational channel binds a mailbox and bridges between an external platform/client and the mail route. The actor replies to the channel mailbox that sent the current turn and preserves channel conversation metadata.
8. **ChannelManager manages lifecycle, not delivery.** It starts/stops/registers/unregisters channels and enforces duplicate `channel_id`/takeover policy. It does not inspect every outbound envelope.
9. **MailRoute delivers envelopes.** Given an exact recipient address such as `agent@main` or `channel@tui-1`, the mail route delivers the envelope.
10. **ActorResolver resolves semantic targets.** Given message content, actor mentions, and a channel default actor, it returns an actor address such as `agent@coder`.
11. **WS connections are dynamic channels.** Each successful WebSocket handshake at `/ws` creates a channel instance that lives for the duration of that connection. The channel is keyed by `channel_id`.

---

## Channel Design

### Channel Protocol and Optional `BaseChannel` Helper

The public channel contract should remain structural, matching BOS's existing style for `MailBox`, `ChatStore`, and other runtime contracts. Channel implementations do **not** need to inherit from a BOS base class.

```python
@runtime_checkable
class Channel(Protocol):
    @property
    def channel_id(self) -> str:
        """Unique BOS channel instance/access-point identity."""
        ...

    @property
    def display_name(self) -> str | None:
        """Human-readable label for status/UI."""
        ...

    @property
    def target_actor(self) -> str:
        """Default actor name for messages without explicit routing."""
        ...

    @property
    def identity_key(self) -> str | None:
        """Stable external platform identity for duplicate validation.

        Examples: "telegram:bot:12345", "slack:app:A01234567".
        Return None only when the adapter has no externally stable identity
        or explicitly supports shared platform identity.
        """
        ...

    async def run(self, mailbox: MailBox) -> None:
        """Run the channel until stopped.

        The mailbox is both:
        - the channel's BOS sender identity for messages to actors
        - the inbox for actor replies/events to this channel
        """
        ...
```

BEP 7 channels are conversational mailbox peers. The BEP does not model inbound-only/outbound-only simplex transports; REST send, SSE, webhook-only, and polling channels belong in future BEPs.

Because channels are the conversational ingress point, they need a narrow gateway-owned runtime context for preflight and semantic routing. This is not a reference to the whole `Gateway`; it is a small service bundle containing only the coordination services a channel needs before sending an envelope to an actor.

```python
@dataclass(frozen=True)
class ChannelRuntimeContext:
    actor_resolver: ActorResolver
    chat_coordinator: ChatCoordinator
    mail_route: MailRoute
    state_changed: Callable[[], Awaitable[None]] | None = None
```

`ChannelRuntimeContext` belongs to `bos.gateway`, not `bos.core`. The public `Channel` contract remains gateway-agnostic and structural. If a shared `BaseChannel` helper lives in `bos.core`, it must not import gateway classes; otherwise the gateway-aware helper can live under `bos.gateway`.

`channel_conversation_id` is intentionally **not** a `Channel` property. A persistent channel can multiplex many external conversations. Conversation identity is message/cursor-level state represented by `ChannelConversationRef`, not channel-level identity.

```python
@dataclass(frozen=True)
class ChannelConversationRef:
    channel_id: str
    channel_conversation_id: str
```

Dynamic WebSocket channels are one channel per connection, so they use a single synthetic conversation ID:

```python
ChannelConversationRef(channel_id="tui-local", channel_conversation_id="default")
```

Persistent platform channels derive a conversation ref from each external thread/chat:

```python
ChannelConversationRef(channel_id="telegram:daily", channel_conversation_id="tg_chat:123456")
ChannelConversationRef(channel_id="slack:work", channel_conversation_id="channel:C001/thread:1700000000.000000")
```

An optional gateway-aware `BaseChannel` helper may exist for common constructor/settings/runtime-context handling, but it is not the extension contract. If a helper is placed in `bos.core` instead of `bos.gateway`, the `runtime` field must be typed generically to avoid a core → gateway dependency:

```python
SettingsT = TypeVar("SettingsT")

class BaseChannel(Generic[SettingsT]):
    SettingsType: ClassVar[type[SettingsT] | None] = None

    def __init__(
        self,
        *,
        channel_id: str,
        target_actor: str,
        display_name: str | None = None,
        settings: SettingsT,
        runtime: ChannelRuntimeContext | None = None,
    ) -> None:
        self._channel_id = channel_id
        self._target_actor = target_actor
        self._display_name = display_name
        self._settings = settings
        self._runtime = runtime

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def display_name(self) -> str | None:
        return self._display_name

    @property
    def target_actor(self) -> str:
        return self._target_actor

    @property
    def identity_key(self) -> str | None:
        return None
```

Example adapter settings/identity:

```python
@dataclass(frozen=True)
class TelegramSettings:
    bot_id: str
    token_env: str

class TelegramChannel(BaseChannel[TelegramSettings]):
    SettingsType = TelegramSettings

    @property
    def identity_key(self) -> str:
        return f"telegram:bot:{self._settings.bot_id}"
```

Gateway/channel factory validation flow:

```python
settings_type = getattr(channel_type, "SettingsType", None)
settings = validate_settings(settings_type, raw_settings) if settings_type else raw_settings
channel = channel_type(
    channel_id=cfg.channel_id,
    target_actor=cfg.target_actor,
    display_name=cfg.display_name,
    settings=settings,
    runtime=channel_runtime_context,
)

if channel.identity_key:
    reject_duplicate_identity_key(channel.identity_key)
```

`SettingsType` is optional and is discovered with `getattr(channel_type, "SettingsType", None)`; when absent, raw `settings` is passed through as a plain dict. This keeps identity validation based on the same normalized settings the channel will use at runtime. Prefer explicit stable identity fields in config (`bot_id`, `app_id`, etc.) over network discovery during validation.

The channel factory supplies `runtime=channel_runtime_context` for both persistent and dynamic channels. Channels use this context to call `ChatCoordinator.prepare_send()` / `hydrate()` / `mark_observed()` and `ActorResolver.resolve()` themselves. This keeps the gateway out of the normal message response path while avoiding globals or actor-side stale checks.

### `ChannelManager` — Lifecycle Registry

`ChannelManager` manages channel lifecycle and identity. It does **not** replace `MailRoute` and does **not** centrally dispatch every outbound message. Actors reply to channel mailbox addresses, and channels consume their own mailboxes.

```python
class ChannelManager:
    """Manages channel lifecycle, identity, status, and takeover policy."""

    channels: dict[str, Channel]        # keyed by channel_id

    async def start_all(self) -> None: ...
    async def stop_all(self) -> None: ...
    async def register(self, channel: Channel, *, takeover: bool = False) -> None: ...
    async def unregister(self, channel_id: str) -> None: ...
    async def start_channel(self, channel: Channel) -> None: ...
    async def stop_channel(self, channel_id: str) -> None: ...

    def get(self, channel_id: str) -> Channel | None: ...
    def list_status(self) -> list[dict]: ...
```

Responsibilities:

- start/stop configured persistent channels
- register/unregister dynamic WebSocket channels
- enforce duplicate `channel_id` and explicit takeover semantics
- expose status for gateway health/status endpoints
- optionally supervise/restart failed persistent channels

Non-responsibilities:

- choosing which actor should handle a message (`ActorResolver`)
- delivering envelopes to mailbox addresses (`MailRoute`)
- broadcasting every actor response to clients

### `ActorResolver` — Semantic Actor Selection

`ActorResolver` translates actor mentions and channel defaults into mailbox addresses. It is a small service shared by gateway-created dynamic channels and persistent platform channels.

```python
@dataclass
class ActorRouteResult:
    target_actor: str
    target_address: str          # e.g. "agent@coder"
    content: MessageContent      # possibly with @mention stripped
    metadata: dict

class ActorResolver:
    def resolve(
        self,
        content: MessageContent,
        *,
        default_actor: str,
        metadata: dict | None = None,
    ) -> ActorRouteResult: ...
```

Examples:

- `@coder fix this` with default actor `main` resolves to `agent@coder` and content `fix this`.
- `summarize this` in a channel configured with `target_actor = "main"` resolves to `agent@main`.
- an unknown mention returns a structured error that the channel can render appropriately.

Route metadata keeps the gateway routing name and the agent-history identity explicit:

```python
metadata = {
    "target_actor": "coder",     # gateway routing / compatibility
    "target_agent": "coder",     # core Agent history attribution
    "target_address": "agent@coder",
    "target_display": "Coder",   # when configured
}
```

When the gateway runs more than one actor, `ActorManager` enables the core `Agent`'s optional history attribution mode for every managed actor. The core agent then renders shared-chat transcript context as a conversation between named agents:

- user messages targeted at an agent are prefixed with `[user -> <target>]`
- the current agent's own assistant history remains assistant-role context and may be prefixed with `[assistant: <agent>]`
- other agents' assistant history is converted to user-role context and prefixed with `[assistant <agent> said]`

Single-agent runtimes leave history attribution disabled so ordinary chats keep their raw transcript shape. Attribution metadata is agent-keyed (`agent_name`, `agent_display`, `target_agent`, `target_display`); gateway-specific actor names are only the source used to set those agent keys.

`ActorResolver` and `MailRoute` are intentionally separate:

| Component | Question answered | Layer |
|-----------|-------------------|-------|
| `ActorResolver` | Which actor should handle this user intent? | Semantic/application routing |
| `MailRoute` | Which mailbox address receives this envelope? | Transport delivery |

### Channel Kinds

| Kind | Created by | Lifetime | Role | Examples |
|------|------------|----------|------|----------|
| `persistent` | Config → gateway spawn at startup | Process lifetime | Platform/channel adapter | TelegramChannel, SlackChannel |
| `dynamic` | WebSocket handshake at `/ws` | Connection lifetime | One WS connection as one channel | TUI session |


Both kinds implement the same `Channel` protocol: a conversational channel runs with a mailbox and bridges external conversation events to/from actor mail. Simplex transports are out of scope for BEP 7.

A generic REST `send message to agent` endpoint is intentionally out of scope for this BEP. Conversational interactions in BEP 7 enter through channel mailboxes. REST/SSE/webhook/polling semantics should be specified in future BEPs if needed.

### Channel Identity: `channel_id` and Platform Identity

Persistent channels have two related identities:

| Identity | Purpose | Example | Validation |
|----------|---------|---------|------------|
| `channel_id` | BOS channel instance/access-point identity | `telegram:daily` | unique across all channels |
| platform identity | external endpoint identity reported by the adapter | `telegram:bot:12345` | unique unless adapter explicitly allows sharing |

`channel_id` is explicit in config for persistent channels. It is not auto-derived as the primary path; explicit IDs make chat cursors, logs, status output, and debugging stable and understandable. Dynamic WebSocket channels provide `channel_id` during handshake.

Platform identity is adapter-specific and is validated through the channel instance `identity_key` property after settings normalization. For example, a Telegram adapter can reject two configured channels that resolve to the same bot ID, because they would race on the same update stream and create ambiguous outbound identity.

Examples:

```
TelegramChannel  → channel_id = "telegram:daily"
                    identity_key = "telegram:bot:12345"
                    chats: 123456, 789012…
SlackChannel     → channel_id = "slack:work"
                    identity_key = "slack:app:A01234567"
                    chats: C001, C002…
TUI session      → channel_id = "tui-local"
                    identity_key = None
```

Rules:

- one `channel_id` = one active channel instance/access point
- duplicate dynamic `channel_id` on WebSocket → rejected (HTTP 409) unless takeover
- duplicate non-null platform identity keys → rejected unless the adapter explicitly declares shared identity support
- actors route replies by `Envelope.recipient` to the sending channel mailbox; `channel_id` may be carried in metadata for status/debugging but is not the primary delivery mechanism

---

## Channel Conversation Mapping

A `channel_id` identifies a channel instance. A `channel_conversation_id` identifies an external conversation within that channel. Cursors and observed revisions are tracked per conversation ref, not per channel.

```python
cursor: dict[ChannelConversationRef, str]  # ref -> chat_id
observed_revision: dict[tuple[ChannelConversationRef, str], int]  # (ref, chat_id) -> revision
```

Rules:

- persistent channels may have many `channel_conversation_id` values
- dynamic WS channels are one channel per connection and use `channel_conversation_id = "default"`
- incoming platform messages resolve a `ChannelConversationRef`, then resolve that ref's BOS `chat_id` cursor
- `/new` and `/resume` update the cursor for the current `ChannelConversationRef` only
- stale-send checks compare the ref's observed revision for that `chat_id` against the chat's current revision
- outbound replies include enough routing metadata for the channel to deliver back to the originating `channel_conversation_id`

Inbound envelopes should carry channel conversation routing metadata:

```python
metadata = {
    "channel": {
        "channel_id": "telegram:daily",
        "channel_conversation_id": "tg_chat:123456",
    },
    "base_revision": 12,
}
```

Actor replies and turn events must preserve channel conversation routing metadata. For a persistent channel with multiple conversations, `env.chat_id` is not enough to choose the external destination because multiple channel conversations may attach to the same BOS chat. The actor/coordinated actor must copy `metadata["channel"]` from the inbound envelope to final replies, command results, and turn events for that turn.

Outbound metadata shape:

```python
metadata = {
    "channel": {
        "channel_id": "telegram:daily",
        "channel_conversation_id": "tg_chat:123456",
    },
    "chat_revision": 13,  # when known for committed final replies
}
```

## Gateway Authentication

Gateway HTTP/WS access starts with a simple API-key model.

Rules:

- all gateway HTTP endpoints require authentication unless explicitly documented as local-only health probes
- WebSocket handshakes require the same authentication as HTTP endpoints
- preferred form: `Authorization: Bearer <api_key>`
- query-string tokens should be avoided; if a constrained client cannot set headers, a fallback must be explicitly documented and treated as less safe
- API keys are sourced from the environment variable named by `[runtime.gateway].api_key_env`; secrets must not be logged or written to `gateway.state`

Example:

```toml
[runtime.gateway]
host = "127.0.0.1"
port = 5920
api_key_env = "BOS_GATEWAY_API_KEY"
```

This is intentionally minimal. Multi-user auth, OAuth, scoped permissions, and per-channel identity are out of scope for BEP 7.

## Runner / Harness / Gateway Relationship

The three runtime layers have distinct responsibilities. There is no separate named-actor runtime mode; named actors are the core actor model used by the gateway runtime.

| Layer | Responsibility | Owns | Does not own |
|------|----------------|------|--------------|
| `runner` | bootstrap | workspace/config loading, process/docker selection, harness creation, gateway instantiation | actor/channel internals |
| `harness` | service container | `mail_route`, chat store, consolidator, LLM client, `create_agent()` | HTTP server, actor/channel lifecycle policy |
| `gateway` | application/process root | HTTP server, `ActorManager`, `ChannelManager`, dynamic WS channels, shutdown sequence | service implementation internals such as mail delivery |

Typical startup shape:

```python
async def start(workspace: Workspace) -> None:
    async with workspace.harness() as harness:
        gateway = Gateway(workspace=workspace, harness=harness)
        await gateway.run()
```

The mail route remains harness-owned. Gateway, actors, and channels bind mailboxes from it. This preserves BOS's core primitive boundary and keeps non-gateway tests/runtimes possible.

Package layout target:

```text
src/bos/core/              # AgentActor, core actor model, MailRoute/ChatStore contracts, Channel protocol
src/bos/gateway/           # Gateway, HTTP server, ActorManager, ChannelManager, ActorResolver, ChatCoordinator, ChannelRuntimeContext, CoordinatedActor
src/bos/runner/            # bootstrap/process/docker entrypoints only
```

The existing `src/bos/named_actors` behavior should be folded into core/runtime concepts rather than kept as a separate mode.

## Gateway Design

### Lifecycle

```
boscli start
  │
  ├─ 1. Runner bootstrap: load config (RootConfig), inject envs, load extensions
  ├─ 2. Runner creates harness: mail route, chat store, consolidator, LLM client
  ├─ 3. Runner creates Gateway(harness, runtime_config)
  ├─ 4. Runner awaits gateway.run()
  │     ├─ HTTP Server starts (aiohttp, listens on configured host:port)
  │     ├─ Actor Manager spawns configured actors
  │     └─ Channel Manager spawns configured persistent channels
  │
  │  ... runtime (potentially weeks/months) ...
  │
  ├─ Actor crash → active turns end with error; gateway may spawn a fresh actor
  ├─ SIGTERM → Gateway drains in order:
  │     1. Stop accepting new connections (HTTP 503)
  │     2. Drain in-flight actor turns or abort them on explicit request
  │     3. Stop channels
  │     4. Stop actors
  │     5. Stop HTTP server
  │     6. Exit
```

### Gateway Components

```
Gateway
├── http_server: aiohttp Application
│   ├── GET  /api/status
│   ├── GET  /api/actors
│   ├── POST /api/upload-image
│   └── WS   /ws?channel_id={id}&chat_id={id}
├── actor_manager: ActorManager
│   ├── actors: dict[str, CoordinatedActor]
│   ├── spawn(name, agent_kind) → CoordinatedActor
│   ├── stop(name)
│   └── restart_policy: dict[str, RestartPolicy]
├── channel_manager: ChannelManager
│   ├── channels: dict[str, Channel]      (keyed by channel_id)
│   ├── start_all()
│   ├── stop_all()
│   └── register()/unregister()/status
├── actor_resolver: ActorResolver
│   └── resolve(content, default_actor) -> agent@...
├── chat_coordinator: ChatCoordinator
│   ├── get_cursor(ref) / set_cursor(ref, chat_id)
│   ├── prepare_send(chat_id, ref, base_revision, content_type)
│   ├── hydrate(chat_id, from_revision)
│   └── active_turn_status(chat_id)
├── channel_runtime_context: ChannelRuntimeContext
│   └── narrow services passed to channel constructors
└── harness: AgentHarness
    ├── mail_route
    ├── chat_store
    ├── consolidator
    └── llm_client
```

`ChannelRuntimeContext` is how gateway-owned coordination services cross into channel adapters. It is deliberately narrower than `Gateway`: channels receive resolver/coordinator services, not HTTP server internals, actor manager mutation APIs, or gateway shutdown control.

### HTTP API Contract

The gateway exposes an authenticated HTTP/WebSocket API for status, actor listing, uploads, and dynamic WebSocket channel creation. Conversational sends enter through channels, not a generic REST send endpoint.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/status` | Health check (actor status, uptime, channel count) |
| `GET` | `/api/actors` | List named actors with display names and agent kinds |
| `POST` | `/api/upload-image` | Upload image, return path-backed image part |
| `WS` | `/ws?channel_id={id}&chat_id={id}` | Bidirectional session; creates a dynamic channel |

**WebSocket routing:** The `/ws` endpoint creates a dynamic `WSChannel` bound to the channel's mailbox. The `WSChannel`, not the gateway request handler, calls `ActorResolver` per inbound message. Per-message routing to other actors is done via `@mention` in message content (e.g., `@coder fix this bug`). There is no `?actor=` query parameter — the channel default actor comes from runtime/channel config.

**Command routing:** `/new` and `/resume` commands that change `chat_id` update the current `ChannelConversationRef` cursor and must hydrate that channel conversation to the resolved chat revision. They do not automatically close other channels attached to the same chat; stale-send protection is handled by `ChatCoordinator`. Duplicate dynamic `channel_id` takeover remains a `ChannelManager` concern.

### Actor Manager

```python
class ActorManager:
    """Manages actor lifecycle within the gateway."""

    actors: dict[str, CoordinatedActor]  # actor_name → gateway-coordinated actor instance
    configs: dict[str, ActorConfig]     # from runtime.actors
    restart_policies: dict[str, RestartPolicy]

    async def start_all(self) -> None:
        """Spawn all configured actors."""

    async def spawn(self, name: str, agent_kind: str) -> CoordinatedActor:
        """Create and start a new gateway-coordinated actor."""

    async def stop(self, name: str) -> None:
        """Gracefully stop an actor."""

    async def restart(self, name: str) -> CoordinatedActor:
        """Stop and re-spawn an actor."""
```

### Actor Restart Policy

Actor restart is turn-boundary only. BEP 7 does not require actor turn recovery.

- **Planned restart:** gateway stops accepting new turns for the actor, waits for active turns to complete, or aborts them only on explicit request. Once active turns are ended, the actor can be replaced.
- **Unexpected actor crash:** active turns are considered ended with error. The gateway may start a fresh actor, but it does not resume in-flight turns. Channels should be notified when possible.

This keeps restart semantics simple: no actor state snapshotting, no live event-sink retargeting, and no partial turn replay.

### Routing Flow

```
Inbound (WebSocket — dynamic channel):
  WS /ws?channel_id={id}&chat_id={id} handshake completes
    → Gateway authenticates and validates handshake
    → Gateway creates WSChannel(channel_id={id})
    → ChannelManager registers WSChannel, enforcing duplicate/takeover policy
    → WSChannel binds mailbox channel@{id} on the mail route
    → WSChannel creates `ChannelConversationRef(channel_id, "default")`, hydrates the requested chat, and records observed revision
    → Each WS message:
        → WSChannel uses ChannelRuntimeContext.chat_coordinator.prepare_send(chat_id, ref, base_revision, content_type)
        → If stale: WSChannel returns missing transcript and asks client to confirm/retry
        → If active turn conflict: WSChannel returns active-turn status or sends interrupt per policy
        → If OK: WSChannel uses ChannelRuntimeContext.actor_resolver.resolve(content, default_actor)
        → WSChannel sends envelope via MailRoute with metadata.channel + base_revision
        → Actor processes and replies to WSChannel's mailbox, preserving metadata.channel
        → WSChannel reads its mailbox and delivers response/events to the WebSocket client

Inbound (Persistent Channel — Telegram, Slack, etc.):
  Message arrives via channel's external platform
    → Channel resolves `ChannelConversationRef` for the external chat/thread
    → Channel resolves/creates that ref's BOS chat cursor
    → Channel uses ChannelRuntimeContext.chat_coordinator.prepare_send(chat_id, ref, base_revision, content_type)
    → If stale: channel rehydrates from ChatStore and asks user to confirm/retry
    → If OK: channel uses ChannelRuntimeContext.actor_resolver.resolve(content, default_actor)
    → Channel sends envelope via MailRoute with metadata.channel + base_revision
    → Actor processes and replies to channel's mailbox, preserving metadata.channel
    → Channel reads its mailbox and delivers response back to the external channel conversation
```

Key invariant: every conversational message that expects an async response enters through a channel mailbox. The actor replies to the sending channel mailbox and preserves channel conversation metadata for the turn. The gateway creates and supervises channels but does not sit in the normal response path.

---

## Shared Chat Resume and Stale-Client Protection

A `chat_id` is portable across channels, while `channel_id` identifies an access point. Multiple channel conversations may attach to the same chat, but BOS must prevent stale clients from silently adding turns against an incomplete transcript.

BEP 7 uses optimistic concurrency for chat turns:

- each chat has a monotonic revision, e.g. `chat_revision = 42`
- each channel conversation tracks the latest revision it has observed for that chat
- every user send includes that channel conversation's `base_revision`
- the server rejects stale sends before actor execution; channel preflight handles the normal path, and actor `begin_turn` is the final race guard

```python
@dataclass
class PrepareSendResult:
    ok: bool
    chat_id: str
    ref: ChannelConversationRef
    current_revision: int
    stale: bool = False
    active_turn: bool = False
    missing_messages: list[dict] | None = None
    error: str | None = None

class ChatCoordinator:
    def get_cursor(self, ref: ChannelConversationRef) -> str | None: ...
    def set_cursor(self, ref: ChannelConversationRef, chat_id: str, *, observed_revision: int) -> None: ...
    def new_chat(self, ref: ChannelConversationRef) -> str: ...

    def prepare_send(
        self,
        *,
        chat_id: str,
        ref: ChannelConversationRef,
        base_revision: int,
        content_type: MessageType | str = MessageType.MESSAGE,
    ) -> PrepareSendResult: ...

    def mark_observed(self, *, chat_id: str, ref: ChannelConversationRef, revision: int) -> None: ...
    def hydrate(self, *, chat_id: str, from_revision: int | None = None) -> list[dict]: ...
    def active_turn_status(self, chat_id: str) -> dict | None: ...

    def begin_turn(
        self,
        *,
        chat_id: str,
        ref: ChannelConversationRef,
        actor: str,
        turn_id: str,
        base_revision: int,
    ) -> ActiveTurn: ...

    def end_turn(
        self,
        *,
        chat_id: str,
        turn_id: str,
        committed_revision: int | None,
    ) -> None: ...
```

### Chat revision source of truth

Chat revisions are server-assigned sequential integers. Timestamps are useful for display and diagnostics, but they are not concurrency tokens.

Rules:

- revision `0` means an empty or uncommitted chat
- each visible transcript commit advances the revision by exactly one
- a completed, aborted-with-visible-record, or errored-with-visible-record turn is one visible transcript commit
- all messages in the same committed turn share the same `metadata["chat_revision"]`
- compaction summaries do not advance `chat_revision` unless they are deliberately made user-visible transcript events
- active in-flight turns are tracked separately by `ChatCoordinator`; they are not represented by revision

The source of truth is the `ChatStore` transcript. `ChatCoordinator` may cache current revisions for speed, but state must be reconstructable from the chat store by reading the maximum committed `chat_revision`.

BEP 7 replaces passive turn persistence with an explicit commit operation:

```python
@dataclass(frozen=True)
class ChatCommit:
    chat_id: str
    turn_id: str
    revision: int
    messages: list[Message]
    committed_at: datetime

class ChatStore(Protocol):
    async def commit_turn(
        self,
        chat_id: str,
        messages: Iterable[Message],
        *,
        turn_id: str,
    ) -> ChatCommit: ...
```

`commit_turn()` semantics:

- appends a complete visible turn atomically
- assigns the next sequential `chat_revision`
- annotates every committed message with that revision
- returns a `ChatCommit` containing the assigned revision
- rejects empty message lists unless a future design explicitly defines no-op commits

`save_summary()` remains separate from `commit_turn()` and does not advance `chat_revision`.

### Actor integration

`ChatCoordinator` stays in `bos.gateway`, not `bos.core`. The core `AgentActor` remains gateway-agnostic.

To let gateway coordinate active turns without making `ChatCoordinator` part of the core contract, `AgentActor` exposes small protected no-op lifecycle hooks. These are BOS-internal subclass hooks, not plugin extension points:

```python
class AgentActor:
    async def _on_turn_started(self, ctx: ActorTurnContext) -> None:
        pass

    async def _on_turn_finished(self, ctx: ActorTurnContext, result: ActorTurnResult) -> None:
        pass
```

Gateway provides a coordinated actor implementation:

```python
class CoordinatedActor(AgentActor):
    async def _on_turn_started(self, ctx: ActorTurnContext) -> None:
        self._chat_coordinator.begin_turn(
            chat_id=ctx.chat_id,
            ref=ctx.channel_ref,
            actor=ctx.actor_name,
            turn_id=ctx.turn_id,
            base_revision=ctx.base_revision,
        )

    async def _on_turn_finished(self, ctx: ActorTurnContext, result: ActorTurnResult) -> None:
        self._chat_coordinator.end_turn(
            chat_id=ctx.chat_id,
            turn_id=ctx.turn_id,
            committed_revision=result.committed_revision,
        )
```

`prepare_send()` is channel-side preflight for UX and early rejection. It receives `content_type` so it can reject normal messages during active turns while allowing explicit `INTERRUPT_MESSAGE` / `INTERRUPT_ABORT` when policy permits. `begin_turn()` is the authoritative race guard: if another message committed or another turn became active after preflight, `begin_turn()` must fail and the actor must reject the message without running the agent.

The actor must preserve inbound `metadata["channel"]` on all outbound envelopes for the turn. The actor knows a turn has ended when its turn coroutine finishes normally, is aborted/cancelled, raises an error, or is cleaned up by `ActorManager` after an unexpected actor failure. `ActorManager` must clear any active turns for a crashed actor; in-flight turn replay is out of scope.

### Active turn policy

Revision checks handle completed transcript changes. Active turns are separate: a channel conversation can be up to date and still be unable to start a normal message because an actor turn is in flight for the same `chat_id`.

BEP 7 does not define chat ownership. Multiple channel conversations may attach to the same chat. `started_by_ref` is diagnostic only, not authorization.

```python
@dataclass(frozen=True)
class ActiveTurn:
    chat_id: str
    actor: str
    turn_id: str
    base_revision: int
    started_by_ref: ChannelConversationRef  # diagnostic only; not ownership
    started_at: datetime
```

Rules:

- at most one active turn per `chat_id`
- normal messages are accepted only when no active turn exists
- if an active turn exists, normal messages are rejected with active-turn status
- explicit `INTERRUPT_MESSAGE` is accepted from any attached channel conversation whose `base_revision == active_turn.base_revision`
- explicit `INTERRUPT_ABORT` is accepted from any attached channel conversation whose `base_revision == active_turn.base_revision`
- stale channel conversations cannot interrupt or abort a turn they have not observed
- interrupts and aborts do not create a new active turn and do not call `begin_turn()`
- there is no chat-level takeover-after-turn concept in BEP 7
- duplicate `channel_id` takeover remains a channel/session concern only

After the active turn commits and the chat revision advances, any attached channel conversation may send a normal message after observing the new revision.

Flow when a stale client sends:

```
1. TUI sends at revision 42.
2. Agent reply advances chat to revision 44.
3. Telegram has only observed revision 39.
4. Telegram tries to send with base_revision=39.
5. ChatCoordinator rejects before the message reaches the actor.
6. Telegram receives missing messages/revision 44, rehydrates, and asks user to confirm or revise.
7. If user confirms, Telegram retries with base_revision=44.
```

The invariant is based on **last observed revision**, not last sent revision. A channel can safely send only against a transcript version it has seen.

## Configuration

All runtime config stays under `[runtime]`. This is the final end-state schema: no legacy `runtime.agent`, no `bind_address`, no `target_address`, and no `HttpChannel` channel config.

```toml
[runtime]
location = "process"
default_actor = "main"

# ── Gateway / HTTP server (infrastructure) ──

[runtime.gateway]
host = "127.0.0.1"
port = 5920
upload_dir = ".bos/uploads/http"
max_upload_bytes = 20971520    # 20 MiB
api_key_env = "BOS_GATEWAY_API_KEY"

# ── Actors ──

[runtime.actors.main]
agent = "main"
display_name = "Main"
restart_on_error = true
max_restarts = 5

[runtime.actors.main.agent_cfg.tools]
disabled = ["abc"]

[runtime.actors.coder]
agent = "researcher"
display_name = "Coder"
restart_on_error = true
max_restarts = 3

# ── Actor resolver ──

[runtime.actor_resolver]
mention_prefix = "@"

# ── Persistent channels ──

[[runtime.channels]]
type = "TelegramChannel"
channel_id = "telegram:daily"
display_name = "Daily Chat"
target_actor = "main"
settings = { bot_id = "12345", token_env = "TELEGRAM_DAILY_TOKEN" }

[[runtime.channels]]
type = "TelegramChannel"
channel_id = "telegram:invest"
display_name = "Invest Advisor"
target_actor = "main"
settings = { bot_id = "67890", token_env = "TELEGRAM_INVEST_TOKEN" }

[[runtime.channels]]
type = "SlackChannel"
channel_id = "slack:work"
display_name = "Work Desk"
target_actor = "coder"
settings = { app_id = "A01234567", token_env = "SLACK_BOT_TOKEN" }
```

### Runtime config fields

| Key | Type | Required | Purpose |
|-----|------|----------|---------|
| `location` | `str` | no | Runtime location, e.g. `process` or `docker` |
| `default_actor` | `str` | yes | Default actor identity for channels and dynamic WS sessions |

Validation:

- `default_actor` must exist in `[runtime.actors]`
- if no actors are configured, config is invalid; single-agent setups still configure `[runtime.actors.main]`

### Actor config fields

Actor table keys are actor identities. The `agent` field selects the reusable agent kind/spec.

| Key | Type | Required | Purpose |
|-----|------|----------|---------|
| `agent` | `str` | yes | Registered agent kind/spec to instantiate |
| `display_name` | `str` | no | Human-readable label for UI and actor listing |
| `restart_on_error` | `bool` | no | Whether gateway may spawn a fresh actor after an unexpected actor failure |
| `max_restarts` | `int` | no | Restart cap for unexpected actor failures |
| `agent_cfg` | `table` | no | Per-actor agent config overrides; uses the same config-schema shape as `[agent.defaults]` |

Validation:

- actor names must be valid mention names such as `main`, `coder`, `research_bot`
- actor names do not include the `agent@` prefix
- forbidden identity/memory fields stay out of actor config; the table key is the identity
- actor tables reject unknown top-level keys; put agent overrides under `[runtime.actors.<name>.agent_cfg]`

### Actor resolver config fields

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `mention_prefix` | `str` | `"@"` | Prefix used for actor mentions |

Validation:

- actor mentions resolve only to actor identities from `[runtime.actors]`
- `display_name` is UI/status-only and is not a routing key

### Channel config fields

| Key | Type | Required | Purpose |
|-----|------|----------|---------|
| `type` | `str` | yes | Channel class/type registered on `ep_channel` |
| `channel_id` | `str` | yes | Unique BOS channel instance identity |
| `display_name` | `str` | no | Human-readable label for UI/status listing |
| `target_actor` | `str` | no | Default actor for messages without explicit routing; falls back to `runtime.default_actor` |
| `settings` | `dict` | no | Adapter-specific configuration passed to the channel |

Validation:

- `type` must resolve through `ep_channel`
- `channel_id` must be unique across all configured persistent channels
- `target_actor` must exist in `[runtime.actors]`
- internal mailbox address is derived from `channel_id`, e.g. `channel@telegram:daily`
- adapter `identity_key` values must be unique when non-null
- if `settings` is omitted, the channel factory passes an empty settings object/dict or the adapter settings type default

### Gateway config fields

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `host` | `str` | `"127.0.0.1"` | Bind address for the HTTP server |
| `port` | `int` | `5920` | Listen port (`0` = auto-assign) |
| `upload_dir` | `str` | `".bos/uploads/http"` | Directory for uploaded images |
| `max_upload_bytes` | `int` | `20971520` | Max upload size in bytes |
| `api_key_env` | `str` | `"BOS_GATEWAY_API_KEY"` | Environment variable containing the gateway API key |

---

## Run State and Status Format

BEP 7 replaces channel-derived `agent.state` endpoint discovery with gateway-owned runtime state. Because the gateway is the process root and HTTP owner, the state file should be named for the gateway.

State file:

```text
.bos/run/gateway.state
```

The gateway writes a compact operational snapshot:

```json
{
  "runtime": "process",
  "pid": 12345,
  "started_at": "2026-06-02T12:00:00Z",
  "updated_at": "2026-06-02T12:00:03Z",
  "gateway": {
    "host": "127.0.0.1",
    "port": 5920,
    "base_url": "http://127.0.0.1:5920",
    "auth": {
      "type": "api_key",
      "configured": true
    }
  },
  "actors": {
    "main": {
      "agent": "main",
      "display_name": "Main",
      "status": "running",
      "address": "agent@main",
      "active_turns": 0,
      "restart_count": 0
    }
  },
  "channels": {
    "telegram:daily": {
      "type": "TelegramChannel",
      "kind": "persistent",
      "display_name": "Daily Chat",
      "status": "running",
      "address": "channel@telegram:daily",
      "target_actor": "main",
      "identity_key": "telegram:bot:12345",
      "channel_conversation_count": 2
    },
    "tui-local": {
      "type": "WSChannel",
      "kind": "dynamic",
      "display_name": "TUI",
      "status": "running",
      "address": "channel@tui-local",
      "target_actor": "main",
      "connected_at": "2026-06-02T12:00:02Z",
      "channel_conversation_count": 1
    }
  },
  "active_turns": {
    "abc123": {
      "chat_id": "abc123",
      "actor": "main",
      "turn_id": "turn-001",
      "base_revision": 12,
      "started_by": {
        "channel_id": "tui-local",
        "channel_conversation_id": "default"
      },
      "started_at": "2026-06-02T12:00:03Z"
    }
  }
}
```

Rules:

- do not write secrets or secret values to `gateway.state`
- expose auth status only as metadata such as `{ "type": "api_key", "configured": true }`
- expose `identity_key` only when it is non-secret
- include dynamic WS channels while connected
- include `channel_conversation_count`, not full cursor/conversation details, in the compact state
- include active-turn `started_by` as a diagnostic `ChannelConversationRef`, not an ownership field
- detailed cursor/conversation inspection can be a separate authenticated API later if needed

Write timing:

- on gateway startup
- on actor status/restart changes
- on persistent channel status changes
- on dynamic channel register/unregister
- on active turn start/end
- on graceful shutdown
- optionally heartbeat/update `updated_at` every N seconds

`GET /api/status` should return the same compact shape or a documented subset. CLI commands discover the gateway from `gateway.state` rather than scanning configured channels for `HttpChannel`.

---

## What Moves Where

| Current location | Concern | Moves to |
|------------------|---------|----------|
| `HttpChannel._build_app()` | aiohttp app creation, routes | `bos.gateway.http` / `bos.gateway.Gateway` |
| `HttpChannel.run()` | Server startup, listen loop | `bos.gateway` HTTP server lifecycle |
| `HttpChannel._ws_handler()` | WebSocket connection handling | `bos.gateway` dynamic `WSChannel` factory |
| `HttpChannel._send_handler()` | REST send endpoint | Removed from BEP 7 scope; future BEP if needed |
| `HttpChannel._dispatch_to_clients()` | Central outbound dispatch to WebSocket clients | Eliminated — each dynamic `WSChannel` reads its own mailbox and delivers independently. `ChannelManager` does not dispatch outbound mail. |
| `HttpChannel._status_handler()` | Health check endpoint | `bos.gateway` |
| `HttpChannel._actors_handler()` | Actor listing endpoint | `bos.gateway` using core actor model |
| `HttpChannel._upload_image_handler()` | Image upload endpoint | `bos.gateway` |
| `HttpChannel._cleanup_runtime_state()` | Shutdown cleanup | `bos.gateway` shutdown sequence |
| `WS_TAKEOVER_CLOSE_CODE` constant | Shared contract | `bos.protocol` |
| `HttpChannelClient` (in `http_client.py`) | Client library | Update to gateway API-key auth and `channel_id` |
| `src/bos/named_actors/*` | Separate named-actor runtime path | Fold into `src/bos/core` actor model and `src/bos/gateway` managers |
| `ChatStore.save_turn()` | Passive turn persistence | Replace with `ChatStore.commit_turn()` returning `ChatCommit.revision` |
| `agent.state` channel endpoint info | Runtime status/discovery | Replace with gateway-owned `.bos/run/gateway.state` |

---

## Comparison with Reference Implementations

### nanobot (HKUDS)

| Aspect | nanobot | BOS (BEP 7) |
|--------|---------|-------------|
| Channel contract | `BaseChannel`: `start()`, `stop()`, `send()` | Protocol-first `Channel.run(mailbox)` plus optional `BaseChannel` helper |
| Streaming | `send_delta()`, `send_reasoning_delta()`, `send_reasoning_end()` | Out of scope for BEP 7; future BEP should define the full stream protocol first |
| Orchestration | `ChannelManager` — discovery, start_all, dispatch, retry | Borrows lifecycle/registry ideas, but outbound envelope delivery remains `MailRoute` responsibility |
| Message transport | `MessageBus` (publish_inbound / consume_outbound) | MailRoute (mailboxes). Channels are peers, not bus consumers |
| Gateway concept | No dedicated gateway — ChannelManager is the spine | Gateway is the explicit process root |
| Channel identity | Implicit (platform adapter keyed by name) | Explicit: `channel_id` is the universal channel key |
| Multi-chat | Not a stated feature | Core feature: channels are views into shared chats |

### hermes-agent (Nous Research)

| Aspect | hermes-agent | BOS (BEP 7) |
|--------|-------------|-------------|
| Channel contract | `BasePlatformAdapter`: `connect()`, `disconnect()`, `send()` | Protocol-first `Channel.run(mailbox)` — mailbox-native and simpler |
| Registration | Factory-based `PlatformRegistry` with 16 integration points | `ep_channel` ExtensionPoint — single registration point |
| Gateway | `GatewayRunner` as process root, owns adapters | Same concept: `Gateway` as process root |
| Inbound routing | `set_message_handler(handler)` callback | MailRoute binding — channels publish to a mailbox, actors consume |
| Plugin path | `plugin.yaml` + `adapter.py` → `ctx.register_platform()` | `ep_channel` already supports this via extension modules |
| Streaming | Draft streaming via `send_draft()`, typing indicators | Out of scope for BEP 7 |
| Channel identity | Platform enum + session key | `channel_id` — more general, works for both persistent and dynamic channels |
| Integration surface | 16 touch points for a new platform | 1 touch point: implement `Channel` and register on `ep_channel` |

### What BOS Does Differently (and Should Keep)

| BOS trait | Why it's better for BOS |
|-----------|------------------------|
| `channel_id` as channel identity | Already established, works for both persistent (config-driven) and dynamic (WS-driven) channels |
| MailRoute for transport | Channels are peers, not bus consumers or callback registrants. Simpler mental model |
| `ExtensionPoint` registration | One pattern for everything. `ep_channel` is just another EP, consistent with tools, providers, interceptors |
| Chat portability across channels | `ChatCoordinator` cursor/revision tracking gives portable chats without stale-client surprises |
| No 16-point integration checklist | One interface, one registration point. BOS values a small, readable core |

---

## Migration Path

### Phase 1: Replace HttpChannel with Gateway HTTP server

1. Create `bos.gateway.Gateway` class with aiohttp server.
2. Move route handlers from `HttpChannel` to `Gateway`.
3. Remove `HttpChannel` as a runtime channel.
4. Runner creates and awaits `Gateway` instead of iterating `HttpChannel`.
5. `/ws` requires `channel_id`; there is no `client_id` alias.
6. REST send is removed from this BEP scope. Tests are updated to the final behavior.

### Phase 2: Introduce final Channel contract and ChannelManager

1. Finalize the core `Channel` Protocol and optional `BaseChannel` helper; add `ChannelManager` to `bos.gateway`.
2. Add gateway `ChannelRuntimeContext` and pass it to channel constructors from the gateway channel factory.
3. Convert `TelegramChannel` to implement the `Channel` protocol and optionally inherit from `BaseChannel`; implement instance `identity_key`.
4. Register channels on `ep_channel` and validate unique `channel_id` plus adapter `identity_key`.
5. `ChannelManager` handles lifecycle, identity, status, duplicate IDs, and takeover policy. It does not dispatch every outbound envelope.

### Phase 3: Formalize WS connections as dynamic channels

1. `/ws` handler creates a `WSChannel` (dynamic) per connection.
2. `WSChannel` is keyed by `channel_id` and registers with `ChannelManager`.
3. Each `WSChannel` binds its own mailbox and consumes actor replies directly.

### Phase 4: Add chat revision commits and coordinated actors

1. Replace `ChatStore.save_turn()` with `ChatStore.commit_turn()` returning `ChatCommit`.
2. Annotate committed messages with sequential `chat_revision`.
3. Add protected no-op turn lifecycle hooks to core `AgentActor`.
4. Add `bos.gateway.CoordinatedActor` to call `ChatCoordinator.begin_turn()` / `end_turn()`.

### Phase 5: Fold named actors into the single gateway runtime

1. Move named-actor behavior into the core actor model and gateway `ActorManager`.
2. Runner bootstraps gateway and awaits `gateway.run()`.
3. `SIGTERM` handling moves to gateway.
4. Actor restart may keep WebSocket connections open, but active turn recovery/retargeting is explicitly out of scope unless separately designed.

### Final-shape notes

- TUI/HTTP clients use `channel_id` and gateway API-key auth.
- `WS_TAKEOVER_CLOSE_CODE` constant moves to `bos.protocol`.
- Config goes directly to the final `[runtime.gateway]`, `[runtime.actors]`, `channel_id`, `target_actor`, and `runtime.default_actor` shape.

---

## Remaining Open Issues

No BEP 7 design blockers remain in this draft. REST agent-send/event-stream APIs and channel/actor streaming protocols are intentionally out of scope and should be handled by future BEPs if needed.

## Revision History

| Date | Change | Intention |
|------|--------|-----------|
| 2026-06-02 | Channel runtime context | Add narrow `ChannelRuntimeContext` so channels can perform preflight and actor resolution without making gateway a message proxy |
| 2026-06-02 | Gateway state/status | Replace `agent.state` channel discovery with compact `.bos/run/gateway.state`; future REST/streaming items remain out of scope |
| 2026-06-02 | Active turn policy | Choose one active turn per chat, no chat owner, normal-message rejection, and interrupts/aborts from any attached up-to-date channel conversation |
| 2026-06-02 | Channel contract and conversation mapping | Choose Protocol-first `Channel`, optional `BaseChannel`, instance `identity_key`, and per-ref `channel_conversation_id` mapping |
| 2026-06-02 | Chat revision source of truth | Choose sequential `chat_revision`, `ChatStore.commit_turn()`, and gateway `CoordinatedActor` over core `ChatCoordinator` injection |
| 2026-06-02 | Final config/schema direction | Choose single gateway runtime mode, fold named actors into core, place gateway under `bos/gateway`, and add channel `identity_key` validation |
| 2026-06-02 | Scope refinement | Add simple API-key auth, remove streaming and compatibility from BEP 7, scope REST agent-send out, and define turn-boundary actor restart |
| 2026-06-02 | Responsibility boundary revision | Clarify runner/harness/gateway layering, ChannelManager lifecycle-only role, ActorResolver, and shared-chat revision checks |
| 2026-06-01 | Initial draft | Propose gateway/channel separation, channel contract, gateway as process root |
