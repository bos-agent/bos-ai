# BEP 17: ASGI-Mountable Gateway and In-Process Lifecycle

- **Status:** Draft
- **Depends on:** BEP 7 (gateway/channel architecture — this BEP re-homes its lifecycle, not its protocol), BEP 13 (concentric rings — the ring guards constrain where the lock and the mount may live), BEP 16 (extras and the embeddable-library direction; this BEP is its declared Track B), BEP 6 (configuration architecture)
- **Supersedes:** BEP 7 § *Gateway Authentication* (§3.8 removes API-key authentication outright)
- **Blocked by:** nothing.

---

## 1. Motivation

BEP 16 made `pip install bos-ai` a library install and declared Track B — an ASGI-mountable gateway — as scoped but undesigned. This BEP designs it.

Embedding BOS has two shapes:

1. **Call the agent in-process.** Build a `Workspace` from a dict, open a harness, call `agent.run()` from your own request handler. BEP 16 §3.6 delivered this; `examples/embed_fastapi.py` is the worked example.
2. **Mount the gateway.** Get the whole BEP 7 runtime — named actors, chat coordination, the WebSocket protocol, persistent channels — inside your own web service, so your application can offer a conversation with the agent without supervising a second process.

Shape 2 is impossible today for three reasons, each concrete:

1. **`Gateway` owns the socket.** [`gateway.py` `run()`](../../src/bos/gateway/gateway.py) builds an `aiohttp.web.AppRunner`, binds a `TCPSite`, reads the bound port back off `site._server.sockets[0]`, and only returns when shutdown completes. A host that already owns its own socket cannot use any of it.
2. **The protocol surface is `aiohttp.web`, which is not ASGI.** There is no mount path into FastAPI, Starlette or Litestar.
3. **Applying a config change means killing the process.** `boscli gateway restart` is stop-the-process-and-start-a-new-one ([`agent.py:694`](../../src/bos/cli/commands/agent.py)). You cannot kill the host's process to pick up a new agent definition.

### 1.1 What this BEP is *not* fixing

Storage seams (chat history, channel→chat cursors, memory watermark, uploaded attachments) were surveyed and deliberately left alone — see §2.2.1. The single-instance guard was considered for removal and deliberately **kept** — see §3.4.

---

## 2. Goals and Non-Goals

### 2.1 Goals

1. A host ASGI application can mount the gateway's protocol surface. The mounted gateway never binds a socket, never writes a PID file, and never installs a signal handler.
2. The single-instance guard stays inside the library and works in both modes, with an observable `live`/`standby` state and a management surface that explains *why* a gateway is not live.
3. A **hot restart** re-reads configuration, agent definitions and channel configuration and rebuilds the runtime, without restarting the host process — with the reload depth stated precisely (§3.5.2) rather than implied.
4. `aiohttp` leaves the repository. One HTTP stack, server and client.
5. BOS performs **no authentication of its own**. The host owns it.
6. `python -m bos.runner` and an embedded host drive the *same* object, so the standalone path is not a second implementation.
7. Wire protocol, turn semantics, chat revision rules and config keys other than those named in §5 are unchanged.

### 2.2 Non-Goals

1. **Storage ports for gateway state, cursors, uploads, or memory.** A survey of every filesystem write in the library rings found five candidates for host-owned persistence. All are out of scope: BEP 16 §3.5 already established that configuration is storage-agnostic, and the remaining stores work. The one exception is `gateway.state`, which is *not* domain storage — its only readers are `bos.runner.proc` and `bos.cli` (18 call sites), i.e. a second process inspecting a running gateway. §3.4.4 keeps it for exactly that reason.
2. **Dual transport.** Keeping the `aiohttp` server alongside an ASGI app means two server implementations maintained forever, while the aiohttp server's only consumer is `python -m bos.runner`, which can run uvicorn instead.
3. **A `WebSocketLike` protocol or transport adapter layer.** Once aiohttp is gone there is exactly one implementation; an interface with one implementation is the thing BEP 16 §2.2.3 warns against. `WSChannel` is rewritten against Starlette directly.
4. **Making the mount an `ep_plugin`.** See §3.10.
5. **Multi-replica / horizontally scaled hosting.** BOS supports one live gateway per `bos_dir`. §3.4.5 states the precondition.
6. **Reloading Python extension code.** §3.5.2 states what a hot restart can and cannot pick up. Changed Python code always requires a host process restart.
7. **Unsetting environment variables on reload.** §3.5.3.
8. **A `fastapi` dependency.** The gateway produces a plain ASGI application; hosts mount it with their own mount API.

---

## 3. Design

### 3.1 Runtime shape

| Artifact | Runtime form | Lifecycle | Who invokes it |
|---|---|---|---|
| `GatewayMount` (`bos.runner`) | Plain object held by the host | Constructed once; `start()`/`stop()`/`restart()` may be called repeatedly | The host's lifespan hook, or `python -m bos.runner` |
| The ASGI app (`mount.asgi_app()`) | ASGI callable | Built once at construction, outlives every `Gateway` instance behind it | The host's server (or uvicorn, standalone) |
| `Gateway` (`bos.gateway`) | Object rebuilt on every start | Replaced wholesale by a restart | `GatewayMount` only |
| Lock watchdog | `asyncio.Task` owned by `GatewayMount` | Runs while the mount is started | `GatewayMount.start()` |
| `python -m bos.runner` | OS process | Unchanged | `boscli gateway start` via `proc.start_background` |

No new process, job, queue, scheduler or event bus is introduced.

### 3.2 What moves, and what does not

`Gateway.run()` today interleaves four concerns. Only one of them leaves:

