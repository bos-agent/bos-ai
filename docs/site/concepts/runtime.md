# Runtime & gateway

The gateway is the process that makes BOS a persistent, multi-client service. This
page explains what it is, how it starts up, and how a message travels from an
external client to the LLM and back.

See [Concepts overview](index.md) for the vocabulary table. For exact configuration
keys see [Configuration](../configuration/index.md).

---

## What the gateway is

`boscli gateway start` boots a long-lived process that:

1. Builds the **harness** — starts shared services (chat store, consolidator, job
   runner, mail route) and populates the extension registry.
2. Registers an **actor** for every `[runtime.actors.<name>]` entry in
   `config.toml`. Each actor is an addressable mailbox bound to an agent kind.
3. Starts each configured `[[runtime.channels]]` — persistent loops that bridge
   external clients to actor mailboxes.
4. Serves an **HTTP control plane** on `[runtime.gateway].host:port`, authenticated
   by the key in the env var named by `api_key_env` (default `BOS_GATEWAY_API_KEY`).
5. Writes a **`gateway.state`** file (in the `bos_dir`) so that `boscli tui`,
   `boscli gateway status`, and HTTP clients can discover the actual port and PID
   without pre-configuration.

```toml
[runtime.gateway]
host        = "127.0.0.1"
port        = 0                      # 0 = auto-assign; actual port in gateway.state
api_key_env = "BOS_GATEWAY_API_KEY"
```

!!! note "Port zero and discovery"
    `port = 0` lets the OS pick a free port. The gateway writes the assigned port
    to `gateway.state` immediately after binding, so other `boscli` commands pick
    it up automatically. You only need a fixed port if you are reverse-proxying or
    firewalling the control plane.

---

## The harness and its services

The **harness** (`bos.core.harness.AgentHarness`) is the lifecycle owner of the
services every agent relies on. You choose which implementation to use by name in
the `[harness]` section; the harness instantiates each by name at startup.

```toml
[harness]
consolidator = "LLMConsolidator"    # drives off-turn memory consolidation
chat_store   = "JsonlChatStore"     # conversation persistence + context assembly
mail_route   = "JsonlMailRoute"     # actor-to-actor and actor-to-channel message routing
job_runner   = "InProcJobRunner"    # off-critical-path background jobs
interceptors = []                   # ordered list of turn interceptors (names or inline tables)
```

The defaults shown above are the built-in implementations. Swap any of them by
registering an alternative at the matching extension point and naming it here.

**What each service does:**

- **`chat_store`** — persists and assembles conversation context. It owns token
  estimation, summary injection, and tool-noise filtering. The default
  `JsonlChatStore` stores messages under `.bos/messages/`.
- **`consolidator`** — summarises history and consolidates memories. Called
  off-turn by the job runner so it does not block responses.
- **`mail_route`** — point-to-point message routing between actors and channels.
  The default `JsonlMailRoute` stores mailbox state under `.bos/mailboxes/`.
- **`job_runner`** — runs background jobs (such as memory consolidation). The
  default `InProcJobRunner` runs in the same process.
- **`interceptors`** — an ordered chain of `TurnInterceptor` instances called
  around every agent turn. Plugin interceptors run ahead of this chain.

Configure any service's implementation details via `[exts.<ep_name>.<impl_name>]`:

```toml
[exts.ep_chat_store.JsonlChatStore]
store_dir = "./messages"            # path relative to bos_dir

[exts.ep_consolidator.LLMConsolidator]
model = "gemini/gemini-2.5-flash"  # cheaper model for consolidation work
```

---

## Actors

An **actor** is a named, addressable runtime instance that runs an agent. Define
actors in `[runtime.actors.<name>]`:

```toml
[runtime.actors.main]
agent        = "main"        # which agent kind this actor runs
display_name = "Main"
# restart_on_error = true
# max_restarts     = 5
```

- The TOML key (`main` here) is the actor's **identity** — it is used as the
  address (`agent@main`), the memory scope for `MemoryPlugin`, and the mention
  target (`@main`) in channels.
