# BEP 7: Gateway and Channel Architecture

Status: **design draft** — architecture, channel model, gateway design, and config settled. Awaiting review.

---

## Core Insight

The current architecture conflates two concerns: the HTTP/WebSocket server is implemented as a channel (`HttpChannel`), and channels are treated as transport peers in a flat `TaskGroup` alongside the actor. This creates several problems:

1. **HttpChannel is not a channel** — it's infrastructure. The HTTP server provides a universal API for all clients (TUI, SDKs, web frontends). Real channels (Telegram, Slack, etc.) adapt external platforms. Conflating them makes the HTTP server hard to evolve independently.
2. **No process root** — the runner creates everything in one `TaskGroup`. If the actor crashes, everything goes down. There's no stable control plane for dynamic actor lifecycle management.
3. **Channels are underspecified** — they're thin wrappers around a mailbox binding. There's no streaming support, no channel identity beyond `bind_address`, and no notion of channels as persistent user interaction contexts.

BEP 7 introduces the **Gateway** as the process root and control plane, separates the HTTP server into gateway infrastructure, and formalizes channels as named, first-class interaction contexts with a proper abstract base class and streaming support.

The reference architecture draws from two prior-art projects: **nanobot** (channel message bus, streaming delta model, `BaseChannel` ABC) and **hermes-agent** (gateway as process root, factory-based adapter registration, `BasePlatformAdapter` ABC).

---

## Goals

1. **Gateway as process root** — the gateway owns the harness, actor manager, HTTP server, and channel lifecycle. It's the stable spine that survives actor restarts.
2. **HTTP server as infrastructure** — the REST/WebSocket API is gateway-owned, not a channel. Channels consume the API or the mail route; they don't own the server.
3. **Channels as named interaction contexts** — a channel is a user-facing concept (a "desk," a "device," a "purpose"), not just a transport adapter. Channels have names, display names, and target actors.
4. **`channel_id` as channel identity** — one `channel_id` = one channel = one active connection. Already the pattern in the current WebSocket handler; generalized to all channels.
5. **Streaming support** — channels can optionally support delta streaming (`send_delta`, `send_reasoning_delta`) for real-time response rendering.
6. **Actor-aware routing** — the gateway resolves actor targets per message (via `@mention`, REST path, or channel default), enabling multi-actor routing without mailbox address leakage.
7. **Multi-channel chat portability** — a user can resume the same `chat_id` across different channels (TUI → phone → TUI), because channels are views into a shared conversation graph.

## Non-Goals