| Concern in `Gateway` today | Disposition |
|---|---|
| `web.AppRunner` + `TCPSite` + `site.start()` + port read-back | **Leaves.** The host owns the socket. Standalone, uvicorn owns it (§3.6.5). |
| Persistent channel creation, actor start/stop, drain, teardown ordering | **Stays**, split out of `run()` into `start()` / `stop()` |
| `write_gateway_state(...)` | **Stays** (§3.4.4) |
| Singleton lock acquisition | **Moves in** — from `runner/__main__.py`'s preamble to `GatewayMount` (§3.4.2) |

PID file, `gateway.log` and signal handling are already outside `Gateway` — they live in [`runner/__main__.py`](../../src/bos/runner/__main__.py) and `proc.start_background` — and stay there, used only by the standalone process.

The net change to `Gateway` is therefore small: `run()` becomes `start()` and `stop()`, and the socket goes away. The correctness that `run()` carries — actors stop *before* channels so a turn closing during the drain still has a live consumer, and teardown is re-awaited through `asyncio.shield` so an escalating signal cannot leave sessions behind — is preserved verbatim in `stop()`.

The ordering invariant that actors and channels come up *before* anything serves is no longer enforced inside `Gateway`; it falls out of the caller's order (`await mount.start()` then serve). §7.6 makes it an acceptance criterion so it cannot regress silently.

### 3.3 `GatewayMount`

#### 3.3.1 Why it cannot live in `bos.gateway`

A hot restart must re-read configuration, which requires `bos.config`. [`test_gateway_ring_isolation.py`](../../tests/test_gateway_ring_isolation.py) forbids `bos.gateway` from importing `bos.config`. [`test_runner_ring_isolation.py:26`](../../tests/test_runner_ring_isolation.py) forbids `bos.runner` only from `bos.cli`, `bos.extensions` and `bos.exts` — `bos.config` and `bos.gateway` are both permitted.

`bos.runner` is also already the composition root that builds a `Gateway` from a `Workspace` ([`runner.py:32`](../../src/bos/runner/runner.py)). `GatewayMount` is the same job with a different driver, so it goes there. The package name reads as process-flavoured; it is kept anyway rather than adding a module for a name.

#### 3.3.2 Shape

```python
from bos.runner import GatewayMount

mount = GatewayMount(workspace_factory, public_base_url="https://app.example.com/bos")
app.mount("/bos", mount.asgi_app())      # at import time, before start()

# in the host's lifespan:
await mount.start()
...
await mount.stop()
```

`workspace_factory` is a zero-argument callable returning a freshly built `Workspace`. It is a factory, not a `Workspace`, because a restart must produce a *new* one (§3.5.1). The standalone runner passes a factory that re-runs `resolve_config_source` + `Workspace(...)`; an embedder with an in-memory config dict passes a factory that rebuilds from their own source.

`public_base_url` is supplied by the host. A mounted gateway cannot discover its own URL — the host owns the socket and may add a mount prefix, a reverse proxy, or TLS. When it is absent, the state file omits `gateway.base_url` and `boscli gateway restart` says so explicitly rather than guessing `http://host:port` (§4.3).

#### 3.3.3 The app outlives the `Gateway` behind it

The host mounts once, at import time, and a restart replaces the `Gateway` instance. The app therefore cannot hold a `Gateway`; it holds the mount.

[`create_gateway_app`](../../src/bos/gateway/http.py) already takes `status_provider` and `ws_handler` as callables, so two thirds of this indirection exists. The third argument, `config`, is passed as a *value* today and is read for `upload_dir` and `max_upload_bytes` — both of which a restart may change. It becomes `config_provider: Callable[[], ResolvedGatewayConfig]`.

### 3.4 Single instance

#### 3.4.1 It stays in the library

One live gateway per `bos_dir` is a correctness requirement, not an operational convenience: two gateways on the same run dir poll the same channels twice, deliver the same inbound message twice, and write the same chat concurrently. Pushing that onto the host would mean every embedder re-implements it, and most would not.

#### 3.4.2 The lock helpers move inward

`acquire_singleton_lock`, `lock_still_owned` and `lock_is_free` are in [`bos/runner/proc.py:141/123/174`](../../src/bos/runner/proc.py). They already handle the hard part — `flock` binds to an inode, so a lock file unlinked and recreated between `open()` and `flock()` would let two processes both believe they are the singleton; `lock_still_owned` compares `(st_dev, st_ino)` to detect it.

`GatewayMount` is in `bos.runner`, so it *could* call them where they are. They move to `bos/gateway/state.py` anyway, beside `GatewayRunDir`, because §3.4.3 makes lock ownership part of the gateway's own reported state and `bos.gateway` may not import `bos.runner`. `bos.runner.proc` re-exports them so `bos.cli`'s call sites (`gateway restart`, `gateway stop`) are untouched.

#### 3.4.3 States

| State | Meaning | `/api/status` | `/ws` | `/api/restart` |
|---|---|---|---|---|
| `starting` | acquiring the lock | served | denied, 503 | 409 |
| `live` | holds the lock; actors and channels running | served | served | served |
| `standby` | did not get the lock | **served** | denied, 503 | 409 |
| `restarting` | tearing down and rebuilding | **served** | denied, 503 | 409 |
| `stopped` | after `stop()` | served | denied, 503 | 409 |

`/api/status` is served in every state. A host whose chat UI stops working must be able to learn why; a status route that goes dark in exactly the failure case is useless. In `standby` the payload carries the PID of the process holding the lock, read from `gateway.pid` when present.

Denying `/ws` before the upgrade reuses the existing shape: [`Gateway.handle_ws`](../../src/bos/gateway/gateway.py) already returns `503 {"ok": false, "error": "shutting_down"}` when shutdown has been requested. §3.6.4 covers how that survives the ASGI port.

#### 3.4.4 `gateway.state` stays, and gains `runtime: "embedded"`