- `runtime.main_actor` names the default actor; channels that do not specify
  `target_actor` route there.
- Actors are long-lived and restartable; if a turn raises an error and
  `restart_on_error` is true, the actor reinitialises.
- Per-actor agent overrides (model, plugins, bindings, …) go under
  `[runtime.actors.<name>.agent_cfg]`.

---

## Channels

A **channel** is a long-lived loop that bridges an external client protocol to an
actor's mailbox. Add channels as an array of tables:

```toml
[[runtime.channels]]
type         = "TelegramChannel"          # registered ep_channel implementation
channel_id   = "telegram+main"            # unique identifier for this channel instance
display_name = "Telegram"
target_actor = "main"                     # must be a defined actor
settings     = { token_env = "TELEGRAM_BOT_TOKEN" }
```

Each channel runs independently for the life of the gateway. The `type` field
names a registered `ep_channel` implementation. The `settings` table is passed to
the channel factory; by convention, secrets are stored as env-var names (`token_env`)
rather than inline values.

Built-in channels: `TelegramChannel`, `LarkChannel` (requires `bos-ai[lark]`).

---

## End-to-end message flow

```
external client (TUI, Telegram, …)
        │
        ▼
  channel.run(mailbox)          ← long-lived loop
        │  posts Envelope
        ▼
  actor mailbox                 ← MailRoute.bind(address)
        │  actor dequeues
        ▼
  agent turn
    ├── system prompt assembly  ← plugins contribute sections
    ├── tool calls (0..N)       ← tools, plugins, subagents
    └── LLM completion          ← ep_provider dispatch
        │
        ▼
  reply Envelope
        │  delivered via MailRoute
        ▼
  channel pushes reply to client
```

1. The channel receives external input (a Telegram message, a TUI keystroke, an
   HTTP POST) and posts it as an `Envelope` into the target actor's mailbox.
2. The actor dequeues the envelope and runs its agent turn: plugins contribute
   system-prompt sections, tools are called as needed, and the LLM produces a
   response.
3. The reply is delivered back through the `MailRoute` and the channel pushes it
   to the client.

Turn interceptors (from `[harness].interceptors` and from plugins) run
around steps 2–3. An interceptor may inspect, mutate, or abort a turn.

---

## `boscli ask` vs the gateway

**`boscli ask`** is a convenience command for one-shot queries. It builds the
workspace and harness in-process, runs a single agent turn, and prints the reply to
stdout (progress streams to stderr on a TTY). It does **not** start the gateway or
any channels — there is no `gateway.state`, no persistent mailbox, and no HTTP
control plane.

| | `boscli ask` | `boscli gateway start` |
|---|---|---|
| Process lifetime | One turn, then exits | Long-lived, until stopped |
| Channels | None | All configured channels |
| HTTP control plane | No | Yes |
| `gateway.state` | Not written | Written on startup |
| Use case | Scripts, quick queries, CI | Interactive use, bots, multi-client |

`boscli ask` honours `--model` / `BOS_MODEL` per invocation and accepts
`--agent <kind>` to select a specific agent spec.

---

## Bootstrap order

When the gateway starts (or when `boscli ask` builds its workspace), the following
sequence runs:

1. **Env**: `[platform.envs]` applied to `os.environ`, then `[platform.envfile]`
   loaded with override.
2. **Extensions**: each `[platform].extensions` entry loaded — as a path if it
   exists relative to `bos_dir`, otherwise as a module name. `bos.exts` imports
   all built-ins and discovers the `bos.exts` entry-point group.
3. **Extension defaults**: `[exts.<ep>.<impl>]` tables are deep-merged into the
   corresponding extension's defaults.
4. **Agents**: every `@ep_agent` factory is invoked once; specs are merged
   (`[agent.defaults] → factory → [agents.<name>]`) and registered.
5. **Harness open**: built-in adapters self-register, named services are
   instantiated, the event bus and job runner start.
6. **Gateway bind**: actors are registered, channels started, HTTP server bound,
   `gateway.state` written.

Agents themselves are built lazily — a `create_agent` call is deferred until the
first message arrives for that actor.