1. This BEP does **not** implement full dynamic actor lifecycle (spawn/stop actors at runtime via API). The gateway gains the actor manager structure needed for it, but the initial implementation starts with statically configured actors.
2. This BEP does **not** make channels external processes (Option B). Channels remain in-process with the gateway, but the HTTP API contract is designed so channels can be moved external later.
3. This BEP does **not** redesign the mail route internals. Channels still bind mailboxes; the gateway does not replace the mail route.
4. This BEP does **not** implement multi-user support. BOS remains single-user. Channels are multiple access points for one user.
5. This BEP does **not** add channel-level plugin/hook support — that belongs in a future BEP.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         Process                              │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   Gateway                             │   │
│  │  (process root — holds the asyncio event loop)        │   │
│  │                                                       │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  │   │
│  │  │  HTTP Server │  │    Actor     │  │   Channel   │  │   │
│  │  │  (aiohttp)   │  │   Manager    │  │   Manager   │  │   │
│  │  │              │  │              │  │             │  │   │
│  │  │  REST / WS   │  │  spawn       │  │  start_all  │  │   │
│  │  │  /api/*      │  │  restart     │  │  stop_all   │  │   │
│  │  │  /ws         │  │  stop        │  │  dispatch   │  │   │
│  │  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘  │   │
│  │         │                 │                  │         │   │
│  │         │          ┌──────┴──────┐           │         │   │
│  │         │          │   Actors    │           │         │   │
│  │         │          │             │           │         │   │
│  │         │          │ agent@main  │           │         │   │
│  │         │          │ agent@coder │           │         │   │
│  │         │          └──────┬──────┘           │         │   │
│  │         │                 │                  │         │   │
│  │         │          ┌──────┴──────────────────┴──┐      │   │
│  │         │          │        MailRoute            │      │   │
│  │         └──────────┤    (shared transport)       │      │   │
│  │                    └─────────────────────────────┘      │   │
│  │                                                         │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │                 Channels                          │   │   │
│  │  │                                                   │   │   │
│  │  │  TelegramChannel  SlackChannel  WSChannel(dynamic)│   │   │
│  │  │  (persistent)     (persistent)  (per-connection)  │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  External:                                                       │
│    TUI ◀──WS──▶ HTTP Server (creates dynamic WSChannel)          │
│                                         └──▶ MailRoute ◀──▶ Actor "main"     │
│    SDK ──REST──▶ HTTP Server ──▶ Actor "coder"                   │
│    SDK ◀──Webhook── HTTP Server (callback)                       │
│    Telegram ◀──▶ TelegramChannel ◀──▶ MailRoute ◀──▶ Actor      │
│    Slack   ◀──▶ SlackChannel   ◀──▶ MailRoute ◀──▶ Actor        │
└─────────────────────────────────────────────────────────────────┘
```

### Key architectural rules

1. **Gateway is the process root.** It owns the event loop. The runner bootstraps the gateway and exits — the gateway keeps the process alive.
2. **Gateway is not a mail participant.** It owns the mail route but does not send or receive on it. Actors and channels are peers on the mail route.
3. **HTTP Server is infrastructure.** It's the universal entrypoint for WebSocket and REST clients. It does not register as a channel.
4. **Channels declare their capabilities.** A channel advertises what its transport supports: `inbound` (user → agent), `outbound` (agent → user), or both. WebSocket and chat platforms are naturally duplex. REST POST is inbound-only (client makes an API call, no return path). Webhook is outbound-only (gateway pushes to a client's callback URL, no user input path). The channel's transport determines what's possible — no forced duplex.
5. **Channels are mail route peers.** Each channel binds a mailbox and bridges between an external platform and the mail route. Channels live in the gateway process.
6. **WS connections are dynamic channels.** Each successful WebSocket handshake at `/ws` creates a channel instance that lives for the duration of that connection. The channel is keyed by `channel_id`.

---

## Channel Design

### `BaseChannel` — Abstract Base Class

Inspired by nanobot's `BaseChannel` and hermes-agent's `BasePlatformAdapter`.

#### Message types

```python
@dataclass
class OutboundMessage:
    """A complete message to deliver through a channel."""
    chat_id: str
    content: str
    content_type: str = "message"      # MessageType
    metadata: dict | None = None
    reply_to: str | None = None        # platform-specific reply target

@dataclass
class SendResult:
    """Result of a send operation."""
    success: bool
    message_id: str | None = None      # platform-assigned ID
    error: str | None = None
    retryable: bool = False
```

#### Base class

```python
from abc import ABC, abstractmethod
from typing import Any

class BaseChannel(ABC):
    """Abstract base class for all channel implementations.

    A channel is a named interaction context that bridges an external
    client (or platform) to one or more actors via the mail route.

    Channels declare what their transport supports via the ``capabilities``
    class attribute:

      - ``"inbound"`` — can receive user messages and forward to the actor.
        Requires ``start()`` to be implemented (listening loop).
      - ``"outbound"`` — can deliver agent responses and agent-initiated
        pushes to the external client. Requires ``send()`` to be implemented.
      - ``{"inbound", "outbound"}`` — fully duplex (WebSocket, Telegram, etc.).

    A REST-only client has ``{"inbound"}`` (fire-and-forget, no reply path).
    A webhook (push notification) has ``{"outbound"}`` (gateway POSTs to a
    client callback URL, no user input path).
    A REST + webhook pair can form one duplex channel: the client sends via
    REST, the gateway pushes responses via webhook. Two simplex transports,
    one logical channel. The transport determines what's possible — no
    forced duplex.
    """

    # ── Class-level identity ──

    name: str = "base"               # unique identifier (e.g., "telegram", "tui")
    display_name: str = "Base"       # human-readable label
    kind: str = "persistent"         # "persistent" (config-driven) or "dynamic" (per-connection)
    capabilities: set[str] = {"inbound", "outbound"}  # what this transport supports

    # ── Required abstract methods ──

    @abstractmethod
    async def start(self) -> None:
        """Start the channel and begin listening for inbound messages.

        Long-running. Must not return until the channel is stopped.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Cleanly shut down the channel. Release all resources."""
        ...

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> SendResult:
        """Send a complete message through the channel.

        Subclasses must raise on delivery failure so the channel manager
        can apply retry policy.
        """
        ...

    # ── Optional streaming methods ──

    async def send_delta(self, chat_id: str, delta: str, metadata: dict | None = None) -> None:
        """Deliver a streaming text chunk. Override to enable streaming.

        Stateful implementations must key buffers by stream_id (from metadata),
        not only by chat_id, to support concurrent streams.
        """
        pass

    async def send_reasoning_delta(self, chat_id: str, delta: str, metadata: dict | None = None) -> None:
        """Deliver a streaming reasoning/thinking chunk. No-op by default."""
        pass

    async def send_reasoning_end(self, chat_id: str, metadata: dict | None = None) -> None:
        """Signal end of a reasoning segment. No-op by default."""
        pass

    # ── Computed properties ──

    @property
    def supports_streaming(self) -> bool:
        """True if the subclass overrides send_delta (auto-detected)."""
        return type(self).send_delta is not BaseChannel.send_delta

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Auth and routing ──

    @property
    @abstractmethod
    def channel_id(self) -> str:
        """The unique channel_id for this channel instance."""
        ...

    @property
    @abstractmethod
    def target_actor(self) -> str:
        """The default actor name for messages without explicit routing."""
        ...
```

### `ChannelManager` — Orchestration

Inspired by nanobot's `ChannelManager`.

```python
class ChannelManager:
    """Manages channel lifecycle, routes outbound messages, handles retry."""

    channels: dict[str, BaseChannel]    # keyed by channel_id

    async def start_all(self) -> None: ...
    async def stop_all(self) -> None: ...
    async def start_channel(self, channel: BaseChannel) -> None: ...
    async def stop_channel(self, channel_id: str) -> None: ...

    # Outbound routing
    async def dispatch_outbound(self, env: Envelope) -> None:
        """Route an outbound envelope to the correct channel(s).

        Uses env.metadata.routing.channel_id to target a specific channel.
        Falls back to chat_id-based routing for broadcast or no-target cases.
        """

    # Retry with exponential backoff
    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> SendResult:
        """Send with configurable retry. Default backoff: 1s, 2s, 4s."""
```

### Channel Kinds

| Kind | Created by | Lifetime | Typical capabilities | Examples |
|------|-----------|----------|---------------------|----------|
| `persistent` | Config → gateway spawn at startup | Process lifetime | `{inbound, outbound}` | TelegramChannel, SlackChannel |
| `persistent` | Config → gateway spawn at startup | Process lifetime | `{inbound, outbound}` | WebhookChannel (REST in + webhook out) |
| `dynamic` | WebSocket handshake at `/ws` | Connection lifetime | `{inbound, outbound}` | TUI session |

Both kinds share the same `BaseChannel` interface. The `capabilities` set determines which methods must function: `inbound` requires `start()`, `outbound` requires `send()`. A duplex channel can be implemented over a single bidirectional transport (WebSocket) or a pair of simplex transports (REST + webhook).

REST POST `/api/actors/{name}/send` is not a channel — it's a direct gateway API call. No session, no channel_id, no mailbox binding. The gateway delivers the envelope and returns 202.

### Channel Identity: `channel_id`

Every channel has a `channel_id`, which uniquely identifies the transport. For platform channels, it's derived from the platform identity. For connections without a natural platform identity (WebSocket, TUI), it's set explicitly. Within a channel, `chat_id` identifies the conversation context. One channel can handle multiple chats:

```
TelegramChannel  → channel_id = "telegram:{bot_id}"     (derived from token)
                    chats: 123456, 789012…              (one bot, many users)
SlackChannel     → channel_id = "slack:{app_id}"        (derived from app_id)
                    chats: C001, C002…                  (one app, many channels)
FeishuChannel    → channel_id = "feishu:{app_id}"       (derived from app_id)
TUI session      → channel_id = "tui-1"                 (explicit, no platform identity)
```

Rules:
- One `channel_id` = one active channel instance
- Duplicate `channel_id` on WebSocket → rejected (HTTP 409) unless takeover
- Channels route outbound messages via `env.metadata.routing.channel_id`

---

## Gateway Design

### Lifecycle

```
boscli start
  │
  ├─ 1. Bootstrap: load config (RootConfig), inject envs, load extensions
  ├─ 2. Create harness: mail route, chat store, consolidator, LLM client
  ├─ 3. Gateway.__init__(harness, runtime_config)
  │     ├─ HTTP Server starts (aiohttp, listens on configured host:port)
  │     ├─ Actor Manager spawns configured actors
  │     └─ Channel Manager spawns configured persistent channels
  │
  ├─ 4. Runner exits. Gateway holds the event loop.
  │
  │  ... runtime (potentially weeks/months) ...
  │
  ├─ Actor crash → Gateway restarts actor (if restart_on_error configured)
  ├─ SIGTERM → Gateway drains in order:
  │     1. Stop accepting new connections (HTTP 503)
  │     2. Drain in-flight actor turns
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
│   ├── POST /api/actors/{name}/send
│   ├── POST /api/upload-image
│   └── WS   /ws?channel_id={id}&chat_id={id}
├── actor_manager: ActorManager
│   ├── actors: dict[str, AgentActor]
│   ├── spawn(name, agent_kind) → AgentActor
│   ├── stop(name)
│   └── restart_policy: dict[str, RestartPolicy]
├── channel_manager: ChannelManager
│   ├── channels: dict[str, BaseChannel]  (keyed by channel_id)
│   ├── start_all()
│   ├── stop_all()
│   └── dispatch_outbound(env)
└── harness: AgentHarness
    ├── mail_route
    ├── chat_store
    ├── consolidator
    └── llm_client
```

### HTTP API Contract

The gateway exposes a stable HTTP/WebSocket API. This is the public contract that the TUI, future SDKs, and future external channels depend on.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/status` | Health check (actor status, uptime, channel count) |
| `GET` | `/api/actors` | List named actors with display names and agent kinds |
| `POST` | `/api/actors/{name}/send` | Fire-and-forget message to a named actor |
| `POST` | `/api/upload-image` | Upload image, return path-backed image part |
| `WS` | `/ws?channel_id={id}&chat_id={id}` | Bidirectional session to the default actor |

**WebSocket routing:** The `/ws` endpoint binds to the runtime's default actor. Per-message routing to other actors is done via `@mention` in message content (e.g., `@coder fix this bug`). The gateway inspects content for `@actor_name` patterns and routes accordingly. There is no `?actor=` query parameter — the runtime config already provides the default.

**Command routing:** `/new` and `/resume` commands that change `chat_id` trigger chat-state cursor updates and close other clients on the same chat (same behavior as today's HttpChannel, but owned by the gateway dispatcher).

### Actor Manager

```python
class ActorManager:
    """Manages actor lifecycle within the gateway."""

    actors: dict[str, AgentActor]       # actor_name → AgentActor instance
    configs: dict[str, ActorConfig]     # from runtime.actors
    restart_policies: dict[str, RestartPolicy]

    async def start_all(self) -> None:
        """Spawn all configured actors."""

    async def spawn(self, name: str, agent_kind: str) -> AgentActor:
        """Create and start a new actor. Returns the running AgentActor."""

    async def stop(self, name: str) -> None:
        """Gracefully stop an actor."""

    async def restart(self, name: str) -> AgentActor:
        """Stop and re-spawn an actor."""
```

### Routing Flow

```
Inbound (REST — fire-and-forget):
  POST /api/actors/{name}/send arrives
    → Gateway resolves actor name (path param) or uses runtime.agent
    → Gateway delivers envelope directly to actor's mailbox (no mail route hop)
    → Actor processes, no response expected
    → HTTP 202 returned

Inbound (WebSocket — session):
  WS /ws?channel_id={id}&chat_id={id} handshake completes
    → Gateway creates a dynamic WSChannel(keyed by channel_id)
    → WSChannel binds a mailbox on the mail route
    → Each WS message:
        → Gateway inspects content for @mention
        → If target actor found: delivers via mail route to that actor
        → If not: delivers via mail route to default actor (runtime.agent)
        → Actor processes, response goes to mail route
        → WSChannel reads response from its own mailbox
        → WSChannel delivers response to the WebSocket client

Inbound (Persistent Channel — Telegram, Slack, etc.):
  Message arrives via channel's external platform
    → Channel publishes envelope to mail route, addressed to target_actor
    → Actor processes, response goes to mail route
    → Channel reads response from its own mailbox
    → Channel delivers response back to the external platform
```

Key: the gateway only writes directly for one-shot REST calls that have no session. For anything session-based (WS, persistent channels), the channel has its own mailbox and the mail route handles delivery. The gateway is not a mail participant — it owns the mail route, but dynamic channels (which the gateway spawns) are peers on it.

---

## Configuration

All config stays under `[runtime]` (user preference). The HTTP server is gateway-owned, not a channel. Channels get their own structured config.

```toml
[runtime]
agent = "main"                 # default agent kind
location = "process"

# ── Gateway / HTTP server (infrastructure) ──

[runtime.gateway]
host = "127.0.0.1"
port = 5920
upload_dir = ".bos/uploads/http"
max_upload_bytes = 20971520    # 20 MiB

# ── Actors (statically configured for now) ──

[runtime.actors.main]
agent = "main"
display_name = "Main"
restart_on_error = true
max_restarts = 5

[runtime.actors.coder]
agent = "researcher"
display_name = "Coder"

# ── Persistent channels ──

[[runtime.channels]]
name = "TelegramChannel"
display_name = "Daily Chat"
target_actor = "main"
token = "12345:abcdef"
# channel_id derived as "telegram:12345" from token

[[runtime.channels]]
name = "TelegramChannel"
display_name = "Invest Advisor"
target_actor = "main"
token = "67890:ghijkl"
# channel_id derived as "telegram:67890"

[[runtime.channels]]
name = "SlackChannel"
display_name = "Work Desk"
target_actor = "main"
app_id = "A01234567"
token = "xoxb-..."
# channel_id derived as "slack:A01234567"
```

### Channel config fields

| Key | Type | Required | Purpose |
|-----|------|----------|---------|
| `name` | `str` | yes | Channel class name registered on `ep_channel` |
| `display_name` | `str` | no | Human-readable label for UI listing |
| `channel_id` | `str` | no | Explicit channel identity. When omitted, derived from the platform identity: `"telegram:{bot_id}"`, `"slack:{app_id}"`, `"feishu:{app_id}"`. Set explicitly for transports without a natural platform identity (e.g., `"tui-1"` for a WebSocket connection). |
| `target_actor` | `str` | no | Default actor for messages without explicit routing. Falls back to `runtime.agent` |
| *(extra)* | `any` | varies | Per-channel-adapter configuration passed through to the channel constructor |

### Gateway config fields

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `host` | `str` | `"127.0.0.1"` | Bind address for the HTTP server |
| `port` | `int` | `5920` | Listen port (`0` = auto-assign) |
| `upload_dir` | `str` | `".bos/uploads/http"` | Directory for uploaded images |
| `max_upload_bytes` | `int` | `20971520` | Max upload size in bytes |

---

## What Moves Where

| Current location | Concern | Moves to |
|------------------|---------|----------|
| `HttpChannel._build_app()` | aiohttp app creation, routes | `Gateway` or `HttpServer` component |
| `HttpChannel.run()` | Server startup, listen loop | `Gateway` HTTP server lifecycle |
| `HttpChannel._ws_handler()` | WebSocket connection handling | `Gateway` (dynamic channel factory) |
| `HttpChannel._send_handler()` | REST send endpoint | `Gateway` |
| `HttpChannel._dispatch_to_clients()` | Central outbound dispatch to WebSocket clients | Eliminated — each channel reads its own mailbox and delivers independently. `ChannelManager.dispatch_outbound()` only routes mail to the correct channel; the channel handles delivery. |
| `HttpChannel._status_handler()` | Health check endpoint | `Gateway` |
| `HttpChannel._actors_handler()` | Actor listing endpoint | `Gateway` |
| `HttpChannel._upload_image_handler()` | Image upload endpoint | `Gateway` |
| `HttpChannel._cleanup_runtime_state()` | Shutdown cleanup | `Gateway` shutdown sequence |
| `WS_TAKEOVER_CLOSE_CODE` constant | Shared contract | `bos.protocol` |
| `HttpChannelClient` (in `http_client.py`) | Client library | **Stays as-is** (it's a client, not infrastructure) |

---

## Comparison with Reference Implementations

### nanobot (HKUDS)

| Aspect | nanobot | BOS (BEP 7) |
|--------|---------|-------------|
| Channel ABC | `BaseChannel`: `start()`, `stop()`, `send()` | Same shape. Adds `channel_id`, `target_actor`, `display_name` |
| Streaming | `send_delta()`, `send_reasoning_delta()`, `send_reasoning_end()` | Same API, same auto-detection via `supports_streaming` |
| Orchestration | `ChannelManager` — discovery, start_all, dispatch, retry | Same pattern |
| Message transport | `MessageBus` (publish_inbound / consume_outbound) | MailRoute (mailboxes). Channels are peers, not bus consumers |
| Gateway concept | No dedicated gateway — ChannelManager is the spine | Gateway is the explicit process root |
| Channel identity | Implicit (platform adapter keyed by name) | Explicit: `channel_id` is the universal channel key |
| Multi-chat | Not a stated feature | Core feature: channels are views into shared chats |

### hermes-agent (Nous Research)

| Aspect | hermes-agent | BOS (BEP 7) |
|--------|-------------|-------------|
| Channel ABC | `BasePlatformAdapter`: `connect()`, `disconnect()`, `send()` | `BaseChannel`: `start()`, `stop()`, `send()` — similar but simpler |
| Registration | Factory-based `PlatformRegistry` with 16 integration points | `ep_channel` ExtensionPoint — single registration point |
| Gateway | `GatewayRunner` as process root, owns adapters | Same concept: `Gateway` as process root |
| Inbound routing | `set_message_handler(handler)` callback | MailRoute binding — channels publish to a mailbox, actors consume |
| Plugin path | `plugin.yaml` + `adapter.py` → `ctx.register_platform()` | `ep_channel` already supports this via extension modules |
| Streaming | Draft streaming via `send_draft()`, typing indicators | `send_delta()` / `send_reasoning_delta()` — simpler, channel-agnostic |
| Channel identity | Platform enum + session key | `channel_id` — more general, works for both persistent and dynamic channels |
| Integration surface | 16 touch points for a new platform | 1 touch point: implement `BaseChannel` and register on `ep_channel` |

### What BOS Does Differently (and Should Keep)

| BOS trait | Why it's better for BOS |
|-----------|------------------------|
| `channel_id` as channel identity | Already established, works for both persistent (config-driven) and dynamic (WS-driven) channels |
| MailRoute for transport | Channels are peers, not bus consumers or callback registrants. Simpler mental model |
| `ExtensionPoint` registration | One pattern for everything. `ep_channel` is just another EP, consistent with tools, providers, interceptors |
| Chat portability across channels | `ChatState` with cursor-per-channel_id already handles this. No reference project does it well |
| No 16-point integration checklist | One interface, one registration point. BOS values a small, readable core |
| Streaming auto-detection | nanobot's `supports_streaming` property (check if `send_delta` is overridden) — no config flag needed |

---

## Migration Path

### Phase 1: Extract HTTP server from HttpChannel (no behavior change)

1. Create `Gateway` class with aiohttp server.
2. Move route handlers from `HttpChannel` to `Gateway`.
3. `HttpChannel` becomes a thin wrapper that delegates to `Gateway.run()`.
4. Runner creates `Gateway` instead of iterating `HttpChannel`.
5. All existing tests pass with no API changes.

### Phase 2: Introduce BaseChannel and ChannelManager

1. Add `BaseChannel` ABC and `ChannelManager` to `bos.core`.
2. Convert `TelegramChannel` to inherit from `BaseChannel`.
3. Register channels on `ep_channel` (already done).
4. `ChannelManager` handles lifecycle, dispatch, retry.

### Phase 3: Formalize WS connections as dynamic channels

1. `/ws` handler creates a `WSChannel` (dynamic) per connection.
2. `WSChannel` is keyed by `channel_id` and registers with `ChannelManager`.
3. Outbound dispatch routes to the correct channel via `channel_id`.

### Phase 4: Gateway owns the event loop

1. Runner bootstraps gateway and exits.
2. `SIGTERM` handling moves to gateway.
3. Actor restart works without dropping WebSocket connections.

### Compatibility notes

- `HttpChannelClient` (used by TUI) does not change at all. It connects via WebSocket, same as today.
- `WS_TAKEOVER_CLOSE_CODE` constant moves to `bos.protocol` — one-line import change.
- Existing TOML config works through Phase 1. Phases 2-4 add new config shape under `[runtime]`.

---

## Revision History

| Date | Change | Intention |
|------|--------|-----------|
| 2026-06-01 | Initial draft | Propose gateway/channel separation, BaseChannel ABC, gateway as process root |