The state file's only readers are `bos.runner.proc` and `bos.cli` — a second process inspecting a running gateway. That is what makes it worth keeping in the mounted case too: an embedded gateway that writes it is visible to `boscli gateway status` and `boscli doctor` with no new tooling. `status_snapshot()["runtime"]` is hardcoded `"process"` today. `GatewayMount` takes a `runtime_label` argument defaulting to `"embedded"`; `python -m bos.runner` passes `"process"`. The gateway does not infer it.

The `state_changed` callback that `Gateway` injects into `ActorManager` and `ChannelRuntimeContext` ([`gateway.py:65`, `:78`](../../src/bos/gateway/gateway.py)) is unchanged.

#### 3.4.5 The watchdog becomes bidirectional, and its precondition is stated

[`runner/__main__.py`](../../src/bos/runner/__main__.py) already polls every 5 s (`_LOCK_WATCH_INTERVAL`) and stands down when it loses the lock. The watchdog moves to `GatewayMount` and gains the opposite direction: when the mount is in `standby` and `lock_is_free(rd)` reports the lock acquirable, it acquires and transitions to `live`, starting actors and channels. One task, one interval, one knob. `mount.acquire()` triggers the same transition on demand.

**Precondition, stated rather than implied:** `flock` is advisory and machine-local. The guard holds between processes on one host sharing one real filesystem path. It does **not** hold across containers with separate mounts, across machines, or on network filesystems with unreliable locking, and on platforms without `fcntl` `acquire_singleton_lock` returns an unguarded handle and every instance believes it is the singleton. Running BOS as multiple replicas is unsupported (§2.2.5); this guard is a safeguard against accidental double-start, not a distributed lock.

### 3.5 Hot restart

#### 3.5.1 A restart rebuilds the whole `Gateway`

Not a preference — two code facts force it:

1. [`create_persistent(configs)`](../../src/bos/gateway/channels/channel_manager.py) instantiates channels from the list it is handed, and `Gateway.__init__` captures that list as `self._persistent_channel_configs`. Picking up new `[[runtime.channels]]` entries therefore requires a new `Gateway` built from a new `GatewayRuntimeConfig`.
2. [`ChannelManager.stop_all()`](../../src/bos/gateway/channels/channel_manager.py) cancels tasks and sets `status = "stopped"` but **does not clear `self._channels`**. Reusing the manager and calling `create_persistent` again raises `ChannelFactoryError: Duplicate channel_id` from `register()`. An implementation that "stops and re-creates" on the same manager is therefore wrong, and wrong in a way that only shows on the second restart.

The sequence:

```
stop the current Gateway      # drain within shutdown_grace_seconds, then teardown
close the harness             # AgentHarness.__aexit__ — plugins, interceptors, Closeable adapters
workspace = workspace_factory()
workspace.resolve_agents()
workspace.bootstrap_platform()        # includes AgentRegistry.clear() — see §3.5.4
open a new harness
Gateway(runtime=workspace.resolve_gateway_runtime(), harness=harness)
start it
```

**The lock is held across the restart.** It is the same instance on the same run dir, and releasing it would open a window for another process to take over mid-restart. It is released only on `stop()` or when the watchdog finds it lost.

#### 3.5.2 Reload depth — what a hot restart does and does not pick up

| Changed | Picked up | Why |
|---|---|---|
| `config.toml` — actors, channels, plugin bindings, gateway settings | yes, **except `host`/`port`** | re-read by the new `Workspace`; the socket is bound once by the caller and a restart only rebuilds the `Gateway` behind it, so a changed endpoint needs a host process restart |
| `[[runtime.channels]]` added, removed, or re-configured | yes | `create_persistent` runs against the new config |
| Agent definitions in `agent_dirs` — added or edited | yes | `resolve_agents()` + `AgentRegistry.register` overwrites by name |
| Agent definitions — **deleted or renamed** | yes, **only with §3.5.4** | otherwise the stale registration survives and stays callable |
| A new channel *type* from a newly installed package | **no** | its module must be imported; see below |
| Installed packages and `bos.exts` modules | **no** | [`_load_ext_modules`](../../src/bos/core/_utils.py) uses `importlib.import_module`, a no-op for an already-imported module |
| Project-local `./extensions/*.py` | **no** (by decision) | see below |
| Environment variables — added or changed | yes | `os.environ.update` / `load_dotenv` re-run |
| Environment variables — **removed** | **no** | §3.5.3 |

**Project-local extension files are not re-executed.** [`_load_ext_paths`](../../src/bos/core/_utils.py) *would* re-execute them — it uses `spec.loader.exec_module`, not the module cache. It is excluded anyway, because re-execution is unsafe: a file that constructs its own `ExtensionPoint(...)` hits [`registry.py:58`](../../src/bos/core/registry.py), which raises on a duplicate extension-point name, and `_load_ext_paths` swallows the exception into a warning — so the file silently fails to load and every tool it registered disappears. Making that safe means changing `bos.core.registry`, the innermost ring, for a mounted-mode developer convenience. Out of scope; §8 records it.

One rule, one exception: **configuration and agent definitions reload; Python code does not, and neither does the bound endpoint — that socket belongs to the caller.** The standalone `boscli gateway restart` is unaffected — it still replaces the process, so it still picks up everything. This limit belongs to the mounted mode and takes nothing away that exists today.

#### 3.5.3 Environment variables are not unset

`bootstrap_platform` applies `[platform.envs]` with `os.environ.update` and loads `[platform.envfile]` with `load_dotenv`. Neither removes a key that is gone from the new configuration. Tracking that would mean remembering which keys BOS itself set and diffing them against the host's own environment, which BOS must not clobber. Documented limitation: to clear a variable in a mounted gateway, set it to the empty string rather than deleting it, or restart the host process.

#### 3.5.4 `AgentRegistry` must be cleared, or a deleted agent stays callable

[`AgentRegistry._registry`](../../src/bos/core/harness.py) is a class-level dict. `register()` only writes. After a hot restart in which an agent file was deleted, this chain is live:

