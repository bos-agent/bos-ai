# Embedding BOS

You have an application, and you want an agent inside it. BOS gives you two ways
to do that, and they are genuinely different shapes — not a beginner version and
an advanced one. **Choose before you write code.** Switching later is a rewrite of
your integration layer, because the two modes put the conversation in different
places.

## Which mode

| | **Mode 1 — call the agent** | **Mode 2 — mount the gateway** |
|---|---|---|
| You write | `agent.run(chat_id, text)` | Nothing per turn |
| Who drives a conversation | Your code | BOS's actors and channels |
| Concurrency, queueing, turn takeover | Yours to build | Built in |
| Telegram / Lark / WebSocket clients | No | Yes |
| Install | `pip install bos-ai` | `pip install bos-ai[gateway]` |
| Needs a writable directory | Only if you configure file-backed stores | Always |
| Runnable example | [`examples/embed_sdk.py`](https://github.com/bos-agent/bos-ai/blob/main/examples/embed_sdk.py) | [`examples/embed_gateway_fastapi.py`](https://github.com/bos-agent/bos-ai/blob/main/examples/embed_gateway_fastapi.py) |

Pick **mode 1** if your application already owns the conversation — a CLI, a batch
job, a Slack bot you wrote, an HTTP endpoint that answers one question. You call
the agent the way you call any other async function.

Pick **mode 2** if you want what `boscli gateway start` runs — named actors, chat
coordination, persistent channels, the WebSocket protocol — but inside your own
process and behind your own auth, instead of supervising a second daemon.

---

## Mode 1 — call the agent in your own process

```bash
pip install bos-ai
```

That is the library install. It ships no `boscli` command, no terminal UI and no
gateway; see the [install table](../getting-started.md#install) for the
extras. Configuration is a plain dict, so it can come from your database, your
environment or a control plane — a `config.toml` on disk is the CLI's
convenience, not a requirement.

```python
import asyncio
from bos.sdk import BosApp

CONFIG = {
    "platform": {"extensions": []},
    "agents": {
        "assistant": {
            "system_prompt": "You are a concise assistant.",
            "model": "openai/gpt-4o",
        }
    },
}

async def main() -> None:
    async with BosApp(CONFIG, bos_dir="/var/lib/myapp/.bos") as app:
        agent = app.agent()
        first = await agent.run("chat-42", "hello")
        print(first.output)
        again = await agent.run("chat-42", "what did I just say?")
        print(again.output)

asyncio.run(main())
```

Four things that snippet is showing you:

**`agent()` returns an `Agent`, not a reply.** There is deliberately no
`app.ask()`. `Agent.run()` takes ten parameters — streaming via `event_sink`,
structured output via `schema`, `interrupt`, `llm_args`, `turn_id` and more — and
any one-line façade over it sends you back down a layer the moment you want one
of them. Dropping to the lower layer therefore hands you the *same* objects, not
a different path: `app.harness` and `app.workspace` are exactly the ones `BosApp`
is using.

**Chat continuity is an id, not an API.** Both calls above pass `"chat-42"`, so
the second turn sees the first in its history. There is no session object to
create, hold or close. Use your own conversation key.

**Agents are built once, at `__aenter__`, and cached.** `app.agent()` is
synchronous because of that. Every kind named in `config["agents"]` is ready when
the block opens; with no argument you get the default (the top-level
`default_agent` key, or the only agent, or one named `main`). For a kind that
only an `@ep_agent` factory registers — not named in your config — build it once
with `await app.build_agent("kind")`.

**One `BosApp` per process.** Bootstrap writes `os.environ` and rebuilds the
agent registry, both process-global, so opening a second while the first is live
raises rather than quietly corrupting it. Share the one app: `agent()` and
`run()` are safe to call concurrently across *different* `chat_id`s.

**And one mode per process: mount a gateway or hold a `BosApp`, never both.**
That exclusion is real but unguarded — nothing raises. Mounting a gateway runs
the same bootstrap (on mount, and again on every `POST /api/restart`), which
rebuilds the shared `AgentRegistry` out from under the `BosApp`, and vice versa;
agents already built keep working, but every later `build_agent()`, actor start
or restart resolves against the other side's workspace (`docs/BACKLOG.md` §4).

**Turns on one `chat_id` are yours to serialize.** In mode 1 nothing serializes
them for you. Two turns running at once on the same `chat_id` each assemble
context before the other has committed, so neither sees the other's message and
their writes interleave — with no error and no warning. Queue per conversation,
or reject the second turn, the way your application already handles two requests
for one resource. (Mode 2 does this for you: the gateway's chat coordinator
refuses a second turn on a busy chat with `active_turn`.)

### No extras required

The base install has no LLM client. Register your own provider and you need
nothing else:

```python
from bos.sdk import LLMResponse, ep_provider

@ep_provider(name="myco")
async def my_provider(messages: list[dict], model: str, **kwargs) -> LLMResponse:
    text = await my_client.complete(messages, model)   # your own client
    return LLMResponse(content=text)
```

Then use `"model": "myco/whatever"`. Or install `bos-ai[litellm]` and use the
built-in provider. Likewise, `"platform": {"extensions": []}` stops BOS
auto-loading anything: import only the adapters you want, rather than `bos.exts`,
which loads every built-in.

[`examples/embed_sdk.py`](https://github.com/bos-agent/bos-ai/blob/main/examples/embed_sdk.py)
is all of the above, runnable offline on a base install with no extras — two
turns on one `chat_id`, a stub provider, and an `event_sink` showing the
intra-turn events:

```bash
uv run python examples/embed_sdk.py
```

### If you want less than `BosApp`

`BosApp` is a small object over one function. When you would rather own the
harness yourself — a test, a script, an app with its own lifecycle object —
`open_harness` is the same bootstrap with nothing on top:

```python
from bos.sdk import open_harness

async with open_harness(workspace) as harness:
    agent = await harness.create_agent(kind="assistant")
```

Take `workspace` from `app.workspace`, or build one yourself with
`Workspace(workspace=".", bos_dir=..., config=...)`. The order inside
`open_harness` is the contract: agent files are loaded before the platform
registers them, and doing it the other way round drops every agent file with no
error. That is why this exists as one function rather than two calls you repeat.

---

## Mode 2 — mount the gateway

```bash
pip install bos-ai[gateway]
```

`GatewayMount` gives you the whole BOS runtime as an ASGI application you mount
inside your own web app. Your host keeps its socket, its middleware and its auth;
BOS never binds a port and never authenticates.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from bos.config import Workspace
from bos.runner import GatewayMount

CONFIG = {...}   # the same dict shape as mode 1, plus [runtime] actors/channels

def make_app() -> FastAPI:
    mount = GatewayMount(
        lambda: Workspace(".", "/var/lib/myapp/.bos", CONFIG),
        public_base_url="https://myapp.example.com/bos",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await mount.start()
        try:
            yield
        finally:
            await mount.stop()

    app = FastAPI(lifespan=lifespan)
    app.mount("/bos", mount.build_app())
    return app
```

**The first argument is a factory, not a workspace.** A restart must re-read
configuration, which means building a new `Workspace` — so `GatewayMount` is
handed a callable it can call again.

**`build_app()` is mounted once, before `start()`, and kept across restarts.** The
`Gateway` object behind it is replaced wholesale by a restart; the ASGI app in
front is stable, so your host mounts it at import time and never re-mounts.

**`start()` and `stop()` belong in your host's lifespan.** They are what acquire
and release the singleton lock and bring the actors and channels up and down.

**`public_base_url` is yours to supply.** You chose the socket and the mount path,
so the gateway cannot discover its own URL. Passing it is what lets the status
snapshot carry a usable `base_url` for clients and for `boscli gateway restart`.

**`bos_dir` must be a real, writable path.** The singleton lock, `gateway.state`
and the channel cursors live in `<bos_dir>/run/`, so a mounted gateway cannot be
entirely in memory even when the chat store is.

Mounted at `/bos`, you get `GET /bos/api/status`, `GET /bos/api/actors`,
`POST /bos/api/restart`, the upload endpoints and `WS /bos/ws`. `POST
/api/restart` re-reads configuration and rebuilds the runtime in place — new
channels go live, a deleted agent stops being callable — without dropping your
host process. `mount.status()` returns the same snapshot that route serves, so
your own health endpoint can expose it directly.

[`examples/embed_gateway_fastapi.py`](https://github.com/bos-agent/bos-ai/blob/main/examples/embed_gateway_fastapi.py)
is a complete host, runnable offline:

```bash
pip install bos-ai[gateway] fastapi uvicorn
uvicorn --factory examples.embed_gateway_fastapi:make_app --port 8123
curl -s http://127.0.0.1:8123/bos/api/status
curl -s -X POST http://127.0.0.1:8123/bos/api/restart
```

!!! warning "BOS performs no authentication"
    A mounted gateway is protected by whatever the host puts in front of it. A
    standalone gateway binds `127.0.0.1` by default; if you bind elsewhere, you
    front it yourself.

---

## What both modes share

Almost everything. The two modes differ only in who drives a turn — below that
line, it is one system:

- **Configuration** — the same schema in both, dict or TOML. See the
  [Configuration reference](../configuration/index.md).
- **Agents** — the same `[agents.<kind>]` tables and the same `@ep_agent`
  factories. Mode 2 additionally maps agents to named *actors*; mode 1 never
  touches the actor table.
- **Tools, plugins, providers, stores** — the same `@ep_tool`, `@ep_plugin`,
  `@ep_provider`, `@ep_chat_store` registrations, resolved the same way. See
  [Extending BOS](../extending/index.md).
- **Memory and skills** — the same consolidation and the same skill directories.
  See [Memory & skills](../concepts/memory-and-skills.md).

So an extension you write for a mode-1 embed keeps working unchanged if you later
mount the gateway. What you would rewrite is your own integration layer — the
code that decides when a turn happens.

---

## The contract

`bos.sdk.__all__` is the promise. If a name is in it, it is supported; if it is
not, it may move:

```bash
python -c "import bos.sdk; print(len(bos.sdk.__all__))"
```

It holds 34 names: `BosApp` and `open_harness`; the agent surface (`Agent`,
`AgentHarness`, `AgentResult`, `Message`, `TurnContext`); the ports you can
implement (`LLM`, `ChatStore`, `Consolidator`, `ToolSet`, `TurnInterceptor`,
`PromptProvider`, `TurnEventSink`) together with every type those ports' own
methods take or return; the nine `ep_*` extension points; and `Workspace`,
`RootConfig`, `validate_config`.

That last point is enforced, not aspirational: a test walks every Protocol in
`__all__` and fails if any type in one of its method signatures is unpromised. So
you can implement any port in `bos.sdk` using only names from `bos.sdk` — writing
a `ChatStore` never sends you hunting in `bos.core` for `ChatCommit` or
`TokenEstimate`.

`bos.sdk` re-exports; it does not redefine. `bos.sdk.Agent` **is**
`bos.core.Agent`, one class and one `isinstance` answer.

Everything else remains importable — including the `_`-prefixed helpers
`bos.core` re-exports for extension authors — and is **explicitly unstable**.
`GatewayMount` is mode 2's entry point and lives in `bos.runner`, outside the
contract, because it depends on the `[gateway]` extra that `bos.sdk` must not
require.

## Where to go next

- **[Configuration](../configuration/index.md)** — every key the dict accepts.
- **[Extending BOS](../extending/index.md)** — tools, plugins, channels, providers.
- **[Runtime & gateway](../concepts/runtime.md)** — what mode 2 is actually running.
- **[CLI](../cli/index.md)** — `boscli ask` and the gateway commands, for driving
  the same project from a terminal.
