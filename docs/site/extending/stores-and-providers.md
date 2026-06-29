# Providers, Stores & Services

Beyond tools, plugins, and channels, BOS exposes extension points for its core services: LLM provider dispatch, conversation persistence, memory consolidation, message routing, job scheduling, turn interception, and agent factories.  This page gives the signature and selection mechanism for each.

---

## LLM providers — `@ep_provider`

A provider is an async function with this signature:

```python
async def my_provider(messages: list[dict], **kwargs: Any) -> LLMResponse: ...
```

`LLMClient` selects a provider by **model-string prefix**:

```
model = "codex/gpt-5"
         ↑     ↑
     prefix  model_name sent to the provider
```

If the prefix (here `"codex"`) is a registered `ep_provider` name, that provider is called with `model="gpt-5"` plus all other kwargs.  If there is no registered provider for the prefix, the full model string is forwarded to the built-in `litellm` provider, which reads the matching `*_API_KEY` env var.

```python
from bos.core import ep_provider

@ep_provider(name="mycloud")
async def mycloud_provider(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    # Call your API here
    ...
```

```toml
# Any agent with model = "mycloud/..." routes here
[agents.main]
model = "mycloud/fast-v2"

# Pass defaults to the provider
[exts.ep_provider.mycloud]
timeout = 30
```

**Built-in provider:** `litellm` is the only built-in provider and the default fallback (registered by `bos.core.defaults`). Because it reaches every backend LiteLLM supports, you only need a custom `@ep_provider` for a non-LiteLLM backend.

---

## Chat stores — `@ep_chat_store`

A chat store owns **conversation persistence and context assembly** — token estimation, summary handling, and tool-noise filtering.  Register a class; its `__init__` receives the `[exts.ep_chat_store.<Name>]` config keys plus `bos_dir` and `workspace_dir`.

### `ChatStore` protocol

```python
commit_turn(chat_id, messages, *, turn_id) -> ChatCommit
get_context(chat_id, *, tokenizer_model=None, filter_mode=None) -> ContextResult
get_compaction_messages(chat_id, *, filter_mode=None) -> list[Message]
estimate_tokens(chat_id, *, tokenizer_model=None, filter_mode=None) -> TokenEstimate
save_summary(chat_id, summary) -> None
get_summary(chat_id) -> Message | None
get_messages(chat_id, *, active_only=True) -> list[Message]
get_revision(chat_id) -> int
get_messages_since(chat_id, *, revision) -> list[Message]
list_chats() -> dict[str, ChatMeta]
```

`InMemChatStore` (`src/bos/extensions/chat_stores/in_memory.py`) is a complete, minimal reference implementation.

**Select via config:**

```toml
[harness]
chat_store = "JsonlChatStore"   # or "InMemChatStore"

[exts.ep_chat_store.JsonlChatStore]
store_dir = "./messages"
```

**Built-ins:** `JsonlChatStore` (default, persistent under `bos_dir`), `InMemChatStore` (ephemeral, useful for testing).

---

## Consolidators — `@ep_consolidator`

A consolidator summarises conversation history and drives memory operations.  The harness builds it with `{model: BOS_CONSOLIDATOR_MODEL, llm}` as defaults.

```python
from bos.core import ep_consolidator

@ep_consolidator(name="MyConsolidator")
class MyConsolidator:
    def __init__(self, model: str, llm: Any) -> None: ...
    # Implements the Consolidator protocol
```

```toml
[harness]
consolidator = "LLMConsolidator"

[exts.ep_consolidator.LLMConsolidator]
model = "gemini/gemini-2.5-flash"
```

**Default:** `LLMConsolidator`.

---

## Mail routes — `@ep_mail_route`

A mail route handles point-to-point message delivery between actors and channels.

### `MailRoute` protocol

```python
class MailRoute(Protocol):
    def bind(self, address: str) -> MailBox: ...
    async def deliver(self, env: Envelope) -> None: ...
```

`bind(address)` returns a `MailBox` for the given address.  `deliver(env)` routes an `Envelope` to its recipient's mailbox.

```toml
[harness]
mail_route = "JsonlMailRoute"
```

**Default:** `JsonlMailRoute` (durable, persists mailboxes under `bos_dir`).

---

## Job runners — `@ep_job_runner`

A job runner executes off-critical-path work (BEP 11): memory consolidation, background indexing, scheduled tasks.

### `JobRunner` protocol

```python
class JobRunner(Protocol):
    async def start(self) -> None: ...
    async def submit(self, job: Job) -> str: ...
    def bind_trigger(self, trigger: JobTrigger, factory: Callable[..., Job | None]) -> None: ...
    async def drain(self, *, timeout: float) -> None: ...
    async def status(self, job_id: str) -> JobStatus: ...
    async def list(self, *, filter: dict | None = None) -> list[JobRecord]: ...
    async def retry(self, job_id: str) -> None: ...
    async def cancel(self, job_id: str) -> None: ...
```

**Triggers:** `"session_close"`, `"idle"`, `"manual"`.

```toml
[harness]
job_runner = "InProcJobRunner"
```

**Default:** `InProcJobRunner` (in-process, built with `{bus: EventBus}`).

---

## Turn interceptors — `@ep_turn_interceptor`

A turn interceptor runs at defined stages of every agent turn.  Configure an ordered chain in `[harness].interceptors`.

### `TurnInterceptor` protocol

```python
class TurnInterceptor(Protocol):
    async def intercept(self, stage: InterceptorStage, context: TurnContext) -> None: ...
```

Raise `AbortTurn` inside `intercept` to stop the turn immediately.

Plugin interceptors (from `get_interceptors()`) run best-effort before the configured chain.

```toml
[harness]
interceptors = [
    "RateLimitInterceptor",
    {name = "AuditInterceptor", log_level = "INFO"},
]
```

Each entry is either a bare name or an inline table with `name = "..."` plus config keys, which are merged into the interceptor's defaults.

---

## Agent factories — `@ep_agent`

A code-defined alternative to `[agents.<name>]`.  Useful when agent configuration depends on runtime information (env vars, remote config, dynamic prompts).

```python
from bos.core import ep_agent

@ep_agent(name="weather_agent", description="Weather forecasting agent")
def weather_agent(region: str = "us") -> dict:
    return {
        "system_prompt": f"You are a weather expert for the {region} region.",
        "model": "gemini/gemini-2.5-flash",
        "tools": {"enabled": ["GetWeather"]},
    }
```

```toml
[exts.ep_agent.weather_agent]
region = "eu"
```

- The factory is invoked **once per bootstrap** with its `[exts.ep_agent.<name>]` config as keyword arguments.
- The result merges as `[agent.defaults] → factory result → [agents.<name>]`, so users can still override it in config.
- The factory may be sync or async.

---

## See also

- [Tools](tools.md) — individual tool registration
- [Plugins](plugins.md) — bundle services into a plugin
- [Configuration](../configuration/index.md) — full `[harness]` and `[exts]` reference