1. `resolve_agents()` drops it from `config.agents` — correct so far
2. the registry still holds it from the previous bootstrap
3. [`subagent.py:203`](../../src/bos/plugins/subagent.py) calls `dict(AgentRegistry.describe())` — everything ever registered
4. `_pick_collection(available, enabled, disabled)` keeps it whenever the configured `enabled` list still names it, which is the ordinary case after deleting a file
5. it is rendered into `<available_subagents>` in the system prompt
6. the model calls it; [`subagent.py:158`](../../src/bos/plugins/subagent.py)'s membership check passes; [`harness.py:371`](../../src/bos/core/harness.py) returns `AgentRegistry.get_defaults(kind)` — the **old** definition — and the deleted agent runs

The fix is small because the registry is fully derivable. Its only writer is the loop at [`workspace.py:639-655`](../../src/bos/config/workspace.py), which iterates `{**factory_specs, **config_specs}` where `factory_specs` comes from `ep_agent` and `config_specs` from `config.agents`. Clearing before that loop rebuilds it exactly. `AgentRegistry.clear()` is added and `bootstrap_platform` calls it.

This touches `bos.core`, the innermost ring, and is in scope because without it §7.5 cannot hold. Residual, unchanged by this BEP: an agent registered by `@ep_agent` in Python code that is later deleted from the code survives, because its module stays imported — the same rule as §3.5.2.

### 3.6 ASGI transport

#### 3.6.1 A plain Starlette application

`bos.gateway` produces a Starlette app. FastAPI, Starlette and Litestar hosts all mount an ASGI callable with their own API, so no host framework enters the dependency tree; BEP 16's stance that `fastapi` is example-only is preserved.

Routes map one-to-one, plus one addition:

| Route | Method | Change |
|---|---|---|
| `/api/status` | GET | none |
| `/api/actors` | GET | none |
| `/api/upload-image`, `/api/upload` | POST | §3.6.3 |
| `/ws` | WebSocket | §3.6.4 |
| `/api/restart` | POST | **new** — triggers §3.5 |

#### 3.6.2 No middleware, because there is no authentication

§3.8 removes API-key authentication, which removes the only middleware. This also removes a trap worth recording so it is not reintroduced: Starlette's `BaseHTTPMiddleware.__call__` begins `if scope["type"] != "http":` and passes everything else through, so an authentication middleware written that way would silently leave `/ws` unauthenticated. Any middleware added to this app in future must be a pure ASGI callable handling both `http` and `websocket` scopes.

#### 3.6.3 Uploads: the limit must be carried over explicitly

`aiohttp` bounds the whole request body with `web.Application(client_max_size=config.max_upload_bytes)` and returns 413 automatically.

**Starlette 1.6.0 has the same thing**, contrary to this section's first draft: `starlette.middleware.body_limit.RequestBodyLimitMiddleware` (also reachable as `Starlette(max_body_size=…)` and `Route(max_body_size=…)`). It rejects on `Content-Length` *before* the body is read and also counts the streamed total, answering `413 Content Too Large`. Measured: 500 KB under a 1 MB limit → 200, 2 MB → 413. It passes non-`http` scopes straight through, so `/ws` is untouched and it satisfies §3.6.2's rule for anything added to this app.

**`max_part_size` cannot be that limit.** Its check at `starlette/formparsers.py:184` runs against `self._current_part.data`, which only accumulates for a part *without* a filename; a part carrying one is streamed to a spooled temporary file and never trips it. Measured: a 2 MB file part passes `max_part_size=1_000_000` and returns 200. So the first draft's claim — that a transcribed handler "would silently reject every upload between 1 MB and 20 MB" — is wrong for file uploads, and, worse, relying on `max_part_size` as the enforcement point would have removed `max_upload_bytes` enforcement altogether.

The implementation therefore:

- wraps the app in `_LiveBodyLimit`, a pure ASGI callable that instantiates `RequestBodyLimitMiddleware` per request with `config_provider().max_upload_bytes`;
- still passes `max_files=1, max_fields=1, max_part_size=config.max_upload_bytes` to `Request.form`, which matches the handler (it reads exactly one field named `file`) and bounds a non-file field, while noting that the body limit is what enforces the configured maximum.

**The limit is read per request, not at construction.** Starlette fixes it wherever it is declared, but the app is built once — by an embedder at import time, *before* `start()`, when there is no `Gateway` and therefore no configuration to read (§3.3.3). A limit captured then would be the dataclass default, not the configured value. Per-request reading is also what lets a hot restart change it.

**No named difference from aiohttp.** The bound is the whole request body, as it was, and the configured limit and its default are unchanged.

`store_uploaded_attachment` is already transport-free — it takes `bytes` — and does not change.

#### 3.6.4 WebSocket

`WSChannel` is rewritten against `starlette.websockets.WebSocket` directly (§2.2.3). Mapping, measured against Starlette 1.6.0:

| Today (aiohttp) | Starlette |
|---|---|
| `ws.send_json(payload)` | same name |
| `ws.close(code=X, message=b"...")` | `ws.close(code=X, reason="...")` — **bytes becomes str**; `WS_TAKEOVER_CLOSE_REASON.encode()` loses its `.encode()` |
| `async for msg in ws` + `msg.type == WSMsgType.TEXT` + `msg.json()` | `async for text in ws.iter_text()` + `json.loads(text)` |
| `msg.type in (ERROR, CLOSE, CLOSED)` → break | disconnect raises `WebSocketDisconnect` → `except` and return |
| `while not ws.closed:` | `while ws.client_state is WebSocketState.CONNECTED:` |
| handler returns the `WebSocketResponse`; the coroutine's lifetime is the connection's | same shape; `await ws.accept()` then run the loop, return `None` |

**Pre-accept rejections keep their HTTP shape.** `handle_ws` rejects with `400 channel_id is required`, `409 duplicate_channel_id` and `503 shutting_down` before any upgrade. The ASGI *websocket denial response* extension preserves this exactly; measured against uvicorn 0.53.0 with a `websockets` client, `send_denial_response(JSONResponse({...}, status_code=409))` produced `HTTP 409` with the JSON body intact, readable from `InvalidStatus.response`. Denial response is an optional ASGI extension: `send_denial_response` raises `RuntimeError` on a server that does not advertise it, so the handler falls back to `ws.close(code=...)` and the BEP does not claim the HTTP shape on arbitrary hosts — only on uvicorn, which is what the standalone runner uses.

#### 3.6.5 uvicorn in the standalone runner

`python -m bos.runner` builds a `GatewayMount`, starts it, then serves `mount.asgi_app()` with uvicorn driven programmatically (measured, uvicorn 0.53.0):

- `uvicorn.Config(app, host, port)` + `uvicorn.Server(config)`
- `server.servers[0].sockets[0].getsockname()[1]` yields the bound port, which is what `port = 0` needs for the state file
- `server.should_exit = True` performs a graceful stop

**`Server.serve()` must not be used.** It is `with self.capture_signals(): await self._serve(sockets)`, and `capture_signals` replaces `SIGINT`/`SIGTERM` with uvicorn's own handler whenever it runs on the main thread. That would displace the handler `runner/__main__.py` installs, and uvicorn's handler takes the socket down *before* the drain — the reverse of §3.2's ordering, where a turn closing during the drain still needs a live consumer for its reply. Signals belong to the process driver, not to the server.

`runner.serve()` therefore runs uvicorn's own startup sequence minus that wrapper: `config.load()`, assign `server.lifespan = config.lifespan_class(config)`, `await server.startup()`, read the port back, then `server.main_loop()` as a task raced against the mount's shutdown and demotion waits, and `should_exit` + `server.shutdown()` under the teardown shield. `lifespan="off"`, because the mount is the lifecycle and an ASGI lifespan would be a competing second one.

The runner hands the resolved port back to the mount so `gateway.base_url` reaches the state file as it does today. The two signal paths in `__main__.py` are unchanged in meaning: the first `SIGTERM` requests the drain, a second cancels.

### 3.7 Clients

`GatewayClient` and the Telegram channel are the two `aiohttp` *clients*; ASGI replaces only the server, so they are ported separately.

| Today | Replacement |
|---|---|
| `aiohttp.ClientSession(headers=...)` | `httpx.AsyncClient(headers=..., base_url=...)` |
| `session.ws_connect(url)` | `websockets.asyncio.client.connect(url)` |
| `ws.receive(timeout=5)` | `asyncio.wait_for(ws.recv(), 5)` |
| `aiohttp.FormData()` + `session.post(data=form)` | `client.post(url, files={"file": (name, handle)})` |
| `session.closed` | `client.is_closed` |
| `ClientTimeout(total=N)` | `httpx.Timeout(N)` |
| `ClientSession(base_url=..., raise_for_status=True)` | `httpx.AsyncClient(base_url=...)` + a `raise_for_status` response hook |

Two footguns to carry into implementation: `httpx` joins a `base_url` by URL rules — the base must end with `/` and the relative path must not begin with one, which differs from `aiohttp`'s behaviour and silently drops path segments if transcribed; and `websockets` surfaces a rejected upgrade as `InvalidStatus`, not as a message, which is what §3.6.4's denial responses are read from.

`GatewayClient`'s public method signatures do not change, apart from losing the `api_key` parameter (§3.8).

### 3.8 Authentication is removed

BOS performs no authentication. A mounted gateway is behind the host's own auth; a standalone gateway binds `127.0.0.1` by default ([`config.py:20`](../../src/bos/gateway/config.py)) and an operator who binds elsewhere fronts it themselves.

Removal surface, verified:

| Location | Action |
|---|---|
| `gateway/http.py` | delete `resolve_gateway_api_key`, `api_key_middleware`, `APP_API_KEY` |
| [`gateway/gateway.py:105`](../../src/bos/gateway/gateway.py) | delete the `"auth"` block from `status_snapshot()` — the state-file shape changes and `boscli gateway status` rendering follows |
| `gateway/gateway.py:125-128` | `build_app` loses its `api_key` argument |
| [`gateway/config.py:24`](../../src/bos/gateway/config.py) | delete `api_key_env` from `ResolvedGatewayConfig` |
| [`config/schema.py:149`](../../src/bos/config/schema.py) | delete `api_key_env` from `GatewayConfig` |
| [`client.py:73/82/358`](../../src/bos/gateway/client.py) | delete the `api_key` parameter and `_auth_headers()` |
| [`cli/commands/agent.py:737/750`](../../src/bos/cli/commands/agent.py) | stop reading the env var and passing it to the client |
| [`cli/commands/doctor.py:140-142`](../../src/bos/cli/commands/doctor.py) | delete the check — warning about an unsupported feature is noise |
| tests | delete `test_gateway_status_requires_bearer_auth`, `test_gateway_allows_unauthenticated_requests_when_key_unset`, `test_resolve_gateway_api_key_*`, `test_ws_endpoint_is_authenticated_even_before_ws_channel_slice` |
| BEP 7 § *Gateway Authentication* | superseded |

**Do not touch `cli/commands/scaffolding.py`.** Its `api_key` occurrences are LLM provider credentials collected by `boscli init`, unrelated to gateway authentication.

`GatewayConfig` is `extra="forbid"` ([`schema.py:143`](../../src/bos/config/schema.py)), so a configuration that still sets `api_key_env` fails validation on load. That is the accepted behaviour: no deprecation shim, matching BEP 16 §5.1's stance. The release note names the key and the one-line fix.

### 3.9 Dependencies and extras

| Extra | Before | After | Measured |
|---|---|---|---|
| `gateway` | `aiohttp` | `starlette`, `uvicorn`, `python-multipart`, `httpx`, `websockets` | measured on implementation: base install 14 MB, with `[gateway]` 19 MB — the extra adds **5 MB** |

Server and client dependencies stay in one extra. Splitting a `client` extra would save an embedder who mounts only the server about 3.4 MB; at that size the extra knob costs more than it saves.

One consequence for BEP 16 §3.4's `_optional` table: `extensions/channels/telegram.py` needed the `gateway` extra only because `aiohttp` lived there. It still maps to `gateway` after the port because `httpx` is there too, so the table entry is unchanged in text and changed in reason. `extensions/channels/lark.py` continues to need `gateway` for the real reason — it imports `bos.gateway` at module level for `ChannelRuntimeContext`.

`bos/gateway/__init__.py` eagerly imports `GatewayClient`, so `import bos.gateway` currently pulls the client stack. It becomes a lazy re-export via module `__getattr__`, so mounting the server does not import `httpx`/`websockets`.

**`websockets` is pinned to 15.x, not 17.x.** `lark-oapi==1.6.8` requires `websockets>=11,<16`, so a 17.x pin makes the `all` extra unresolvable. Everything this BEP uses — `websockets.asyncio.client.connect`, `InvalidStatus.response` carrying the denial body, `websockets.protocol.State` — is present in 15.

**`aiohttp` still reaches a `[litellm]` or `[all]` install transitively**, because `litellm==1.84.0` depends on it. BOS declares it nowhere, and a `[gateway]`-only install does not have it; §7.5's criterion is about BOS's own imports and its extra, not about the transitive closure of every other extra.

### 3.10 Look-alikes

| Name | What it is | Not to be confused with |
|---|---|---|
| `GatewayMount` | The embedded composition root: owns the lock, the ASGI app, and the current `Gateway` | An `ep_plugin`. BOS plugins are **agent capabilities** (memory, skills, plan, subagent) bound under `[agents.*.plugin-bindings.*]`. The gateway is the runtime that *drives* agents; naming it a plugin would collide with an established concept. |
| `Gateway` | The BEP 7 runtime: actors, channels, chat coordination, protocol handlers | `GatewayMount`, which builds and replaces it |
| `GatewayRunDir` | `<bos_dir>/run/` and the five paths under it | `bos_dir` itself |
| `GatewayClient` | The wire client the TUI uses to talk to a gateway | The gateway's own server surface |
| `restart` (mounted) | Rebuild in place; config and agents reload, Python does not | `boscli gateway restart` (standalone), which replaces the process and reloads everything |

### 3.11 Ownership, per concern

| Concern | Single owner | Note |
|---|---|---|
| The listening socket | The host (uvicorn, standalone) | Never `Gateway`, in either mode |
| Process identity — PID file, log file, signal handlers | `bos.runner.__main__` / `proc.start_background` | Standalone only; a mounted gateway creates none |
| Singleton lock acquisition and the watchdog | `GatewayMount` | Lock *helpers* live in `bos.gateway.state` (§3.4.2) |
| `gateway.state` content | `Gateway.status_snapshot()` | Written through the `state_changed` callback `Gateway` already injects |
| `gateway.state` file path | `GatewayRunDir` | Unchanged |
| Configuration source and reload | The `workspace_factory` the host supplies | `Gateway` never reads a `Workspace` (BEP 13 §3.3) |
| Which `Gateway` instance is current | `GatewayMount` | The ASGI app indirects through the mount (§3.3.3) |
| Authentication | The host | BOS has none (§3.8) |
| Public base URL | The host | A mounted gateway cannot discover it (§3.3.2) |
| Chat history, mailboxes, cursors, memory | Unchanged from BEP 5 / BEP 7 / BEP 10 | Explicitly out of scope (§2.2.1) |

---

## 4. Audience flows (end state)

### 4.1 Embedder — mounting the gateway

```bash
pip install bos-ai[gateway,litellm]
```

```python
mount = GatewayMount(lambda: build_workspace(), public_base_url="https://app.example.com/bos")
app.mount("/bos", mount.asgi_app())

@asynccontextmanager
async def lifespan(app):
    await mount.start()
    try:
        yield
    finally:
        await mount.stop()
```

Their application serves `/bos/ws` to its own front end behind its own authentication. If another process already holds the lock for that `bos_dir`, `start()` returns with the mount in `standby`; `GET /bos/api/status` says so and names the holding PID, and the watchdog promotes it to `live` once the lock frees.

### 4.2 End user — the CLI and TUI

Unchanged. `uvx boscli ask`, `boscli tui`, `boscli gateway start/stop/status/restart` behave as today. The TUI's WebSocket client is reimplemented on `websockets`; its behaviour, including takeover and reconnect, is unchanged. The one user-visible difference is that no `Authorization` header is sent and none is required.

### 4.3 Operator

`boscli gateway status` works against both a standalone and an embedded gateway, because both write `gateway.state` (§3.4.4); `runtime` distinguishes them.

`boscli gateway restart` branches on `runtime`:

- `process` — today's path: stop, poll `lock_is_free`, start.
- `embedded` — `POST {base_url}/api/restart`. When the state file has no `base_url` because the host did not supply one, the command reports that the gateway is embedded and its URL is unknown, and tells the operator to restart it through the host. It does not guess a URL.

`boscli gateway stop` against an embedded gateway is refused with the same explanation: the lifetime belongs to the host process.

### 4.4 Background / automated

- The lock watchdog (§3.4.5) is the only new periodic work: one task, 5 s, in-process.
- `boscli gateway start` still spawns `sys.executable -m bos.runner`; `proc.start_background`, the PID file and the log file are unchanged.
- CI gains coverage for the mounted path (§7.7).

---

## 5. Compatibility and fallout

### 5.1 Breaking: `[gateway] api_key_env` is removed

`GatewayConfig` is `extra="forbid"`, so a config that still sets it fails to load with a pydantic validation error. No shim. Fix: delete the line. Release note names it.

### 5.2 Breaking: the gateway no longer authenticates

Any deployment relying on `BOS_GATEWAY_API_KEY` loses its only protection. A gateway bound to a non-loopback address must be fronted by the host or a reverse proxy. The default bind is `127.0.0.1` and is unchanged.

### 5.3 Breaking: `gateway.state` loses its `auth` block

`status_snapshot()` no longer emits `gateway.auth`. Anything parsing the state file for it breaks. Within the repo there is exactly one reader, and it is **not** `boscli gateway status` (which never rendered it) — it is `cli/commands/doctor.py`'s gateway-auth warning, deleted with the change. The shipped `config/template.toml` and both built-in presets set `api_key_env`, so they are updated too or every scaffolded project fails to load.

### 5.4 Breaking: `bos.gateway` API shapes

- `create_gateway_app` returns a Starlette application, not `aiohttp.web.Application`, and takes a `config_provider` in place of `config`. Imported by `tests/test_gateway_http.py`.
- `WSChannel(websocket=...)` takes a `starlette.websockets.WebSocket`.
- `Gateway.run()` is replaced by `start()` and `stop()`. Its only in-repo caller is [`runner.py:32`](../../src/bos/runner/runner.py); the remaining construction sites are eight tests.
- `GatewayClient(api_key=...)` loses that parameter.
- `GatewayClient` becomes a lazy attribute of `bos.gateway` (§3.9). `from bos.gateway import GatewayClient` and `from bos.gateway.client import GatewayClient` both keep working; `bos.gateway.__all__` is unchanged.

Compatibility stance: no shims. `bos-ai` is at 2.0.0 and these are internal surfaces; no third-party consumer is known.

### 5.5 Not breaking

- Wire protocol: envelope JSON, session ack, takeover close code, chat revision semantics.
- Config keys other than `api_key_env`.
- `boscli` command names, arguments and output shapes other than §5.3.
- Ring topology. The seven ring-isolation guards pass unmodified; the lock helpers move *inward* (`runner` → `gateway`), which the existing direction already permits.
- `python -m bos.runner --config ...` and how `boscli gateway start` spawns it.

### 5.6 Third-party impact

A package registering channels via `ep_channel` is unaffected — `Channel` and `ChannelRuntimeContext` do not change. A package that constructs a `WSChannel` directly, or that subclasses `Gateway`, would break; none is known.

---

## 6. Implementation plan (dependency-ordered)

Each step rests on one already completed, and each leaves the three gates green.

**Layer 1 — behaviour-preserving, transport untouched.** The aiohttp surface is unchanged throughout, so `test_gateway_http.py` and `test_gateway_ws.py` act as the guard for all of it.

1. **`AgentRegistry.clear()`** and the `bootstrap_platform` call (§3.5.4). Independent of everything else; lands first. Test: register, clear, rebuild, assert a removed agent is absent from `describe()`.
2. **Move the lock helpers** from `bos/runner/proc.py` to `bos/gateway/state.py`, re-exporting from `proc` (§3.4.2). Pure move; `tests/test_proc.py` passes unmodified.
3. **Split `Gateway.run()` into `start()` and `stop()`**, removing `AppRunner`/`TCPSite` from `Gateway` and leaving the drain and teardown ordering verbatim (§3.2). `runner.start()` temporarily owns the socket so the standalone path keeps working on aiohttp.
4. **Introduce `GatewayMount`** in `bos.runner` (§3.3): lock acquisition, the bidirectional watchdog, states, `asgi_app()` deferred to layer 2, `start`/`stop`/`restart`. Rewrite `runner/__main__.py` to drive it. `boscli gateway start/stop/status/restart` must behave exactly as before this step.
5. **Hot restart** (§3.5), still on aiohttp. Tests: a new `[[runtime.channels]]` entry is live after restart; a deleted agent is no longer callable; the lock is held throughout; a second restart does not raise `Duplicate channel_id`.

**Layer 2 — transport.** Layer 1's tests are rewritten here, once.

**Steps 7 and 9 land together.** They are listed separately below for what each covers, but they cannot be sequenced: `runner.serve()` builds an `aiohttp.web.AppRunner` around `mount.build_app()`, so the moment that returns a Starlette app the standalone path is broken and no gate is green between them. Steps 6, 8 and 10 keep their own boundaries.

6. **Remove authentication** (§3.8). Smallest independent slice of the transport work and it deletes the middleware the port would otherwise have to carry.
7. **Port the server to Starlette**: routes, uploads with the explicit limits (§3.6.3), `WSChannel` and `handle_ws` with denial responses (§3.6.4). Rewrite `tests/test_gateway_http.py` and `tests/test_gateway_ws.py`.
8. **Port the clients** to `httpx` + `websockets`: `GatewayClient`, then `extensions/channels/telegram.py` (§3.7). Rewrite `tests/test_telegram_channel.py`.
9. **uvicorn in the runner** (§3.6.5), including port read-back for `port = 0`.
10. **Drop `aiohttp`; rewrite the `gateway` extra** (§3.9); make `GatewayClient` a lazy re-export. Verify nothing imports `aiohttp` anywhere in `src/` or `tests/`.

**Layer 3 — surface and documentation.**

11. **`POST /api/restart`** and `boscli gateway restart`'s `runtime` branch (§4.3).
12. **`examples/embed_gateway_fastapi.py`** and its CI step, alongside BEP 16's `embed_fastapi.py`.
13. **Documentation**: an *Embedding the gateway* page, the BEP 7 authentication supersession note, and the release note for §5.1–5.4.

---

## 7. Acceptance criteria

Each states its preconditions.

1. Given a FastAPI host that mounts `mount.asgi_app()` at `/bos` and drives `start`/`stop` from its lifespan: a WebSocket client connects to `/bos/ws`, completes a turn, and receives the reply. The host process binds exactly one socket, and no PID file, log file or signal handler is created by BOS.
2. Given two processes sharing one `bos_dir`: exactly one reaches `live`; the other reports `standby` from `/api/status` with the holder's PID, and denies `/ws` with 503. When the holder exits, the standby reaches `live` within two watchdog intervals without intervention.
3. Given a live mounted gateway: `POST /api/restart` after adding a `[[runtime.channels]]` entry leaves that channel running; after deleting an agent file whose name is still in an `enabled` list, that agent is absent from `<available_subagents>` and cannot be invoked. A second consecutive restart succeeds (no `Duplicate channel_id`). `lock_is_free` reports the lock held throughout.
4. Given a live mounted gateway: editing a project-local `./extensions/*.py` and restarting does **not** change tool behaviour, and this is asserted, not assumed (§3.5.2).
5. Given a base install plus `[gateway]`: no module under `src/` or `tests/` imports `aiohttp`, and the `[gateway]` extra adds ≤ 8 MB over a base install (BEP 16 §2.1.1's 14 MB).
6. Given the standalone runner with `port = 0`: actors and channels report started *before* the socket accepts its first connection; the state file carries the bound port; `boscli gateway status/stop/restart` behave as on the previous release except for the removed `auth` block.
7. Given an upload of 15 MB with the default `max_upload_bytes`: it succeeds. At 25 MB it is rejected without buffering the whole body.
8. Given a duplicate `channel_id` on `/ws` against uvicorn: the client observes HTTP 409 with `{"ok": false, "error": "duplicate_channel_id"}`, matching the current aiohttp behaviour.
9. Given a config that sets `[gateway] api_key_env`: loading fails with a validation error naming that key.
10. `uv run pytest -q`, `uv run ruff check src tests examples`, and `npx -y pyright src` are green, with pyright at zero errors. All seven ring-isolation guards pass unmodified.

---

## 8. Open questions

1. **Re-executing project-local extension files on restart.** Excluded by §3.5.2 because `ExtensionPoint`'s duplicate-name guard turns a reload into a silent module-load failure. Revisit only with a concrete need; it requires changing `bos.core.registry`.
2. **Unsetting environment variables on reload** (§3.5.3). Deferred; the workaround is an empty value.
3. ~~**Denial-response fallback behaviour on non-uvicorn ASGI servers** (§3.6.4).~~ **Closed by step 7.** Observed: uvicorn 0.53.0 advertises the extension and delivers `HTTP 409` with the JSON body intact, readable from `websockets.exceptions.InvalidStatus.response.body`; it logs a cosmetic `ASGI callable returned without completing handshake` alongside it. `send_ws_denial` falls back to close code `4000 + status` (4400/4409/4503/4501 — no collision with `WS_TAKEOVER_CLOSE_CODE` 4001) where the extension is absent. `GatewayClient` does **not** need to distinguish the two: it treats every failed connect as a reconnect trigger, and the one code it does inspect, the takeover code, is emitted after the upgrade rather than as a denial.

---

## 9. Revision history

- 2026-09-20 — Draft. Decisions, in the order they were settled: both tracks in one BEP, split before transport, because `run()` is the only place the two changes collide and splitting first keeps the existing aiohttp tests as the guard (§6); `aiohttp` removed completely rather than kept for the clients — measured 11 MB against 6.3 MB for `starlette`+`uvicorn`+`python-multipart`+`httpx`+`websockets` (§3.9); the singleton lock **kept** in the library after initially being scoped for removal, because two gateways on one run dir is a correctness failure, not an operational one (§3.4.1); host-owned storage seams surveyed and rejected as out of scope, with `gateway.state` reclassified as a cross-process concern and kept (§2.2.1, §3.4.4); the mount is **not** an `ep_plugin` (§3.10); hot restart rebuilds the whole `Gateway` because `ChannelManager.stop_all()` does not clear its registry (§3.5.1); `AgentRegistry.clear()` pulled into scope because without it a deleted agent stays callable (§3.5.4); authentication removed entirely with no deprecation shim (§3.8, §5.1); `client` dependencies folded into the `gateway` extra rather than split (§3.9). Measured against starlette 1.6.0 / uvicorn 0.53.0: `Request.form`'s `max_part_size` default of 1 MiB versus `max_upload_bytes`' 20 MiB (§3.6.3); `BaseHTTPMiddleware` skipping non-`http` scopes (§3.6.2); websocket denial responses preserving HTTP 409 and its JSON body (§3.6.4); `server.started` and `server.servers[0].sockets[0].getsockname()` for port read-back (§3.6.5).
- 2026-09-21 — Layer 2 implemented (steps 6–10); four claims corrected against the packages as installed, rather than left as drafted. `max_part_size` bounds a non-file part only, so it cannot be the upload limit and §3.6.3's premise was wrong in a way that would have removed the limit entirely; starlette 1.6.0's `RequestBodyLimitMiddleware` is the `client_max_size` equivalent §3.6.3 said did not exist, wrapped so the limit is read per request (§3.6.3). `uvicorn.Server.serve()` installs `SIGINT`/`SIGTERM` handlers through `capture_signals()` and cannot be used without inverting §3.2's teardown order; the runner drives `startup`/`main_loop`/`shutdown` itself (§3.6.5). Steps 7 and 9 have no green intermediate and land together (§6). `websockets` is pinned to 15.x because `lark-oapi` requires `<16`, and `aiohttp` still arrives transitively via `litellm` (§3.9). Extra size measured at 5 MB over a 14 MB base, against the ≤ 8 MB budget. Open question 3 closed with what step 7 observed (§8).
