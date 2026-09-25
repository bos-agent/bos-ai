# BEP 19: External Agent Runtimes — Claude Code and Codex

- **Status:** **Partially implemented** — §6 Layers 1–3 shipped; Layers 4–5 (the two runtimes, packaging, live validation) not started. See §9.
- **Depends on:** BEP 18 (the embedding SDK — this BEP widens two of its promised signatures and adds one method to `BosApp`), BEP 13 (concentric rings — `AgentPort` lands in the innermost ring, the two runtimes in `bos.extensions`), BEP 4 (micro-kernel — `ep_tool` is the registry the MCP egress reads), BEP 6 (configuration architecture), BEP 12 (`AgentResult` and structured output — both runtimes must produce one), BEP 5 (`ChatStore` — what BOS persists for an external chat)
- **Blocked by:** nothing. Every dependency exists in the repo today.

---

## 1. Motivation

BOS can drive any model LiteLLM can reach, and it cannot drive either of the two agent harnesses people actually use for code: Claude Code and Codex. Those are not models. Each is a complete agent loop with its own tools, its own context management, its own permission system, and — the part that matters commercially — its own subscription login. Pointing `model = "claude-opus-4-5"` at LiteLLM gets the model and throws away the harness, and pays per token for what a subscription already covers.

Both vendors now ship an official Python SDK that exposes the whole harness as a library:

- **`claude-agent-sdk`** 0.2.159 (`anthropics/claude-agent-sdk-python`). Ships platform wheels that bundle the Claude Code CLI — `manylinux_2_17_x86_64` is 97 MB. Requires `mcp>=1.23,<3`, `anyio`, `jsonschema`, `sniffio`.
- **`openai-codex`** 0.156.1 (`openai/codex`, `sdk/python`). Pure-Python wheel plus a pinned `openai-codex-cli-bin==0.156.1` that carries the `codex` binary. Speaks JSON-RPC to `codex app-server` over stdio.

The adaptation turns out to be close to total. Every load-bearing parameter of `Agent.run` has a counterpart on both sides — session resume, per-turn model, reasoning effort, JSON-schema structured output, interrupt, an event stream, working directory, a system-prompt slot. §3.9 is the full table, including the seven things that have no counterpart and are dropped on purpose.

### 1.1 What this BEP is *not* claiming

- **Not that the two runtimes are equivalent.** They are not, and §3.5 is an entire section about the one place the gap is dangerous: directory confinement. Codex enforces it with an OS sandbox; Claude Code does not have one for file tools, and BOS has to build the confinement itself out of a permission callback.
- **Not that subscription auth is guaranteed.** BOS requires that the native runtime's existing login be used and fails loudly when it cannot confirm that. Whether a given account or SDK version permits a given use is between the operator and the vendor. BOS stores no subscription token.
- **Not that this has been validated against a real subscription.** Nothing in this BEP has been run. §7 separates the criteria a fake can satisfy from the ones that need a real login, and §8.1 records that the second set is unverified until the work in §6 Layer 4 is done.
- **Not a background-task system.** `ask()` blocks for the duration of the native turn, exactly as it does for a BOS agent. §2.2 says why, and §1.2 what the alternative would have cost.

### 1.2 Runtimes are agents, not delegation tools

The obvious alternative is to expose each runtime as a set of tools — start a task, continue it, poll it, cancel it — so a cheap BOS main agent delegates to Claude or Codex and collects the result later. That is a task-orchestration subsystem: a task record store, a session-mapping table, an owner check, a concurrency limiter, a completion-notification path back into the parent turn, and a recovery story for tasks found in an unknown state after a restart.

Making the runtimes *agents* removes all of it, because BOS already has every one of those mechanisms for agents:

- Delegation: `_HarnessAgentRunner.run()` ([`harness.py:282-314`](../../src/bos/core/harness.py)) calls `create_agent(kind)`. The existing `AskSubagent(role=…)` tool therefore reaches an external runtime the day the runtime is an agent kind, with no new tool and no change to [`plugins/subagent.py`](../../src/bos/plugins/subagent.py).
- Turn lifecycle, interrupt, event fan-out, parent/child nesting: `AgentActor` and the gateway, unchanged.
- Persistence and recovery: `ChatStore`, unchanged.

The cost is that a delegated turn is synchronous. §2.2 accepts that.

---

## 2. Goals and Non-Goals

### 2.1 Goals

1. `create_agent("claude-code")` and `create_agent("codex")` return objects that satisfy every contract `AgentActor`, `_HarnessAgentRunner` and `boscli ask` already require of an agent, backed by the vendors' official Python SDKs.
2. Adapt as much of `Agent.run`'s parameter surface as each SDK supports, and enumerate — in this document and in an error or log line at runtime — every parameter that is dropped.
3. Make the per-agent options an embedder needs available at construction time and in config: permission level, working directory, native model, system prompt, timeout, MCP tool exposure — and let a project give a runtime any number of named instances with their own prompts.
4. Confine an external runtime to a declared directory, and state per runtime what that confinement is actually made of.
5. Expose a chosen subset of `ep_tool` tools to both runtimes through **one** MCP server with one implementation and one authorization check.
6. Give a host application a read path for an external chat's messages without hijacking `ChatStore`.
7. Add no new scheduler, queue, task store, event bus, or config layer.

### 2.2 Non-Goals

1. **Asynchronous / background external tasks.** `ask()` returns when the native turn ends. A host that wants to survive a disconnect already has the gateway's turn machinery. Revisit only with a concrete use case the gateway cannot serve.
2. **Approval round-trips to a human.** BOS has no channel that carries an approval request to a user and back. Every permission decision in this BEP is made by policy, synchronously, without blocking — see §3.5.4. Building that channel is its own BEP.
3. **Mirroring native transcripts into `ChatStore`.** §3.7.
4. **Cross-runtime session migration.** Switching an established chat from `codex` to `claude-code` starts a new native session. BOS says so rather than claiming a lossless move.
5. **ACP.** Not a dependency, not implemented. A future ACP runtime would implement the same `AgentPort` and reuse §3.4–§3.8 unchanged.
6. **Remote Codex Cloud.** Local runtime only.
7. **Reimplementing either harness's tool loop, context compaction, or subagents.**

---

## 3. Design

### 3.1 Runtime shape

| Thing | What it is at runtime | Where it lives | Lifecycle |
|---|---|---|---|
| `AgentPort` | A `Protocol` — no runtime object | `bos/core/agent/contract.py` | n/a |
| `ClaudeCodeAgent` | A plain object, one per `create_agent` call | `bos/extensions/runtimes/claude_code.py` | Built by `create_agent`, appended to `AgentHarness._owned`, closed by `_aclose` on harness exit |
| `CodexAgent` | Same | `bos/extensions/runtimes/codex.py` | Same |
| The `claude` CLI subprocess | One per turn, spawned by `ClaudeSDKClient.connect()` | Child of the BOS process | Created in `run()`, disconnected in the same `finally` |
| The `codex app-server` subprocess | **One per `CodexAgent`**, spawned lazily on first turn | Child of the BOS process | Created on first `run()`, killed by `CodexAgent.aclose()` |
| `BosToolMcpServer` | An in-process `uvicorn.Server` on `127.0.0.1:<ephemeral>` serving one Starlette app | `bos/extensions/runtimes/mcp_egress.py` | One per harness, started lazily the first time an external agent declares `mcp_tools`, stopped on harness exit |

Nothing here is a daemon, a queue, or a scheduled job. The only long-lived additions to a BOS process are the `codex app-server` child and the loopback MCP server, and both exist only when an external agent was built.

### 3.2 The seam: two reserved agent kinds, and agents that inherit from them

`AgentHarness.create_agent` ([`harness.py:411-489`](../../src/bos/core/harness.py)) ends in `agent: Agent = _apply(Agent, kwargs); return agent` and has no substitution point. This BEP adds one:

```python
# Kind -> "module:Class", not the class itself: this module is imported on a
# base install with no extras, so importing a vendor SDK here would break
# `import bos.sdk`. Resolved by importlib on first build, per kind.
EXTERNAL_AGENT_KINDS: dict[str, str] = {
    "claude-code": "bos.extensions.runtimes.claude_code:ClaudeCodeAgent",
    "codex": "bos.extensions.runtimes.codex:CodexAgent",
}
```

The values are dotted paths, not the classes themselves — `harness.py` is on the import path of a base `bos-ai` install with neither extra present, and importing `claude_agent_sdk` or `openai_codex` at module load would make `import bos.sdk` itself require both 97 MB vendor wheels. `_load_external_runtime(runtime)` ([`harness.py:72-91`](../../src/bos/core/harness.py)) resolves one entry with `importlib.import_module` on first build and reports the real `ImportError` plus the extra to install if it fails (§7.10) — it does not assume the failure means the extra is missing, since the same exception shape also covers a genuinely broken import inside an installed runtime module.

An agent is backed by one of them when **either** its kind is a reserved name, **or** its resolved spec carries `external_runtime` — which is what inheriting from a reserved name produces (§3.4). The branch goes immediately after `merged_cfg` is computed and **before** `_bind_plugins_for_agent`, because none of plugins, the local `ToolRegistry`, `ResolvedToolSet`, `_CompositePluginInterceptor` or `_PluginPromptProvider` applies to a runtime that owns its own tool loop:

```python
merged_cfg = _deep_merge(copy.deepcopy(agent_defaults), agent_cfg or {})
runtime = kind if kind in EXTERNAL_AGENT_KINDS else merged_cfg.get("external_runtime")
if runtime is not None:
    external = _load_external_runtime(runtime)(
        kind=kind or runtime, cfg=merged_cfg, chat_store=self.chat_store, workspace=self._workspace,
        mcp=self._ensure_tool_mcp_server,   # §3.8; an accessor, not a server — calling it is
                                            # what starts the loopback server, and a runtime
                                            # with nothing servable never calls it
    )
    self._owned.append(external)        # _aclose() on harness exit — §3.1
    return external
```

`.get`, not `.pop`: `external_runtime` **stays in `merged_cfg`**, which becomes the runtime's own `cfg`. Not because anything downstream reads it there — nothing in the repo does; `CodexAgent` takes its runtime as a constructor literal and `resolved_config` restates that, and stripping the key from the cfg leaves the suite green. The reason is simply that there is nothing to gain by mutating: `.pop` would need a justification and has none, while leaving the key costs nothing and keeps the merged config an accurate record of what was asked for — which is what a third-party `ExternalRuntime` that echoes its raw cfg would report.

`kind` stays the agent's own name, so a Codex-backed agent called `george` reports itself as `george` through `AgentPort.name`, in events, and in `boscli inspect` — the runtime is how it is built, not who it is.

There is no extension point and no adapter abstraction. Two names, two classes, one dict. A third runtime is a third entry; if a fourth ever needs to come from outside the repo, *that* is when an extension point earns its keep.

**`ep_agent` is not the seam.** It is a factory for agent *config dicts*, not agent objects — [`extensions/agents/bos_config.py:174-195`](../../src/bos/extensions/agents/bos_config.py) returns `{"description": …, "system_prompt": …, "tools": …, "plugins": …}`, which `bootstrap_platform` folds into `AgentRegistry`. It cannot return a runtime.

#### 3.2.1 Externally-backed agents do not inherit `[agent.defaults]`

`bootstrap_platform` merges `[agent.defaults]` under every registered agent ([`workspace.py:683-685`](../../src/bos/config/workspace.py)). A project with `[agent.defaults] model = "gpt-4o"` would silently hand `"gpt-4o"` to Codex as a *native* model name, along with `max_tokens`, `plugins` and the rest.

The registration loop therefore starts from `{}` instead of `agent_defaults` when the name is reserved **or** the inheritance-resolved spec carries `external_runtime` — the second half is what keeps `george` covered, since `george` is not a reserved name. `config_specs` is already inheritance-resolved at that point in the loop, so the check is available where it is needed. Each runtime then validates its own config strictly (§3.4).

### 3.3 `AgentPort` — widening four signatures

`Agent` is a concrete class, and four signatures name it:

| Site | Today |
|---|---|
| [`harness.py:411`](../../src/bos/core/harness.py) | `create_agent(...) -> Agent` |
| [`sdk/_app.py:123`](../../src/bos/sdk/_app.py) | `BosApp.agent(kind) -> Agent` |
| [`sdk/_app.py:140`](../../src/bos/sdk/_app.py) | `BosApp.build_agent(kind) -> Agent` |
| [`gateway/actors/agent_actor.py:105`](../../src/bos/gateway/actors/agent_actor.py) | `AgentActor(agent: Agent, ...)` |

A repo-wide scan of what those consumers actually *call* returns four members, and no more:

| Member | Call sites |
|---|---|
| `ask(chat_id, content, interrupt=, ctx_metadata=, llm_args=, event_sink=, turn_id=, commit_observer=)` | [`agent_actor.py:358`](../../src/bos/gateway/actors/agent_actor.py) |
| `run(chat_id, content, *, …, schema=, max_schema_retries=)` | [`harness.py:307`](../../src/bos/core/harness.py), [`cli/commands/agent.py:442`](../../src/bos/cli/commands/agent.py) |
| `request_stop()` | [`agent_actor.py:133`](../../src/bos/gateway/actors/agent_actor.py) |
| `name` | [`agent_actor.py:540`](../../src/bos/gateway/actors/agent_actor.py), via `getattr` |

So `bos/core/agent/contract.py` gains exactly that, and the four signatures widen to it:

```python
@runtime_checkable
class AgentPort(Protocol):
    """What a host requires of an agent, whichever runtime backs it (BEP 19 §3.3)."""

    @property
    def name(self) -> str: ...
    def request_stop(self) -> None: ...
    async def ask(self, chat_id: str, content: MessageContent, ...) -> str: ...
    async def run(self, chat_id: str, content: MessageContent, *, ..., schema: dict[str, Any] | None = None,
                  max_schema_retries: int = 1) -> AgentResult: ...
```

`Agent` satisfies it structurally and is not modified.

**The rejected alternative** was `class CodexAgent(Agent)` overriding only `run()`, which would have left all four signatures untouched and changed no contract. It was rejected because it is inheritance used to borrow an interface: it makes `isinstance(codex_agent, Agent)` true for an object that runs none of `Agent`'s behaviour, forces the two runtimes to satisfy `Agent.__init__`'s required `llm` and `consolidator` kwargs ([`agent.py:264-286`](../../src/bos/core/agent/agent.py)) with objects they never call, and inherits ~700 lines of turn loop, compaction and structured-output retry as dead weight with nothing marking it inapplicable. `AgentPort` is also a written-down list of what a host may rely on, which is the thing to consult when a later BEP adds an SDK-level method.

#### 3.3.1 `boscli inspect` must stop assuming `Agent`

[`cli/commands/inspect.py:204,219-223`](../../src/bos/cli/commands/inspect.py) reaches past the public surface into `agent._prompt_provider._plugins`, `agent._kind`, `agent._name`, `agent._model` and `agent._tools`. Against an external runtime those raise `AttributeError`. `_agent_capabilities` gains a branch: for an object that is not an `Agent`, report `kind`, `name`, the resolved runtime, the resolved `cwd`, the `permission` level, the resolved `mcp_tools`, and the subset of those the host has no `ep_tool` for, with `plugins` and `skills` empty. All of it from the runtime's `resolved_config`, which `ExternalRuntime` promises, so nothing here is a duck-typed reach and nothing requires the agent to have run a turn — which matters for the unavailable names, since the MCP server that would otherwise notice them is built lazily on the first turn and `inspect` runs none. This is the operator's inspection path (§4.3) and is the only place in the repo that reads an agent's internals.

### 3.4 Configuration

The same keys work from TOML and from `agent_cfg`; `AgentConfig` is `extra="allow"` ([`schema.py:62`](../../src/bos/config/schema.py)) so they pass through `_agent_config_to_core_kwargs` untouched. Each runtime validates them itself and **raises on an unknown key** — the pass-through is a transport, not a licence.

```toml
[agents.codex]
cwd = "services/api"            # workspace-relative; see §3.5.1
permission = "workspace-write"  # REQUIRED, no default; see §3.5
model = "gpt-5.1-codex"         # native model name, passed through verbatim
system_prompt = "..."           # → developer_instructions
timeout_seconds = 1200
auth = "subscription"           # default; see §3.10.3
mcp_tools = ["DeskCreateTask"]  # default []; see §3.8

[agents.codex.native_options]   # escape hatch, per-runtime allowlist
personality = "concise"
```

```toml
[agents.claude-code]
cwd = "."
permission = "read-only"
model = "claude-opus-4-5"
setting_sources = ["project"]   # Claude only; default ["project"]. See §3.5.3
mcp_tools = []
```

`[agents.claude-code]` is a legal TOML table: bare keys permit `-`.

**`_parent = "codex"` and `_parent = "claude-code"` work, and are the main way to configure one.** An agent that inherits from a reserved kind is a *named instance* of that runtime: it gets its own name, its own prompt, its own `cwd` and permission, and it is addressable everywhere a BOS agent is — `[runtime.actors.*]`, `AskSubagent(role=…)`, `boscli ask --agent`, `app.agent()`.

```markdown
<!-- <bos_dir>/agents/george.md -->
---
_parent: codex
cwd: services/api
permission: workspace-write
mcp_tools:
  - DeskCreateTask
---
You are George, the implementer for the payments service. …
```

`george` now has Codex's capabilities under its own name and its own prompt, and a sibling `martha.md` differs only in its body. The bare kinds stay available — `[agents.codex]` gives the workspace one default profile, and `create_agent("codex")` works with no config at all — but nothing forces a project to address the runtime by the vendor's name.

The mechanism is the existing resolver plus one table. `_resolve_agent_inheritance(specs, factory_specs)` already accepts any parent present in `specs` or `factory_specs` and deep-merges the parent's resolved spec underneath the child ([`workspace.py:439-483`](../../src/bos/config/workspace.py)), so the two reserved kinds are supplied as pseudo-factory specs at that one call site:

```python
_EXTERNAL_RUNTIME_SPECS = {"codex": {"external_runtime": "codex"},
                           "claude-code": {"external_runtime": "claude-code"}}
config_specs = _resolve_agent_inheritance(config_specs, factory_specs | _EXTERNAL_RUNTIME_SPECS)
```

Three consequences fall out of the resolver's existing behaviour rather than needing new code. `george` inherits `external_runtime`, which is what §3.2 dispatches on. If the project also writes `[agents.codex]`, `parent in specs` is true and its resolved spec merges in too, so shared `cwd` / `permission` / `model` can be set once and each named instance overrides only what differs. And chains (`harry` → `george` → `codex`) resolve transitively with cycle detection, unchanged.

The merge is **only** at that call site: the reserved kinds are deliberately *not* added to the `{**factory_specs, **config_specs}` enumeration that registers agents, so a project that never mentions them gets no phantom `codex` agent in `AgentRegistry.describe()` and no new ambiguity in `resolve_default_agent()`.

`external_runtime` is written by the resolver, never by hand. A user-authored `external_runtime` key is rejected by the strict validation in §3.4, with a message naming `_parent` — one way to say it.

Profiles can equally come from the actor table, which needed no change either:

```toml
[runtime.actors.coder]
agent = "george"                       # or "codex" for the bare default profile
agent_cfg = { permission = "read-only" }
```

`BosApp.build_agent` gains an additive optional parameter so the programmatic path can set these without TOML:

```python
coder = await app.build_agent("codex", agent_cfg={"cwd": "services/api", "permission": "workspace-write",
                                                  "mcp_tools": ["DeskCreateTask"]})
```

This is also the API for choosing which tools reach MCP (§3.8) — one key, one source of truth, rather than a second `expose_tools()` entry point that could disagree with the config.

#### 3.4.1 System prompt: `system_prompt` and `base_instructions`

`system_prompt` keeps its name and means **"the instructions for this agent"** on an external runtime too — but it is **layered on top of** the runtime's own prompt rather than replacing it. Replacing is a separate key, `base_instructions`, and it is rarely what anyone wants.

Each runtime has three prompt layers. BOS owns exactly one of them:

| Layer | Owner | Claude Code | Codex |
|---|---|---|---|
| The harness's own base prompt | The vendor | Claude Code's system prompt | codex base instructions |
| **The agent's instructions** | **BOS — `system_prompt`** | `--append-system-prompt` | `developer_instructions` |
| Project docs under `cwd` | The repo | `CLAUDE.md`, via `setting_sources` (§3.5.3) | `AGENTS.md`, via `project_doc_max_bytes` |

- **`system_prompt: str`** → Claude Code `system_prompt={"type": "preset", "preset": "claude_code", "append": …}`; Codex `thread_start(developer_instructions=…)`. The harness keeps its own tool guidance.
- **`base_instructions: str`** → Claude Code the plain-`str` form of `system_prompt`; Codex `thread_start(base_instructions=…)`. You now own the prompt and the harness's tool guidance is gone. Setting both keys is an error.

Appending is the default because the destructive reading is the dangerous one: Claude Code's system prompt is what makes Claude Code's tools work, and a config author writing `system_prompt = "You are the implementer."` is expressing a role, not asking for the harness to be lobotomised. An earlier draft of this BEP renamed the key to `instructions` and rejected `system_prompt` outright; that is reverted, because it would also have broken the file route below — where the key is not written by hand at all.

#### 3.4.1.1 How a host supplies it

Two routes, both existing mechanisms, and **no path-valued config key**. A `*_file` key would have to resolve relative to something, and in a host holding several workspaces (§3.12) that "something" is exactly the ambiguity to avoid. Files are read by the layer that already knows the workspace root; everything below it sees a resolved string.

**1. A file, per named agent — the authoring route.** `resolve_agents()` scans `[platform.agent_dirs]` (default `./agents`, resolved against that workspace's `bos_dir`) for `.toml` and `.md`. In a `.md` file the YAML frontmatter is the agent config and **the body is `system_prompt`**; the agent's name is the filename stem ([`workspace.py:529-564`, `:184-204`, `:974-982`](../../src/bos/config/workspace.py)). Combined with `_parent` (§3.4), one file is one named instance of a runtime — `agents/george.md`, `agents/martha.md` — and the prompt is just the body, where a prompt belongs.

This needs **no change to the loader**: the body lands in `system_prompt`, which is the key §3.4.1 defines. It is also why that key could not be renamed or rejected — nobody writes it by hand on this route.

One frontmatter constraint, which the existing parser imposes rather than this BEP: `_parse_simple_yaml_mapping` is a small subset of YAML, so keys must be plain scalars or simple indented blocks ([`workspace.py:229-262`](../../src/bos/config/workspace.py)). The list form shown in §3.4 for `mcp_tools` is within it; anything more structured belongs in a `.toml` agent file or in `[agents.*]`.

**2. `agent_cfg`, programmatically — the embedding route.** A host that keeps prompts in its own store (per tenant, in a database, templated per request) passes the resolved string, to a kind the workspace's own config does not already build:

```python
coder = await app.build_agent("codex", agent_cfg={"permission": "read-only", "system_prompt": prompts.for_tenant(t)})
```

`agent_cfg` is the override layer for that call, so it wins over `[agents.codex]` if written. It is **not**, however, a way to reconfigure an agent that `BosApp` already built: `__aenter__` pre-builds and caches every kind your config names in `[agents]` — including a named instance such as `george` from `agents/george.md` — before application code runs, and `build_agent` caches per kind. So `build_agent("george", agent_cfg={…})` finds `george` already cached and raises, naming the cache and what to do instead, rather than silently discarding the override ([`sdk/_app.py`](../../src/bos/sdk/_app.py)'s `BosApp.build_agent`). Reach for `agent_cfg` on a kind the config leaves unbuilt — a bare reserved kind with no `[agents.<reserved>]` table, as above — or call `harness.create_agent` directly, which has no per-kind cache and honours `agent_cfg` on every call. The TOML equivalent for a per-actor profile is a section — which, unlike an inline table, can hold a multi-line string:

```toml
[runtime.actors.coder.agent_cfg]
permission = "workspace-write"
system_prompt = """
You are the implementer for this service. …
"""
```

Precedence, lowest to highest, within one `create_agent` call: the reserved kind's `{"external_runtime": …}` → `[agents.<reserved>]` if written → the named agent's own spec (`.md` frontmatter and body, or `[agents.george]`) → `[runtime.actors.*].agent_cfg` → the `agent_cfg` passed to `build_agent` / `create_agent`. This is BEP 6 merge order with the §3.4 parent link slotted in; nothing new. Through `BosApp.build_agent`, this order is reachable only on a kind's first build — a kind already cached cannot re-enter it (above).

#### 3.4.1.2 What belongs in it

The append slot should carry **only what the harness cannot already know**. Everything else is duplication, and duplication of a vendor's own prompt makes the agent worse, not better.

Worth writing: the agent's role in this deployment and who consumes its reply (`AskSubagent` and host code are not a human at a terminal); the output contract — what "done" and "blocked" look like; the boundary, in words, matching §3.5's `permission` and `cwd`, including that it should report rather than widen its own scope; and the host's domain vocabulary when §3.8 exposes domain tools, since `DeskCreateTask` means nothing without it.

Not worth writing: how to use `Read` / `Edit` / `Bash`, how to search a repo, how to write good code, or general helpfulness. The harness's own prompt does all of that better.

#### 3.4.1.3 The Claude Code default is a trap

`ClaudeAgentOptions.system_prompt` defaults to `None`, and `SubprocessCLITransport._build_command` turns `None` into `--system-prompt ""` — an *empty* prompt, not Claude Code's own ([`subprocess_cli.py:572-573`](https://github.com/anthropics/claude-agent-sdk-python)). The way to get the CLI's default is `{"type": "preset", "preset": "claude_code"}` with no `append`, which matches none of the branches and so emits **no flag at all** (`:577-585`).

So `ClaudeCodeAgent` always sets the option explicitly: the bare preset when `system_prompt` is unset, the preset with `append` when it is set, a plain `str` for `base_instructions`. Leaving the SDK default in place would ship an agent with Claude Code's tools and none of the prompt that drives them — a quality regression with no error anywhere to point at. §7.13 therefore asserts the emitted CLI flags, not the resolved config. Codex is inert by comparison: omitting both instruction keys leaves its defaults alone.

#### 3.4.1.4 Project docs

The third layer is authored by the repo, not by BOS, and BOS does not silently change it. `CLAUDE.md` loads when `setting_sources` contains `"project"`, which is the default (§3.5.3). `AGENTS.md` is read by codex itself; the knobs are the `project_doc_max_bytes` and `project_doc_fallback_filenames` config keys, reachable through `native_options` and injected via `thread_start(config=…)`. Both stay at the runtime default unless configured. Whether `project_doc_max_bytes = 0` fully suppresses `AGENTS.md` is behavioural, so it is §7.25 rather than a claim here.

### 3.5 Permission and directory confinement

This is the section to read before trusting anything else in this BEP. The two runtimes enforce confinement with different machinery at different strengths, and the config key is deliberately a BOS-owned three-value enum so the asymmetry is described once, here, instead of being implied by two vendors' mode names.

| `permission` | Codex | Claude Code |
|---|---|---|
| `read-only` | `Sandbox.read_only` + `ApprovalMode.deny_all` | `permission_mode="plan"`, `can_use_tool` denies every write tool |
| `workspace-write` | `Sandbox.workspace_write` + `ApprovalMode.auto_review` | `permission_mode="acceptEdits"`, `can_use_tool` path check, `sandbox={"enabled": True}` |
| `full-access` | `Sandbox.full_access` + `ApprovalMode.auto_review` | `permission_mode="bypassPermissions"`, no path check |

The §3.5.4 approval handler is installed on the Codex client at **every** level, not only `workspace-write`: it is the answer to a request to escalate past whichever sandbox the row above set, and there is no level at which BOS can grant one.

#### 3.5.1 `cwd`

Resolved relative to the harness workspace (`AgentHarness._workspace`), then `Path.resolve()`d and required to be inside it. A `cwd` that escapes fails at `create_agent`, not at first turn. The resolved absolute path is the confinement root for §3.5.2 and §3.5.3, and is what `boscli inspect` reports.

#### 3.5.2 Codex: OS sandbox

`Sandbox.read_only` / `workspace_write` / `full_access` map to `SandboxMode.read_only` / `workspace_write` / `danger_full_access` at thread start and to `ReadOnlySandboxPolicy` / `WorkspaceWriteSandboxPolicy` / `DangerFullAccessSandboxPolicy` at turn start ([`openai_codex/_sandbox.py`](https://github.com/openai/codex)). The codex runtime enforces these at the OS level. `workspace_write` confines writes to the thread's `cwd` plus configured writable roots. **This is real confinement**, and it is the one BOS does not have to build.

#### 3.5.3 Claude Code: there is no file sandbox — BOS builds the confinement

Three things must be said plainly, all verified in `claude_agent_sdk/types.py` 0.2.159:

1. `ClaudeAgentOptions.cwd` sets the subprocess working directory. It does not restrict anything.
2. `add_dirs` *widens* access beyond `cwd`. There is no narrowing counterpart.
3. `SandboxSettings` covers **bash commands only**, is `enabled: False` by default, and is macOS/Linux only. Its own docstring is explicit: *"Filesystem and network restrictions are configured via permission rules, not via these sandbox settings — Filesystem read restrictions: Use Read deny rules; Filesystem write restrictions: Use Edit allow/deny rules."*

`permission_mode` is an approval policy, not a sandbox. So for Claude Code the confinement is assembled by BOS:

- **File tools** — a `can_use_tool` callback (`CanUseTool = Callable[[str, dict, ToolPermissionContext], Awaitable[PermissionResult]]`). For `Read`/`Write`/`Edit`/`Glob`/`Grep`/`NotebookEdit`, resolve the path argument and return `PermissionResultDeny` when it leaves the root. Under `read-only`, deny every mutating tool outright.
- **Bash** — `sandbox={"enabled": True, "allowUnsandboxedCommands": False}`. Parsing shell commands for paths is not attempted; the OS sandbox is the only honest answer, and where it is unavailable (Windows) `workspace-write` on Claude Code must be refused at `create_agent` with a message saying why.
- **Settings inheritance** — `setting_sources` defaults to `None`, which loads `~/.claude/settings.json`, `.claude/settings.json` and `.claude/settings.local.json`. A server whose behaviour changes with the operator's personal machine config is not reproducible, and those files carry hooks, permission rules and MCP servers that can contradict `permission`. BOS therefore sets it explicitly, defaulting to `["project"]` — repo-controlled, and required for `CLAUDE.md` to load, which is usually the point of a coder agent. `"user"` is opt-in.

The BEP's language for this everywhere else is **"tool-argument path denial plus a bash OS sandbox"**, never "sandboxed".

#### 3.5.4 Approvals are decided by policy, never awaited

BOS has no channel that carries an approval request to a user and back (§2.2.2), so both callbacks return immediately.

For Claude Code that is what `can_use_tool` does by construction.

For Codex there is a hazard that must be handled rather than documented away: `CodexClient._default_approval_handler` **auto-accepts** — it returns `{"decision": "accept"}` for both `item/commandExecution/requestApproval` and `item/fileChange/requestApproval`. `AsyncCodex.__init__` takes only a `CodexConfig` and passes no handler down through `AsyncCodexClient` to `CodexClient`, so with `ApprovalMode.auto_review` every escalation past the sandbox is granted silently. Left alone, `permission = "workspace-write"` would mean "anything that asks is allowed", contradicting its own config key.

`CodexAgent` therefore installs its own handler on the constructed client, in `_ensure_client` and **before** the auth preflight — the first call that can spawn the child, and so the first moment a server request can arrive:

```python
# BEP 19 §3.5.4. The SDK's default handler auto-accepts every escalation and
# AsyncCodex exposes no way to replace it, so we reach the sync client that owns
# the transport. test_codex_approval_handler_attribute_exists fails loudly if a
# future openai-codex renames this, rather than silently restoring auto-accept.
client._client._sync._approval_handler = self._deny_approval
```

There is no `permission` argument and no per-level branch: `_deny_approval` refuses every approval request at every level. The sandbox is the real boundary and is already set per turn from `permission`; an approval request only ever arrives to escalate *past* it, and with no channel to a human, "no" is the only answer BOS can honestly give — under `full-access` too, where full access is what the *sandbox* grants and a request to go beyond it is still unanswerable. Refusing them all does not disarm a working agent: `auto_review` maps to `AskForApproval(on_request)`, so the server asks only when the agent asks to escalate, and `deny_all` (`read-only`) does not ask at all. Each refusal is logged at WARNING with the method, the runtime and the agent kind, because a silently refused escalation and a model that simply chose not to try look identical from the outside.

**There is no `"deny"` decision in this protocol.** Each method spells refusal its own way, and `CodexClient._reader_loop` writes whatever the handler returns straight back as the JSON-RPC `result`, so a plausible-looking guess is a protocol violation on the wire. The five approval methods in the `ServerRequest` union, with the values read out of the schema the shipped binary generates (`codex app-server generate-json-schema`):

| method | refusal | notes |
|---|---|---|
| `item/commandExecution/requestApproval` | `{"decision": "decline"}` | `decline` = refused, the agent continues the turn; `cancel` would refuse *and* interrupt the turn |
| `item/fileChange/requestApproval` | `{"decision": "decline"}` | same vocabulary |
| `item/permissions/requestApproval` | `{"permissions": {}}` | not a `decision` at all — the response is a granted-permission profile, whose `fileSystem` and `network` are both optional, so `{}` grants nothing |
| `execCommandApproval` (legacy) | `{"decision": {"denied": {"rejection": …}}}` | the older `ReviewDecision`, which also has two refusals: `abort` ("do nothing until the user's next command") and `DeniedReviewDecision` ("should not execute it, but it should continue the session and try something else"). `denied` is the legacy analogue of `decline`, `abort` of `cancel` |
| `applyPatchApproval` (legacy) | `{"decision": {"denied": {"rejection": …}}}` | same |

A refused escalation is not a turn failure: the agent should be left to finish with what the sandbox already allows. Four of the five vocabularies put that as a choice — a refusal that stops the turn, and a refusal that lets the agent carry on — and each of those four rows picks the second. `item/permissions/requestApproval` offers no decision at all, its response being a granted-permission profile rather than a verdict, so refusing it means granting nothing and the agent carries on by construction.

`denied` is also the one refusal in the protocol that carries text back to the model, and that text is the reason to prefer it over `abort`: an agent told *why* it was refused and what it may still do can adapt, where one told only "no" cannot. Both legacy methods send the same string, from a single module constant (`_LEGACY_REJECTION`). It is model-facing, so it is written in the agent's terms — that the agent runs unattended, that no one can be asked, and that it should continue within the sandbox — and carries no BOS vocabulary and no section numbers.

Two further properties of the seam constrain the handler. It is handed **every** server-to-client request, not only approvals: the other five (`item/tool/call`, `item/tool/requestUserInput`, `mcpServer/elicitation/request`, `attestation/generate`, `account/chatgptAuthTokens/refresh`) fall through to `{}`, exactly as the vendor default answers them, and widening that is out of this section's scope. And it is **synchronous**, called on the vendor's single stdout reader thread rather than the event loop, so it must never block — that thread is the sole consumer of the child's stdout, and stalling it stalls every notification and every response for every turn.

The pinned test is what makes the private reach-in acceptable, and it is pinned in both directions: an SDK upgrade that moves the attribute breaks CI instead of quietly re-opening the hole, and one that adds a supported `approval_handler` parameter breaks CI to say the reach-in can stop. There is deliberately no `getattr` guard — a silently restored auto-accepting default is the entire hazard. That these refusals are *accepted* by a live `codex app-server` is only confirmable by a real run — a normal run reaches the two `requestApproval` methods; the legacy pair and the permissions profile are backstops a live run may never exercise. Asserted in §7 criterion 18 rather than claimed from CI.

#### 3.5.5 Verification is behavioural

Per-runtime acceptance (§7) asserts observed effects — a write outside the root fails, a read outside the root fails — not that a mode name was passed. Mode names are evidence of intent, not of enforcement.

### 3.6 Session continuity

`ask(chat_id, …)` must resume the same native conversation on the next turn.

| | Resume | Id returned by |
|---|---|---|
| Claude Code | `ClaudeAgentOptions.resume = <session_id>` | `ResultMessage.session_id` |
| Codex | `AsyncCodex.thread_resume(thread_id)` | `AsyncThread.id` |

The mapping is stored in the **metadata of the assistant message BOS commits each turn** (§3.7): `{"external_runtime": "codex", "native_session_id": "…"}`. It is rewritten every turn, so the newest committed message always carries the freshest id; recovery after a restart is `chat_store.get_messages(chat_id)` scanned from the end. No new store, no `ChatStore` schema change, no separate mapping file. `read_native_session_id` re-runs that scan on every call; an in-process cache in front of it is not built (§8.2).

A native session that cannot be resumed — deleted, archived, pruned — is reported as an error naming the runtime and the id. BOS does not silently start a fresh session under the same `chat_id`.

Changing an established chat's runtime starts a new native session and says so (§2.2.4).

### 3.7 What BOS persists, and `BosApp.get_messages`

BOS commits **two messages per turn**: the user message, and the final assistant text with the metadata from §3.6 plus `{"usage": …, "native_turn_id": …}`. It does not commit the native runtime's tool calls, tool results, or thinking.

That thin record is what `AgentActor`, `AskSubagent`, memory ingestion and `list_chats` see, and it is the only part BOS guarantees. Intra-turn activity reaches a live UI through `event_sink` (§3.9), not through the store.

The full transcript is read on demand from the runtime that owns it. A new method on `BosApp` — the one place a host asks for messages without knowing which runtime produced them:

```python
async def get_messages(self, chat_id: str, *, source: Literal["auto", "bos", "native"] = "auto") -> list[Message]: ...
```

- `source="bos"` — `harness.chat_store.get_messages(chat_id)`. Unchanged behaviour, always available.
- `source="native"` — delegates to `native_messages(chat_id)` on the runtime that owns the session. Claude Code: `get_session_messages(session_id, directory=…)`, which parses the JSONL under `~/.claude/projects/` and chains it by `parentUuid`. Codex: `thread.read(include_turns=True)`, on an `AsyncThread` constructed directly over the client — never `thread_resume`, which would re-establish a live session with this agent's sandbox, approval mode and `cwd` on what the caller asked to be a read. Both project into BOS `Message` objects (`role`, `content`, plus `metadata["source"] = "<runtime>"`, `metadata["native_turn_id"]`, and `metadata["native_item_id"]` for a message projected from a transcript item) so a host's rendering code does not fork. A gap marker (below) is not projected from an item and carries no `native_item_id`; the turn id is the handle a host has on it.
- `source="auto"` — read the BOS record, and if its metadata names an external runtime, return the native transcript; otherwise return the BOS record.

**Messages only, and only the ones a conversation is made of.** Codex's `ThreadItem` is a nineteen-member union; two of them — `UserMessageThreadItem` and `AgentMessageThreadItem` — are messages, and the other seventeen (reasoning, command execution, file change, MCP tool call, web search, plan, the inline `ContextCompactionThreadItem`) are the turn's internal work. They are skipped, and a native read carries no `tool_calls`, for a structural reason and not merely a stylistic one: **BOS represents tool activity as a pair** — an assistant message advertising `tool_calls`, then a `role="tool"` message whose `tool_call_id` matches it — and a native transcript has no such pairing. Projecting a `CommandExecutionThreadItem` into that shape means inventing a call id and an assistant message the runtime never produced, which a host renders as authoritative rather than as a reconstruction. That is a forgery, not a projection, and it is the reason this is settled rather than a matter of effort.

**And BOS does not have that activity anywhere else.** `event_sink` (§3.9) is a live stream emitted while BOS itself runs a turn; nothing under `bos/core/` persists it, and for the case `source="native"` exists for — a session BOS did not author — none was ever emitted. So the honest statement to a host is that the tool activity is unavailable through BOS, not that it is available on another surface. A host that needs it reads the runtime's own store with the runtime's own tools.

Among agent messages, `final_answer` and no-phase are kept and `commentary` is skipped — the same rule the turn path's own `_final_assistant_response_from_items` applies, for the same reason: commentary is not the answer.

**A turn the runtime did not load becomes a visible gap, not a shorter list.** Codex's `Turn.items_view` is `notLoaded | summary | full`, and on anything but `full` the turn's `items` are absent or summarized. Projecting them anyway would publish a shorter conversation than the one that happened, which is exactly the silent lie this section exists to prevent; dropping the turn would do the same more quietly. So each such turn emits one marker `Message` carrying `metadata["items_view"]` and logs at WARNING, and the read still succeeds — a partly-unloaded old thread is ordinary, and raising would break the read for exactly the long-lived sessions someone wants to read.

**Routing is by stored metadata, not by a `chat_id` naming convention.** The `external_runtime` key is already required for §3.6, so the lookup costs nothing extra, it works for chat ids a host created before this BEP, and it avoids a second id convention colliding with the existing one (`INTERNAL_CHAT_SEPARATOR = "~"`, shape `{parent}~{tag}~{uuid}`, [`_chat_store_utils.py:12-38`](../../src/bos/core/_chat_store_utils.py)). Which *agent* speaks for that runtime is decided by matching `resolved_config["external_runtime"]` against it, and never guessed: no built agent for the runtime, more than one, or one with no `native_messages` each raise, naming the runtime and what to do, rather than reading the wrong transcript or falling back to the BOS record — which would answer a different question than the one asked. `native_messages` is duck-typed rather than added to `AgentPort` or `ExternalRuntime`, because only a runtime with a transcript of its own can offer it and widening either protocol would oblige BOS's own `Agent` to implement it.

`ChatStore` is **not** modified and not wrapped. A host reading `app.harness.chat_store.get_messages(chat_id)` directly — which is what the first host does today, and the only public path, since `Agent` exposes no `chat_store` property — keeps getting exactly what it gets now: the BOS record. `app.get_messages` is additive.

**What `source="native"` does not promise.** It is a live read of data BOS does not own. Each runtime compacts and prunes on its own schedule; a Codex thread can be archived or deleted; Claude Code's transcripts live under the invoking user's `~/.claude/projects/` and move with that home directory. `items_view` can come back partial. So a native read can return fewer messages than it did before, can contain gap markers where the runtime declined to load part of its own history, or can fail. `source="bos"` is the only read BOS stands behind. The known upgrade path for Claude Code is `ClaudeAgentOptions.session_store`, an official port whose contract is *"every transcript line written locally is also passed to `session_store.append()`, and `resume` can materialize from the store when the local file is absent"* — implementing it over `.bos/` would make transcripts workspace-local and portable. Not in this BEP. Codex exposes no equivalent port.

### 3.8 The MCP egress

A host application's own API is registered as BOS tools via `ep_tool`. A BOS agent calls them directly. An external runtime cannot: it has its own tool loop in another process. MCP is the only wire either vendor offers, and both accept **streamable HTTP**:

- Claude Code: `McpServerConfig` is `McpStdioServerConfig | McpSSEServerConfig | McpHttpServerConfig | McpSdkServerConfig`; the HTTP form is `{"type": "http", "url": …, "headers": {…}}`.
- Codex: `[mcp_servers.<name>]` supports STDIO and Streamable HTTP with `url`, plus `bearer_token_env_var` / `http_headers` / `env_http_headers`. `AsyncCodex.thread_start(config=…)` injects config overrides per thread.

So there is **one server, not one per vendor**:

- `BosToolMcpServer` ([`mcp_egress.py`](../../src/bos/extensions/runtimes/mcp_egress.py)) builds an [`mcp.server.lowlevel.Server`](https://github.com/modelcontextprotocol/python-sdk), not the higher-level `mcp.server.mcpserver.MCPServer` an earlier draft of this section sketched: `MCPServer.add_tool` has **no schema parameter at all** — it derives a tool's input schema by introspecting the Python function it wraps — so a generic `**kwargs` closure forwarding to `ep_tool.invoke(name, kwargs)` would advertise a tool that takes no arguments, never the host tool's real JSON schema. The lowlevel `Server` is constructed with `on_list_tools=` / `on_call_tool=` handlers that build `types.Tool` / `types.CallToolResult` objects directly, so `ep_tool.build_openai_schema(ext)`'s schema reaches the client unmodified.
- **Per-agent scoping cannot be an HTTP middleware**, and the earlier draft's sketch of one was the section's central error: a middleware sees a request and a response byte-stream, never the MCP-level `tools/list` *response body* it would need to filter — every agent would have seen every tool. Instead, `on_list_tools`/`on_call_tool` each read the caller's bearer token off `ctx.request.headers` (`ServerRequestContext.request`, `Optional[Request]`) and compute that call's allowed tool set themselves, so the listing *for that call* already **is** the allowlist. This works because `mcp` 2.x's streamable-HTTP session runner attaches the inbound Starlette `Request` to *each dispatched message* (`mcp/server/streamable_http.py`'s `_message_metadata`, read back out in `mcp/server/runner.py`'s `_make_context`), not to the connection — so a single kept-alive HTTP connection carrying many calls still gets the right grant on every one of them, even interleaved across agents. `ServerRequestContext.request` is marked transitional upstream (`# TODO(L54): remove for Context rework`); `mcp_egress.py`'s `_headers_of` is the one place that reads it, annotated as `ServerRequestContext[Any, Request]` so pyright — not just a runtime probe — catches the day it moves. (`ServerRequestContext` takes two type parameters; the second, `RequestT`, defaults to `Any`, so annotating only `[Any]` would leave `ctx.request` unchecked.)
- The app actually served is `Server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="127.0.0.1")` under an in-process `uvicorn.Server` on an ephemeral loopback port, run via `config.load()` / `server.startup()` / `server.main_loop()` rather than `Server.serve()` — the latter installs `SIGINT`/`SIGTERM` handlers on the thread it is called from, which belongs to whatever embeds BOS. `streamable_http_app` is a method both the lowlevel `Server` and the higher-level `MCPServer` expose under the same name; naming `MCPServer` for it was an easy mistake given that, but the object actually serving requests is the lowlevel one built two bullets up.
- A raw ASGI wrapper (`_gate`, not Starlette's `BaseHTTPMiddleware` — that buffers through a wrapping response and does not get along with the transport's SSE streams) sits in front of the app and fails closed: every ASGI scope except `lifespan` (which carries no headers and is how the MCP session manager itself starts up) must present a bearer token from a prior `register_agent()` call, including a `websocket` scope this app registers no route for — refused with a 401 over HTTP or a `1008` close over that scope, never silently proxied through. A token is never written to config or to a transcript; Codex receives it through `http_headers`, Claude Code through `headers`.
- Claude Code's in-process `create_sdk_mcp_server()` is deliberately **not** used: it would be cheaper for Claude and unusable for Codex, producing two tool-serving code paths. A Codex stdio shim is also rejected — a shim process still needs IPC back into BOS, and HTTP *is* that IPC.

For Codex the endpoint is injected per thread, not per process: `thread_start`/`thread_resume` take a `config` object that is merged over `~/.codex/config.toml`, and BOS passes `{"mcp_servers": {"bos-tools": {"url": server.url, "bearer_token_env_var": "BOS_MCP_BEARER_<random>"}}}` with the token itself in `CodexConfig(env=…)`. The server name is one constant (`_MCP_SERVER_NAME`), and it is `bos-tools` — what the server already calls itself over MCP — rather than the bare project name, because the merge means it is shared with the operator's own config and a collision is a real failure (§8.2).

Which auth key carries the token is a security choice, not a style one, and it was settled by driving the real `codex app-server` against a loopback server that records the `Authorization` it receives. `bearer_token_env_var` is the one whose token the child actually sends, against every credential key such an entry can carry. The enumeration is the binary's own rather than the set of things tried — `codex mcp get <name> --json` renders a loaded streamable-HTTP `transport` as exactly `url`, `bearer_token_env_var`, `http_headers`, `env_http_headers`, `http_headers_helper` — and each of the four credential keys was planted on a colliding operator entry and lost, as did no operator entry at all. One path is outside that enumeration and **unmeasured**: OAuth, which is a mechanism rather than a key (`codex mcp add --oauth-client-id/--oauth-client-registration/--oauth-resource`, credentials supplied by `codex mcp login`), and which measuring needs a provider. `http_headers` — which an earlier round shipped — loses to an operator `bearer_token_env_var`, and losing is silent (§8.2). `bearer_token` is not a literal-token key at all and fails config loading outright. The variable name is generated fresh per client so that no other `[mcp_servers.*]` entry can name it and read BOS's token out of the shared environment; `CodexConfig(env=…)` is additive, so the child keeps the rest of its environment. The cost is that the token rests in `/proc/<pid>/environ`, same-user readable — worse than a JSON-RPC message, better than the `--config` command line.

It is built once, inside `_ensure_client`, so an agent that never runs a turn never asks the harness for a server at all. Nor does one with nothing to serve, and that is two cases rather than one: an empty `mcp_tools`, and an `mcp_tools` whose every name the host has no `ep_tool` for. The runtime settles the second itself, by asking the same `unregistered_tools` predicate `resolved_config` uses — no server needed — so it can emit the §7.5 warning *and* decline to bind a port, which are only compatible because the rule is a predicate rather than something the server decides. It hands `register_agent` only names that already resolve, so `register_agent`'s own warn-and-skip does not fire a second time on this path; that warning stays as the safety net for any other caller.

Selection is one list, resolved against the global `ep_tool` registry by name:

```toml
mcp_tools = ["DeskCreateTask", "DeskGetOrder"]   # default []; "*" is not accepted
```

A name with no matching `ep_tool` entry is a `logger.warning` naming the agent and the name, and is skipped — a host that renames a tool gets a visible line, not a startup crash. An empty list means no MCP server is started for that agent at all.

There is **no registration-time `mcp_exportable` flag**. Exposure is a deployment decision, the list is explicit, and a second gate at registration would only add a way for the two to disagree.

**No automatic de-duplication against native tools, either.** The list is explicit: not writing `Bash` or `WebSearch` is what keeps them out. Guidance, not mechanism: expose the host's domain API, and leave file, shell, and web search to the runtime's own tools, which are already good and already covered by the subscription. Detecting "overlap" across two vendors' tool vocabularies would be guesswork.

Tool execution stays in the BOS process, under BOS's own tool implementations. The MCP server is an entry point, not a second execution path.

### 3.9 Parameter adaptation

| `Agent.run` parameter | Claude Code | Codex |
|---|---|---|
| `chat_id` | `resume=<session_id>` (§3.6) | `thread_resume(thread_id)` (§3.6) |
| `content` — text | user message content | `TextInput` |
| `content` — `ImagePart` | image content block | `ImageInput(url=<data url>)` / `LocalImageInput(path=…)` |
| `content` — `FilePart` | path injected as text | `MentionInput(name, path)` |
| `llm_args["model"]` | `options.model`; mid-session `set_model()` | `thread.turn(model=…)`, per turn |
| `llm_args["reasoning_effort"]` | `options.effort` (`low`…`max`) | `thread.turn(effort=…)` |
| `schema` | `options.output_format={"type": "json_schema", "schema": …}` → `ResultMessage.structured_output` | `thread.turn(output_schema=…)` |
| `max_schema_retries` | the injected `StructuredValidator` validates; on failure, one correction message per retry (BEP 12 semantics preserved) | same |
| `interrupt` callback — **truthy return** (a *message*, `agent.py:600-602`) | must be delivered *into* the running turn, which then continues; the exact primitive is pinned in Layer 4b against the installed `claude-agent-sdk` | `AsyncTurnHandle.steer(input)` — "Send additional user input to this active turn"; the same handle keeps streaming afterwards |
| `interrupt` callback — **raised `AbortTurn`** (the *stop*) | `ClaudeSDKClient.interrupt()` is reserved for this path | `AsyncTurnHandle.interrupt()`, bounded and best-effort — unwinding BOS-side alone would leave the child running against `cwd` |
| ↳ what the caller gets on an abort | `ABORTED_TURN_CONTENT` with `finish_reason="aborted"`, as `Agent` does (`agent.py:837-840`) — **not** a re-raise | same |
| `request_stop()` | same, raced against the native turn | same |
| `event_sink` | `receive_response()`: `ToolUseBlock`→`tool`/`start`, `ToolResultBlock`→`tool`/`finish`, `TextBlock`→`response`, `ResultMessage`→`turn`/`finish` | `AsyncTurnHandle.stream()`: `item/started`·`item/completed`→`tool`, `turn/completed`→`turn`/`finish` |
| `turn_id`, `ctx_metadata`, `commit_observer` | BOS-side, unchanged | same |
| cfg `system_prompt` — appends (§3.4.1) | `system_prompt={"type": "preset", "preset": "claude_code", "append": …}` | `thread_start(developer_instructions=…)` |
| cfg `base_instructions` — replaces (§3.4.1) | `system_prompt=<str>`, the plain-str form | `thread_start(base_instructions=…)` |
| *neither set* | `system_prompt={"type": "preset", "preset": "claude_code"}` — **never left at `None`**, §3.4.1.3 | both omitted; runtime defaults stand |
| cfg `cwd` | `options.cwd` | `thread_start(cwd=…)` |
| cfg `max_iterations` | `options.max_turns` — see note below | **no counterpart — dropped** |

The `interrupt` rows are split because the callback's **name is not its meaning**, and conflating the two is a live source of inverted implementations. It is a poll, and its *return value* is a message to merge into the turn that is still running (`Agent._interrupt`: `ctx.add_message(llm_message, merge=True)`); `AgentActor._make_interrupt` returns exactly that for a queued `INTERRUPT_MESSAGE`, a user's follow-up sent mid-turn. Stopping is a different signal entirely — a raised `AbortTurn`, or `request_stop()` — never a truthy return. Two consequences the earlier one-row form hid: reading the row as "fires → stop" kills the user's follow-up instead of delivering it (Codex Layer 4a shipped that and had to be fixed), and the poll is **destructive** — it pops the pending envelopes — so a runtime must not poll once its turn has terminated, or it takes a message it can no longer deliver.

`AgentResult` is populated from `ResultMessage` (`usage`, `total_cost_usd`, `num_turns`, `terminal_reason`) or `TurnResult` (`usage`, `status`, `error`, `duration_ms`). `finish_reason` carries the native terminal reason verbatim.

**Dropped on purpose**, with one `logger.debug` line at construction listing whichever were set: BOS `tools` / `exclude_tools` (the runtime has its own; the host's reach it via §3.8), BOS plugins, `consolidator` and all compaction, `interceptor`, `max_tokens`, `tool_noise_filter`, `history_attribution`. None of these has a counterpart, and forwarding them would be a lie about what the runtime honours. `max_iterations` is deliberately not in this list, because the row above still means to forward it to Claude Code's `options.max_turns` — but as shipped (Layers 1–3), it is not yet special-cased per runtime anywhere: `bos/extensions/runtimes/_shared.py`'s `_DROPPED_KEYS`, the one set `parse_external_config` consults for *both* runtimes, includes `"max_iterations"`, and its own comment calls it a key "with no counterpart in either runtime" — which treats this table's Codex column as the whole truth and contradicts the Claude Code column above. That is a statement about what Layers 1–3 have wired up, not a considered reversal of this row: for the mapping above to hold, `ClaudeCodeAgent.__init__` (Layer 4) must pop `max_iterations` out of its config and translate it to `options.max_turns` *before* handing the remaining keys to `parse_external_config`, so the shared drop only ever fires for Codex, which truly has no counterpart. Until Layer 4 does that, treat the Codex column as correct today and the Claude Code column as the design target, not yet true.

### 3.10 Lifecycle, concurrency, timeouts, auth

#### 3.10.1 Clients and concurrency

Codex: one `AsyncCodex` per `CodexAgent`, started lazily, owning one `codex app-server` child; threads are per `chat_id`. Claude Code: one `ClaudeSDKClient` per turn with `resume=`, disconnected in `finally`.

```python
# ponytail: a client per turn costs one CLI spawn (~1s). A per-chat_id session pool
# is the upgrade if that latency shows up; resume= makes the stateless version correct.
```

Two concurrent turns on one `chat_id` are rejected with a busy error rather than queued — the native session is single-threaded and BOS does not hide that.

#### 3.10.2 Timeouts and shutdown

`timeout_seconds` wraps the turn in `asyncio.timeout`; on expiry the native turn is interrupted, then the error is raised.

Every wait that a wedged child could otherwise use to hold a **shutdown** open is bounded. Two of them are RPCs the child answers at its leisure — the interrupt request and the `auth = "subscription"` preflight — and for both, guarding against *errors* is not the same as bounding against *slowness*: each sits on a queue read with no timeout of its own, so a child that never answers simply never returns. The preflight's bound is separate and much more generous than the teardown graces, because it is a credential check against a live service and a slow but working login must not trip it. Closing the client bounds itself (the vendor terminates, waits, then kills). Thread setup (`thread_start` / `thread_resume` / the turn request) runs before the turn's `asyncio.timeout` window exists, so each of those calls carries `timeout_seconds` itself, and a setup timeout names its phase so it is never mistaken for a turn that timed out while streaming — in particular, a wedged child on the resume path is **not** reported as the session-continuity error of §3.6, which would send an operator looking for a corrupt session. A setup timeout interrupts nothing, and on the turn request that is a limitation rather than a saving: the native turn may already be running and BOS cannot stop it (§8.2, *An orphaned Codex turn*). It ends when the client closes, which is the honest limit of the sentence below.

`timeout_seconds` bounds **one native turn attempt**, not the whole `run()` call. That is what it already meant: each schema-validation retry gets its own fresh window, so a retried turn can take a multiple of it. Extending the same per-attempt bound to setup is consistent with that; making it a whole-call deadline would be a different promise and is deliberately not made. With `timeout_seconds` unset there is no bound anywhere, because the caller declined one — a wedged setup is still recoverable, since no turn task is registered yet, so `aclose()` takes the uncontended client lock and closing the client fails the pending request.

`aclose()` asks every in-flight turn to stop, gives them a bounded window to drain, then closes the client regardless — closing the client is what reaps the child, so it must not be reachable only on the happy path. This is a bounded drain, not a guaranteed one: a turn that ignores both the interrupt and the cancel is abandoned to the event loop (the same doctrine as `Agent._abandon`) and reported in a warning naming how many were left. What the child is doing does end, because the client closes; what BOS cannot promise is that the in-process task tracking it has finished first, or — for a turn orphaned by a timed-out turn request (§8.2) — that anything short of closing the client could have stopped it sooner.

#### 3.10.3 Auth

`auth = "subscription"` (default) means the native runtime's existing login. Preflight at `create_agent`: Codex calls `AsyncCodex.account()` and fails when no account is present; Claude Code fails when `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set in the environment the subprocess would inherit, because that silently converts a subscription run into a billed API run. `auth = "api_key"` is the explicit opt-in that suppresses the check. BOS never stores a token.

#### 3.10.4 Packaging

Two independent extras, out of `all` (the Claude wheel alone is 97 MB on linux x86_64), in the dev group so pyright checks the runtimes against real SDK types and a fake-backed suite can be written against real shapes:

```toml
claude-code = ["claude-agent-sdk==0.2.159", "mcp>=2.0,<3", "bos-ai[gateway]"]
codex       = ["openai-codex==0.156.1",     "mcp>=2.0,<3", "bos-ai[gateway]"]
```

`mcp` is pinned to 2.x explicitly. `claude-agent-sdk` allows `>=1.23,<3`, and the two majors are not compatible: 1.x's `FastMCP` became `mcp.server.mcpserver.MCPServer` in 2.x, and `mcp/server/fastmcp.py` in 2.x exists only to raise `ModuleNotFoundError` with a migration hint. `claude-agent-sdk` itself imports only `mcp.server.Server`, `mcp.types` and `mcp.shared.*`, all of which 2.x still exports. `bos-ai[gateway]` supplies starlette and uvicorn for §3.8.

A missing extra surfaces as an actionable error at `create_agent` naming the extra to install — not an `ImportError` traceback at first turn.

### 3.11 Look-alikes

| Pair | Distinction |
|---|---|
| `Agent` vs `AgentPort` | `AgentPort` is what a host may rely on; `Agent` is BOS's own implementation of it. `create_agent` returns the port. |
| `bos.core.agent.Agent` vs a Claude Code *subagent* | The latter is internal to the native runtime; BOS never sees it. `get_subagent_messages` exists but is out of scope. |
| `permission` (BEP 19) vs `[runtime]` / `[harness]` | `permission` is an agent key. The agent kind name is the runtime switch — there is deliberately no `runtime =` or `harness =` agent key, both of which would collide with existing top-level sections. |
| `cwd` (BEP 19) vs `workspace` (`AgentHarness`) | `cwd` is a subdirectory of the workspace and is the confinement root. The workspace is the harness's. |
| `mcp_tools` vs `tools` | `tools` is BOS's own include list and is dropped for external runtimes (§3.9). `mcp_tools` is the MCP egress allowlist. Different mechanisms, no overlap. |
| A reserved kind vs an agent that inherits it | `codex` is the runtime's name and the zero-config agent. `george` (`_parent: codex`) is a named instance with its own prompt, `cwd` and permission; `AgentPort.name` reports `george`. §3.4. |
| `system_prompt` vs `base_instructions` | Both are "the prompt". On an external runtime `system_prompt` is *appended* to the harness's own and `base_instructions` *replaces* it. On a BOS `Agent` there is no harness prompt, so `system_prompt` is the whole thing — the same key, the same intent, a different base. §3.4.1. |
| BOS `system_prompt` vs `CLAUDE.md` / `AGENTS.md` | `system_prompt` is per-agent config BOS sends over the wire. The project docs are files under `cwd`, read by the runtime itself. Three layers, §3.4.1. |
| Codex `Sandbox` vs Claude `SandboxSettings` | An OS filesystem sandbox vs bash-command isolation only. §3.5. |

### 3.12 Hosts that hold several workspaces

BOS is a library. The first host embeds it and may manage several workspaces inside one application, so "per process" and "per workspace" are not interchangeable here, and this section states which is which for everything BEP 19 adds.

**Everything BEP 19 adds is per harness, and a harness is per workspace.** The two runtime objects are built by `create_agent` and tracked in that harness's `_owned`; each `CodexAgent` owns its own `codex app-server` child; each `ClaudeCodeAgent` spawns and disconnects its CLI child within one turn; the MCP server (§3.8) is one per harness, on an **ephemeral** loopback port — never a fixed one — with a bearer token per agent. Two workspaces in one process therefore get two MCP servers on two ports with disjoint tokens, and closing one harness touches nothing the other owns.

**Three things are process- or user-global, and the design has to live with each:**

1. **`ep_tool` is a process-global registry.** The MCP egress reads it by name (§3.8), so two workspaces in one process draw from one pool of tool names. The `mcp_tools` allowlist is per agent and is what scopes exposure, but it cannot disambiguate two workspaces' extensions registering different implementations under one name — `ExtensionPoint.register` logs a warning and the last writer wins ([`registry.py:67-70`](../../src/bos/core/registry.py)). That is BEP 4's property, not something BEP 19 changes; a host wanting per-workspace tool implementations must either namespace the names or keep the workspaces in separate processes.
2. **Native session storage is per OS user, not per workspace.** Claude Code writes transcripts under `~/.claude/projects/`; Codex keeps threads under `CODEX_HOME` (default `~/.codex`). Reads are safe because BOS always addresses a session by the explicit id it stored (§3.6) and passes the resolved `cwd` as `directory` to `get_session_messages`, so lookups are scoped and ids do not collide across workspaces. What is *not* isolated is the storage itself: one workspace's operator-visible transcript directory is every workspace's. Relocating Codex's would mean moving `CODEX_HOME`, which also holds `auth.json` and would break subscription login (§8.2).
3. **The environment.** The Claude auth preflight (§3.10.3) reads `ANTHROPIC_API_KEY` from the process environment, which is shared. The check is per agent and read-only, so it is correct either way; but because `ClaudeAgentOptions.env` and `CodexConfig.env` are per client, each runtime passes an explicit environment to its child rather than letting it inherit whatever another workspace's `[platform.envs]` wrote.

**The pre-existing blocker, named rather than solved.** A host cannot hold two workspaces open *concurrently* today, and BEP 19 does not change that. `BosApp.__aenter__` refuses a second live instance in one process, and its own comment gives the reason: `bootstrap_platform()` writes `os.environ` and rebuilds the agent registry, so two would overwrite each other ([`sdk/_app.py:20-27`, `:51-58`](../../src/bos/sdk/_app.py)). `open_harness` has the same hazard, because it calls the same `bootstrap` ([`sdk/_bootstrap.py:14-32`](../../src/bos/sdk/_bootstrap.py)), and the shared state is `AgentRegistry`'s class-level dict, the `[exts]` defaults merged into process-global `ExtensionPoint` objects, and `[platform.envs]`. Sequential use — open, use, close, open the next — works today and is what §4.1 shows.

Making concurrent multi-workspace hosting work means moving that state off the process, which is a BEP 18 / BEP 6 change with its own fallout and is out of scope here. What BEP 19 owes it is not to deepen the hole: every criterion in §7 that could be affected is asserted per harness, and §7.27 pins that two harnesses opened in sequence leave no shared runtime state behind.

---

## 4. Audience flows (end state)

### 4.1 Embedder

```python
async with BosApp(config, bos_dir=".bos") as app:
    coder = await app.build_agent("codex", agent_cfg={
        "cwd": "services/api", "permission": "workspace-write",
        "mcp_tools": ["DeskCreateTask"], "timeout_seconds": 1200,
    })
    reply = await coder.ask(chat_id, "Add a health endpoint and run the tests.")
    for message in await app.get_messages(chat_id):   # full native transcript
        render(message)
```

The same `chat_id` on the next call resumes the same Codex thread. `app.agent("george")` works without `build_agent` when the workspace ships an `agents/george.md` with `_parent: codex` (§3.4) — which is the shape most projects should use, since the agent then has its own name and its own prompt file.

A host holding several workspaces opens them **one at a time** (§3.12) and picks the prompt route that matches where its prompts live — the workspace's own `agents/george.md` when the prompt belongs to the project, or `agent_cfg` on a bare reserved kind when the host keeps prompts per tenant instead of shipping a named agent file (§3.4.1.1 — `george` would already be cached by `__aenter__` the moment a workspace ships that file):

```python
for ws in tenant_workspaces:                       # sequential: BosApp is one-per-process
    async with BosApp(ws) as app:
        coder = await app.build_agent("codex", agent_cfg={
            "permission": "read-only",
            "system_prompt": prompts.for_tenant(ws.name),   # a resolved string, never a path
        })
        await coder.ask(chat_id_for(ws), task)
```

### 4.2 End user

Through a BOS agent, unchanged: the main agent calls `AskSubagent(role="coder", task=…)` and the external runtime executes, because `AgentRunner` resolves a role to an agent kind (§1.2). Directly: `boscli ask --agent codex "…"`.

### 4.3 Operator

- `boscli inspect agent codex` reports the resolved runtime, the absolute `cwd`, the `permission` level, the resolved `mcp_tools` with any unmatched names, and whether the extra is installed and the native login present (§3.3.1).
- A missing extra, an absent login, an escaping `cwd`, an unknown config key, and `workspace-write` on Claude Code where the bash sandbox is unavailable all fail at `create_agent` with a message naming the fix.
- Unmatched `mcp_tools` names appear as warnings, once, at build.
- Native transcripts are where the runtime puts them; `app.get_messages(chat_id, source="bos")` is the record BOS guarantees (§3.7).

### 4.4 Background / automated

An external agent bound to an actor runs under the gateway like any other: turn admission, interrupt, event fan-out and shutdown drain all work through `AgentPort`. Shutdown calls `request_stop()`, which interrupts the native turn; `aclose()` reaps the child. Nothing waits on an approval (§3.5.4), so an unattended run cannot hang on one.

---

## 5. Compatibility and fallout

### 5.1 Breaking: four signatures widen from `Agent` to `AgentPort`

`create_agent`, `BosApp.agent`, `BosApp.build_agent`, `AgentActor.__init__`. At runtime nothing changes — `Agent` satisfies the protocol. For an embedder type-checking against `bos.sdk`, a call to an `Agent` member outside `AgentPort` on the *result* of these now fails. No such member is called anywhere in this repo (§3.3).

`bos.sdk.__all__` grows by **five** names, not one: `AgentPort`, plus `MessageContent`, `TextPart`, `ImagePart`, `FilePart` (34 → 39). Only three of the four content names are forced. `AgentPort.ask`/`AgentPort.run` annotate `content` as `MessageContent`, a bare `TypeAlias` (`str | list[MessageContentPart]`); `test_promised_ports_are_implementable_from_the_contract_alone` calls `typing.get_type_hints()` on every promised Protocol's methods, and `get_type_hints` inlines a plain `TypeAlias` at the call site — so the test never sees the name `"MessageContent"` at all, only the `TextPart` / `ImagePart` / `FilePart` TypedDicts the alias expands to, and only those three are what the test actually requires `__all__` to contain. `MessageContent` itself is promised by hand, alongside them: an implementer building a non-string `content` value from the contract alone needs the alias's own name, and promising the leaves but not the union the signature is written in terms of would be an odd contract to hand someone. (`bos.sdk/__init__.py`'s own comment on the `FilePart, ImagePart, TextPart` import spells out this same distinction — an earlier version of that comment claimed the test forced all four, which was corrected once the mechanism above was traced through `typing.get_type_hints`.) BEP 18 §3.8's contract list now carries all five new names (BEP 18 §9, 2026-09-24), and both `test_the_contract_surface_is_importable_and_identical` and `test_promised_ports_are_implementable_from_the_contract_alone` pass with `AgentPort` in `__all__`.

### 5.2 Breaking: `[agent.defaults]` no longer reaches the two reserved kinds

Only observable in a project that both sets `[agent.defaults]` and uses a reserved kind — impossible before this BEP, since the kinds do not exist. Recorded because the registration loop's uniformity is what changes (§3.2.1).

### 5.3 Not breaking

`Agent` is unmodified. `ChatStore` is unmodified (§3.7). `ep_tool`, `ep_agent`, `SubagentPlugin` and the plugin lifecycle are unmodified. `boscli inspect` gains a branch; its output for a BOS agent is unchanged. `BosApp.build_agent`'s new parameter is optional. A base install with no extras imports `bos.sdk` and builds BOS agents exactly as before.

### 5.4 Third-party impact

Two new optional dependencies, each pinned exactly, each carrying a vendor CLI binary. Both vendors ship these SDKs under their own release cadence and Codex's app-server protocol is marked experimental upstream — hence exact pins and a §7 criterion that the pinned versions are the ones tested. An extension that subclasses `Agent` is unaffected. An extension that type-annotates against `create_agent`'s return type must widen with it.

---

## 6. Implementation plan (dependency-ordered)

**Layer 1 — the seam, no SDKs.**
1. `AgentPort` in `bos/core/agent/contract.py`; export from `bos/core` and `bos.sdk.__all__`; widen the four signatures (§3.3, §5.1). Update BEP 18 §3.8's list. Tests: `test_the_contract_surface_is_importable_and_identical`'s hardcoded `expected` set gains `"AgentPort"` plus the `MessageContent`/`TextPart`/`ImagePart`/`FilePart` names `AgentPort.ask`/`run` pull in (34 names → 39; §5.1 has the breakdown), and `test_promised_ports_are_implementable_from_the_contract_alone` passes for it; existing suites unchanged.
2. `EXTERNAL_AGENT_KINDS` and the `create_agent` branch (§3.2), against a `FakeExternalAgent` registered only in tests. Register the agent in `_owned`. Then the `_parent` half: `_EXTERNAL_RUNTIME_SPECS` merged into the `_resolve_agent_inheritance` call only (§3.4), and the `[agent.defaults]` skip keyed on the reserved name **or** a resolved `external_runtime` (§3.2.1). Tests: `george` with `_parent: codex` dispatches to the runtime while `AgentPort.name` stays `george`; `[agents.codex]` terms reach `george`; a hand-written `external_runtime` is rejected; no phantom `codex` in `AgentRegistry.describe()` when nothing names it.
3. `boscli inspect`'s non-`Agent` branch (§3.3.1).
4. `BosApp.build_agent(kind, agent_cfg=None)` and `BosApp.get_messages(...)` with `source="bos"` only (§3.7).

**Layer 2 — shared runtime scaffolding, still no SDKs.**
5. Config parsing and strict validation shared by both runtimes: `cwd` resolution and containment, `permission`, `system_prompt` / `base_instructions` (mutually exclusive), `external_runtime` rejected when hand-written, `timeout_seconds`, `auth`, `mcp_tools`, `native_options`, unknown-key rejection (§3.4, §3.4.1, §3.5.1).
6. Session mapping: write on commit, read back from `chat_store` (§3.6). Tested against the fake.
7. The thin two-message commit and `AgentResult` assembly (§3.7, §3.9).

**Layer 3 — the MCP egress.** Depends on 5.
8. `BosToolMcpServer`: `MCPServer` over selected `ep_tool` entries, `streamable_http_app`, loopback uvicorn, per-agent bearer token and subset, warn-and-skip on unmatched names, lazy start, harness-bound shutdown (§3.8). Tested with an MCP client in-process — no vendor SDK needed.

**Layer 4 — the two runtimes.** Each depends on 1–7; 8 is optional per runtime.
9. `CodexAgent` (§3.5.2, §3.5.4, §3.9, §3.10). Includes the pinned approval-handler-attribute test.
10. `ClaudeCodeAgent` (§3.5.3, §3.9, §3.10). Includes the `setting_sources` default and the Windows `workspace-write` refusal.
11. `get_messages(source="native"|"auto")` for both (§3.7).

**Layer 5 — packaging, docs, live validation.**
12. The two extras, the dev group, the `all` exclusion, the actionable missing-extra error (§3.10.4).
13. Live validation against real subscription logins for both runtimes: start, multi-turn resume, resume after process restart, cancel, timeout, a denied out-of-root write, quota-exhausted and login-expired surfaces. Results recorded in §8.1 as observed, not assumed.
14. Docs: an *External runtimes* page covering §3.4, the §3.5 asymmetry verbatim, and §3.8; release note for §5.1.

---

## 7. Acceptance criteria

**Satisfiable without a vendor SDK or network (Layers 1–3):**

1. `create_agent("codex")` with no `permission` key raises at construction, and the message names the key and its three values.
2. `cwd = "../outside"` raises at construction, naming the resolved path and the workspace.
3. An unknown key under `[agents.codex]` raises, naming the key.
4. `[agent.defaults] model = "x"` plus `[agents.codex]` yields a `CodexAgent` whose resolved native model is not `"x"`.
5. `mcp_tools = ["NoSuchTool"]` logs exactly one warning naming the agent and the tool, and starts no MCP server.
6. An in-process MCP client with agent A's bearer token lists and calls only A's subset; agent B's token cannot reach A's tools; a request with no token is rejected.
7. `harness.__aexit__` leaves no live MCP server and no child process.
8. `tests/test_sdk.py::test_the_contract_surface_is_importable_and_identical` and `::test_promised_ports_are_implementable_from_the_contract_alone` pass with `AgentPort` in `__all__`.
9. `boscli inspect agent <external>` succeeds and reports runtime, absolute `cwd`, `permission`, resolved `mcp_tools`, and any of those names the host has no `ep_tool` for — in text mode, which is the default, not only under `--json`. Read from the runtime's `resolved_config` (§3.3.1), so it holds on an agent that has never run a turn.
10. `import bos.sdk` on a base install with no extras succeeds; `create_agent("codex")` there raises an error naming `bos-ai[codex]`.
11. An `agents/george.md` whose frontmatter is `_parent: codex` and whose body is a prompt builds a Codex-backed agent whose `name` is `george` and whose `system_prompt` is the file body. `[agents.codex] cwd = "x"` reaches `george` unless `george` overrides it. A workspace naming neither reserved kind has no `codex` entry in `AgentRegistry.describe()`, and `resolve_default_agent()` is unaffected.
12. A hand-written `external_runtime` key raises, and the message names `_parent` (§3.4). Setting both `system_prompt` and `base_instructions` raises (§3.4.1).
13. With `system_prompt` set, the Claude Code command carries `--append-system-prompt` and **not** `--system-prompt`; with neither prompt key set, it carries **neither** flag; with `base_instructions` set, it carries `--system-prompt <text>`. Asserted on the built command, not on the resolved config, because the defaulting trap in §3.4.1.3 is invisible at the config layer.
14. `uv run pytest -q`, `uv run ruff check src tests examples`, and `npx -y pyright src` are green, pyright at zero errors.

**Requires a real subscription login (Layer 4–5, per runtime, recorded in §8.1):**

15. A turn starts, streams `TurnEvent`s that a host renders, and returns an `AgentResult` with non-empty `usage`.
16. A second `ask()` on the same `chat_id` continues the same native session — asserted by the native session/thread id, not by the model's reply.
17. After a BOS process restart, a third `ask()` on that `chat_id` resumes it, recovered from `ChatStore` metadata alone.
18. Under `permission = "workspace-write"`, a write inside `cwd` succeeds and a write outside it **fails** — observed, per §3.5.5. Under `read-only`, every write fails. And when the agent asks to escalate past the sandbox, the request is **refused, the agent continues the turn**, and a WARNING naming the method appears in the log — no auto-accept. This is the only way to confirm that §3.5.4's refusal values are accepted by a live `codex app-server`; CI cannot.
19. `request_stop()` mid-turn interrupts the native turn, which stops writing to the workspace, and the turn returns what it had produced with `finish_reason = "interrupted"`. Preconditions: the child confirms the interrupt within the grace — one that does not is abandoned (§3.10.2) and keeps writing until the client closes. Reaping the child is `aclose()`'s job, not `request_stop()`'s; `request_stop()` only sets the flag each in-flight turn races.
20. `timeout_seconds` expiry raises. Precondition: it bounds one turn *attempt*, not the whole call — each schema retry gets a fresh window. Expiry while the turn is streaming interrupts the native turn first. Expiry during setup interrupts nothing and says which phase it was: for `thread_start`/`thread_resume` nothing had started, but for the turn request the child may have started the turn anyway and BOS has no id with which to stop it — the §8.2 orphan limitation, observable only on a live server (§6 step 13).
21. `schema=` returns validated structured output through each runtime's native mechanism.
22. With `mcp_tools` set, the runtime lists and successfully calls the exposed BOS tool, and an unexposed `ep_tool` is not callable.
23. `app.get_messages(chat_id)` returns the native transcript — the user and assistant messages in the runtime's own session, in order, including ones BOS never authored — while `source="bos"` returns the two-message-per-turn record. Three preconditions, all of them §3.7's rules and all of them things a tester running this against a live server will otherwise read as a mismatch: (a) *messages*, not tool activity — projecting it would mean forging the `tool_calls`/`tool_call_id` pairing BOS uses and Codex does not have, and an earlier draft of this criterion said "including tool activity" and was wrong about what a `Message` can carry; (b) Codex `commentary`-phase agent messages are **dropped**, so a turn the `codex` CLI renders with visible commentary yields fewer assistant messages here than the CLI shows; (c) a turn the runtime reports as not fully loaded appears as one gap marker rather than as missing messages.
24. Quota exhaustion and an expired login each surface as a distinct error; neither falls back to API-key billing.
25. With `system_prompt` set, the runtime's reply reflects it while its own tool guidance still works — the pairing §3.4.1 protects. With `project_doc_max_bytes = 0` in `native_options`, Codex's reply no longer reflects an `AGENTS.md` placed in `cwd`; if it still does, §3.4.1's sentence about that knob is corrected rather than the behaviour claimed.
26. Criteria 15–25 hold at the pinned SDK versions, and those versions are what CI installs.

**Multi-workspace (§3.12), satisfiable with fakes:**

27. Two harnesses opened and closed **in sequence** over different workspaces each get their own MCP server on a different ephemeral port with disjoint bearer tokens, and after the second closes no port is listening, no child process survives, and nothing BEP 19 added remains in process-global state.

---

## 8. Open questions

### 8.1 Readiness by track

| Track | Status |
|---|---|
| Layers 1–3 (seam, scaffolding, MCP egress) | **Shipped**, this branch. §7 criteria 1–12, 14 and 27 pass at the layer that now exists — §9's 2026-09-24 entry says exactly which test proves which criterion and where the proof stops short of a real runtime. Criterion 13 needs `ClaudeCodeAgent` and moves to Layer 4. The gaps Layer 4/5 inherit are below, in §8.2. |
| Layer 4 Codex | **Built**, this branch — `CodexAgent` and its MCP egress wiring. §7.15–26 remain **unverified** until run against a real ChatGPT login, with one exception: §7.22's first clause — the runtime *lists* the exposed BOS tool over MCP — is proven in CI against the real `codex app-server`, which needs no login to start a thread. Its other two clauses still wait on one (§8.2). The vendor-module-classification gap and the empty-`mcp_tools` half of §7.7 are closed; the child-process half of §7.7 and §7.27 is not, and cannot be until a real `codex app-server` is spawned — which is the same live run. |
| Layer 4 Claude Code | **Not started.** Implementable, with §7.13 and §7.15–26 unverified until run against a real login; plus §3.5.3's confinement is BOS-built and must be validated behaviourally before `workspace-write` is documented as safe, and `setting_sources` must be added to `_shared.py`'s known-key set before §3.4's own `[agents.claude-code]` example stops raising "unknown key" (§8.2). |
| MCP egress on a shipped install | **Unblocked for Codex.** `bos-ai[codex]` pins `mcp>=2,<3` and depends on `bos-ai[gateway]`, which is where `starlette` and `uvicorn` come from, so `BosToolMcpServer.start()` works on that extra rather than only in a dev checkout. Still open for Claude Code, which has no extra yet (§3.10.4). |
| Concurrent multi-workspace hosting | **Blocked, and not by this BEP.** `BosApp` refuses a second live instance per process and `bootstrap` rewrites process-global state (§3.12). Sequential use works, and is now proven for the process/port/token trio by `test_two_sequential_harnesses_share_no_runtime_state` (§7.27) — not for child-process cleanup, which needs Layer 4. Everything BEP 19 adds is already per harness, so lifting the blocker elsewhere does not require revisiting this BEP. |
| `workspace-write` on Windows | **Blocked.** Claude Code's bash sandbox is macOS/Linux only; the runtime refuses the combination until there is an enforcement story. |

### 8.2 Unresolved

- **Whether `full-access` should exist at all.** Kept because a trusted local dev loop is a real use case, but it is the one level where BOS enforces nothing. Candidate for requiring an explicit second opt-in.
- **Where process-global BOS state should live so a host can hold several workspaces at once.** `AgentRegistry`'s class dict, the `[exts]` defaults merged into `ExtensionPoint` objects, and `[platform.envs]` writing `os.environ` are the three (§3.12). A BEP 18 / BEP 6 question, listed here because the first host wants it and because BEP 19's §7.27 is the guard that this BEP does not add a fourth.
- **Claude Code `session_store`.** Would make transcripts workspace-local and portable (§3.7). Deferred; the argument for doing it is that `~/.claude/projects/` is the wrong home for a server's data.
- **An orphaned Codex turn, after a `thread.turn` timeout.** `timeout_seconds` bounds the turn request (§3.10.2), but cancelling it does not stop the work: `AsyncCodexClient._start_turn` submits to a module-level executor and awaits it through `asyncio.wrap_future`, and its cancel path only closes the orphaned *subscription* — the vendor's own docstring calls this "releasing an unclaimed result". The native turn therefore starts, and BOS cannot interrupt it, because `turn_interrupt` takes a turn id and the turn id is precisely what the cancelled call never returned. The turn ends when `aclose()` closes the client. Not a regression: before the bound the call hung *and* the turn ran — bounding it traded a silent hang for a silent orphan. No fix now, deliberately: recovery would mean reading the thread back to find an in-flight turn and building a handle for it, speculative work on a rare path over an RPC that can wedge the same way. Revisit if the vendor exposes the started turn id on the cancel path, or if §6 step 13 shows the orphan is common enough to matter.
- **Nothing pins the *set* of Codex approval methods.** `_APPROVAL_DENIALS` (§3.5.4) is keyed by the five approval methods in today's `ServerRequest`; a sixth added by a future `openai-codex` would fall through to the `{}` the other five server requests get. Deliberately not guarded, because the failure is loud rather than silent: `{}` fails `required` on every approval response shape in the schema, so the server sees a malformed response — not the auto-accept this handler exists to kill. A test *could* pin the set by regenerating the schema from the shipped binary and diffing the method list, which is why this is a watch item and not an impossibility; it was judged not worth a subprocess in CI for a failure mode that announces itself. Re-generate (`codex app-server generate-json-schema`) and diff when the `openai-codex` pin moves.
- **`get_messages(source="native")` cannot tell two agents on one runtime apart.** `commit_external_turn` records the *runtime* per turn (§3.6), not the agent kind, so when two built agents both declare `external_runtime = "codex"` — a bare `codex` and a `_parent`-inheriting `george`, say — nothing in the store says which served the chat. They are not interchangeable in general either: a Claude Code transcript is keyed by the agent's `cwd`, so the wrong one reads a different conversation. `BosApp.get_messages` therefore raises, naming both kinds and pointing at `app.agent(<kind>).native_messages(chat_id)`, rather than picking one. Revisit by adding the agent kind to the metadata `commit_external_turn` writes; deferred because that widens a stored schema every external turn already writes, for a case the first host does not have.
- **A Codex turn whose `items_view` is not `full` has no recovery path.** §3.7 makes it a visible gap marker, which is the honest answer but not the useful one: the messages genuinely are not in the `thread/read` response. The vendor does expose `thread/items/list` (`ThreadItemsListParams` takes `threadId`, an optional `turnId`, a cursor and a limit), which looks like the paged read that would materialize them, but `AsyncCodex` wraps no such method and `AsyncThread` has none — reaching it would mean a raw JSON-RPC call past the SDK's public surface. Revisit if `openai-codex` wraps `thread/items/list`, or if a real long-lived thread shows the markers are common enough to be worth the reach-past.
- **Nothing pins the *set* of `TurnItemsView` values either, and here the failure is total rather than per-turn.** §3.7's gap marker handles `notLoaded` and `summary`, and the check is written as "not one of the loaded values" so an unknown value would be treated as not-loaded — but it never gets there. `TurnItemsView` is a closed `Enum`, so a value a future `openai-codex` adds fails `ThreadReadResponse` validation inside the vendor's own `thread_read`, and `native_messages` surfaces that as "the native transcript … could not be read". There is no per-turn degrade to reach for: the response never parses, so the whole read dies, not one turn of it. Same family as the approval-method entry above and the same revisit condition — when the `openai-codex` pin moves, regenerate the schema (`codex app-server generate-json-schema`) and diff the enum.
- **The Codex MCP wiring is proven as far as `tools/list`, and no further.** §3.8's Codex half is `thread_start`/`thread_resume`'s `config={"mcp_servers": {"bos-tools": {"url": …, "bearer_token_env_var": …}}}` plus `CodexConfig(env={<that var>: <token>})`. This is no longer inference: `tests/test_codex_mcp_wiring.py::test_the_real_codex_child_reads_the_override_and_lists_the_tool` spawns the **real** `codex app-server` — no login needed for `thread_start` — and asserts that the child reads the override, connects to BOS's loopback server, authenticates with BOS's token and lists exactly the granted tool. The `config=` and `-c` channels are the same one, also measured: a `bearer_token` planted through either is refused with the same inner sentence, `bearer_token is not supported for streamable_http`. Only that sentence matches — each channel wraps it differently (`failed to load configuration:` over JSON-RPC, `failed to load bootstrap configuration` / `Caused by:` from the CLI), so it is the diagnosis that is shared, not the whole string. §7.22 has three clauses — the runtime lists the tool, calls it, and cannot call an unexposed one — and this proves the first. The third is proven, but at BOS's own server rather than through the child (`test_mcp_egress.py::test_a_tool_outside_the_allowlist_is_refused_without_executing`). The second needs a model turn and therefore a login, so it, and the third seen from the child's side, stay on Task 11's checklist. `CodexConfig(config_overrides=…)`, the `--config` passthrough, is no longer a fallback worth holding: it was there in case `config=` turned out not to be the channel, and it is; it would also put the bearer token on the child's command line for any local `ps`.
- **BOS's `[mcp_servers.bos-tools]` entry shares a namespace with the operator's own; the loud half of that is unresolved and the quiet half is closed.** `config=` *merges* rather than replaces, per key even for a whole-table override — overriding the table wholesale with a complete entry still left a `bearer_token_env_var` from `config.toml` in place. **Loud, unresolved — two of them, both total:** if the operator's same-named entry is **stdio**, the whole config stops loading ("url is not supported for stdio in `mcp_servers.bos-tools`"); if it is HTTP and carries `bearer_token`, that key is rejected for streamable HTTP wherever it comes from and the operator's copy is merged in beside ours, so the config stops loading too. Either way every turn fails, and at least the error names the table. **Live, and a different kind of neighbour:** a colliding entry carrying `http_headers_helper` makes Codex **execute a command** — measured, the operator's helper ran and wrote its marker file — even though the header it printed lost to BOS's. Sharing this namespace is therefore not only about which credential wins; it can also run code the operator configured for a different server. Mitigated only by the name — `_MCP_SERVER_NAME` is `bos-tools`, what the server already reports for itself over MCP, and less likely to be a table someone already wrote than `bos`. Not resolved further because the remaining fixes are worse: a random name is unreadable in the operator's own config, and `CODEX_HOME` cannot be relocated, for the reason below. **Quiet, now closed:** an earlier round shipped `http_headers`, and against an operator entry carrying `bearer_token_env_var` the child sent *theirs* — measured, not conditional. A 401 from `_gate` does not fail `thread_start`, and `_gate` logs no refusal while the server runs with `access_log=False`, so the agent would simply have had no BOS tools and nothing would have said so. BOS now sends `bearer_token_env_var` itself, which wins against each of the four credential keys the binary's own `codex mcp get --json` enumerates for a streamable-HTTP entry (§3.8), so this path no longer produces a silent toolless agent. The OAuth path named there is unmeasured and is the one shape that could still reopen it. `_gate`'s silence is still worth fixing on its own account — revisit by logging the refusal there, deferred because an unauthenticated endpoint that logs on every probe is its own problem.
- **Codex config isolation.** Codex reads `~/.codex/config.toml`, so the operator's machine config reaches the runtime the way `setting_sources` prevents for Claude Code. `CODEX_HOME` would relocate it, but `auth.json` lives there too, so relocating breaks subscription login. Unresolved; the asymmetry stands and is documented.

**Carried forward from Layers 1–3** (recorded in the Task 1–9 execution ledger). The Codex track of Layer 4 closed four of them. `boscli inspect`'s external branch now reads the runtime's `resolved_config` — which `ExternalRuntime` promises, so it is no longer a duck-typed reach at `.cfg` — and therefore reports the resolved absolute `cwd` and the real `permission` instead of `"."` and `None`. `_load_external_runtime` tells a missing vendor module apart from a broken runtime module. And `bos-ai[codex]` gives the MCP egress an install path outside a dev checkout. What is left:

- **`setting_sources` is not a known config key yet.** `bos/extensions/runtimes/_shared.py`'s `_KNOWN_KEYS` has no entry for it, so §3.4's own `[agents.claude-code]` example (`setting_sources = ["project"]`) would raise "unknown key" the day something routes a Claude Code config through `parse_external_config`. Both the Layer-2 implementation plan and this BEP's own Layer-2/3 scope lines omitted it; adding a key nothing reads yet would have been speculative. `ClaudeCodeAgent` (Layer 4) owns adding it, alongside the rest of §3.5.3.
- **`read_native_session_id` has no in-process cache.** §3.6 describes recovery after a restart as a `chat_store.get_messages(chat_id)` scan from the end; an in-process cache in front of that scan was listed as part of Layer 2 (§6) but was never built, so every call re-scans the store. It plausibly belongs on the Layer 4 runtime object, which is the thing that would actually own a per-`chat_id` cache's lifetime; re-check once `ClaudeCodeAgent` / `CodexAgent` exist.
- **"No child process survives harness teardown" (§7.7, §7.27) is still proven only where no child exists.** Both the Layer 1–3 double (`_FakeRuntime`) and the Codex double (`FakeAsyncCodex`) spawn nothing, so what the suite proves is the harness-side bookkeeping and that `aclose()` reaches `client.close()` — not that the `codex app-server` child is reaped. One test does now spawn a real child (`::test_the_real_codex_child_reads_the_override_and_lists_the_tool`), so this is no longer out of CI's reach on principle, but that test drives `agent.aclose()` directly and asserts nothing about the process. Closing it means asserting the pid is gone after `AgentHarness.__aexit__`, which needs a second reach into vendor privates (`client._client._sync._proc`) — revisit if a leak is ever observed, or when Task 11's live run reports on it. The other half of this entry is closed: "a runtime whose `mcp_tools` is empty never invokes the MCP-server accessor" (§3.1, the lazy half of §7.7) is now proven on the real `CodexAgent` by `tests/test_codex_mcp_wiring.py::test_an_agent_with_no_mcp_tools_never_asks_for_a_server` and, for the all-unknown-names case §7.5 asks about, `::test_a_config_naming_only_unknown_tools_warns_and_starts_no_server`. Both arm an `mcp` accessor that raises rather than one that counts — calling it is what builds the server, so one call is already the failure.

### 8.3 Resolved during design

- **Whether to subclass `Agent`** — no. §3.3.
- **Whether a `runtime = …` agent key selects the runtime** — no; the kind's name does, and `runtime` / `harness` would collide with existing top-level sections. §3.2, §3.11.
- **Whether to expose each runtime as start/continue/poll/cancel delegation tools instead of as an agent** — no. §1.2.
- **Whether `mcp_exportable` belongs on `@ep_tool`** — no; the `mcp_tools` list is the single gate. §3.8.
- **Whether `_parent = "codex"` should work** — yes, and it is the recommended shape. Reversed twice during design: first blocked to keep dispatch keyed purely on the name, then restored once the `.md` authoring route (§3.4.1.1) made named instances the natural home for a prompt. Dispatch is now "the reserved name, or the `external_runtime` inherited from it", which costs one dict merged into an existing resolver call. §3.2, §3.4.
- **Whether `system_prompt` should carry over to external runtimes** — yes, meaning *append*, with `base_instructions` as the explicit replace. An earlier draft renamed it to `instructions` and rejected `system_prompt`; that would have broken the `.md` route, where the body lands in `system_prompt` and no one writes the key by hand. §3.4.1.
- **Whether the prompt should be a path-valued config key** — no. Paths resolve against something, and in a multi-workspace host that something is ambiguous. Files are read by `resolve_agents()`, which knows the workspace root; everything below sees a resolved string. §3.4.1.1, §3.12.
- **Whether `chat_id` should encode the runtime** — no; stored metadata already carries it. §3.7.
- **Whether `ChatStore.get_messages` should transparently return native transcripts** — no; the read is additive on `BosApp`. §3.7.
- **Whether to use Claude's in-process SDK MCP server** — no; one HTTP server serves both runtimes with one implementation. §3.8.

---

## 9. Revision history

- 2026-09-24 — Final fix wave before merge, applying a whole-branch review of Layers 1–3. All four Important findings were the same shape: two individually-correct tasks composing into wrong behaviour, invisible to either task's own review. `BosApp.build_agent`'s `agent_cfg` was silently discarded whenever a kind was already cached — either because `__aenter__` pre-built every kind `[agents]` names (Task 4 × Task 8), or because an earlier `build_agent` call had; it now raises, naming the cache and what to do instead, and `build_agent(kind)` with no `agent_cfg` still returns the cache unchanged. `boscli inspect agent`'s `mcp_tools` line crashed with an unhandled `TypeError` on a non-list config value (e.g. `mcp_tools = 7`) instead of reporting it as malformed. `BosApp.get_messages`'s auto-routing scan used the compaction-active window while `read_native_session_id`'s identical backwards scan over the same metadata had already been fixed to use `active_only=False` (§3.6); a summary written over an externally-backed chat silently routed `source="auto"` to the BOS record instead of native — the routing scan now reads separately with `active_only=False`, while `source="bos"` keeps the active-window read §3.7 pins it to. `_ensure_tool_mcp_server` had no active-harness guard, unlike `create_agent`; a runtime object a host still held past harness teardown could build a fresh server into a cleared `_owned` and start a listener nothing would ever close. `parse_external_config`'s type sweep, which already hardened `mcp_tools`/`native_options` against a bare value where a structure belongs, is extended to `cwd`, `model`, `timeout_seconds`, `system_prompt` and `base_instructions` — `cwd = ["a", "b"]` no longer becomes a directory literally named `['a', 'b']`. `AgentPort` is now asserted directly against what `create_agent`'s reserved-kind dispatch returns, not only inferred from it not being an `Agent`.

  Three passages here were also wrong. §3.4.1.1 and §4.1 showed `build_agent("george", agent_cfg=…)` as the worked example for the programmatic prompt route; `george` is exactly the kind a project names via `agents/george.md`, so `__aenter__` would already have cached it and the shown call now raises. Both examples build a bare reserved kind (`codex`) the config leaves unbuilt instead, and §3.4.1.1 states the `BosApp`-cache caveat in prose. §4.3 and §7.9 wrote `boscli inspect --agent <kind>`; the real form is the `inspect agent NAME` subcommand, `--agent` is not an option on it, and both are corrected. §6 Layer 2 step 6 listed an in-process session-id cache as shipped, and §3.6 stated it in passing; `read_native_session_id` never grew one — moved to §8.2 as a Layer 4 carry-forward, and §3.6 now says so.

- 2026-09-24 — Implementation, Layers 1–3 (this branch, `bc0ddb1..1b5f474`, sixteen commits across nine tasks). §6's Layer 1 (the seam), Layer 2 (shared config/session/commit scaffolding) and Layer 3 (the MCP egress) are built and tested without either vendor SDK; Layer 4 (`ClaudeCodeAgent`, `CodexAgent`) and Layer 5 (packaging, live validation) have not started. §7 criteria 1–12, 14 and 27 pass; 13 needs `ClaudeCodeAgent` and is now explicitly Layer 4's; 15–26 need a real subscription login and stay unverified, as designed. Passing needs one qualification worth stating plainly: several criteria are proven at the shared module that now exists rather than through a live `create_agent("codex")` call, because no runtime class exists yet to call that module from its own `__init__`. Criteria 1–3 and the `system_prompt`/`base_instructions` half of 12 are pinned by `test_external_agent_config.py` calling `bos.extensions.runtimes._shared.parse_external_config` directly; the harness's own dispatch (`harness.py:440-453`) never calls it, and `_FakeRuntime` (`tests/conftest.py:127-148`, standing in for both reserved kinds via the `fake_runtimes` fixture) performs no validation at all — so `create_agent("codex", agent_cfg={})` under that fixture does *not* raise on a missing `permission` today; only `parse_external_config({}, ...)` does. Criterion 5's warning-and-skip half is fully proven end-to-end against `BosToolMcpServer` (`test_an_unmatched_tool_name_warns_and_is_skipped`); its "starts no MCP server" half rests on the same lazy-construction guarantee as criterion 7, which §8.2 already carries forward. Criterion 9 is a genuine partial: `boscli inspect` succeeds and correctly reports `runtime`, `permission` and `mcp_tools` (`test_inspect_reports_an_external_agent_without_touching_agent_internals`), but the `cwd` it reports is `cfg.get("cwd", ".")` — the configured value, not the absolute one `parse_external_config` would compute — contradicting the "absolute" language in §3.3.1, §4.3 and §7.9 itself; no test asserts otherwise, and this is the same gap Task 4's review recorded and §8.2 now carries forward. Criteria 4, 6, 8, 10, 11, 12's `_parent`-rejection half, and 27 are real, complete proofs at the layer they belong to: 4 through `AgentRegistry.get_defaults` after a genuine `Workspace.bootstrap_platform()` run, 6 and 27 through a real in-process `mcp.ClientSession` speaking streamable HTTP to a real `BosToolMcpServer`, 8 through `bos.sdk`'s actual `__all__` and `typing.get_type_hints`, 10 through the real `_load_external_runtime` import-and-report path (with only the target module name faked), 11 through `Workspace.resolve_agents()`/`bootstrap_platform()` on real TOML and Markdown fixtures.

  Four passages proved wrong and are corrected in place rather than footnoted. **§5.1** claimed `bos.sdk.__all__` grows by one name (`AgentPort`); it grew by five, 34 → 39 (`AgentPort`, `MessageContent`, `TextPart`, `ImagePart`, `FilePart`), and only three of the four content names are compelled by `test_promised_ports_are_implementable_from_the_contract_alone` — `AgentPort.ask`/`run` annotate `content` as the bare `TypeAlias` `MessageContent`, which `typing.get_type_hints()` inlines to its member TypedDicts before the test ever sees the alias's own name; `MessageContent` is promised by hand, and `bos/sdk/__init__.py`'s own comment on the point now says so. **§3.2**'s code sketch showed `EXTERNAL_AGENT_KINDS: dict[str, type]` mapping kinds straight to the runtime classes, and `merged_cfg.pop("external_runtime", None)`. Neither survived contact with the rest of the repo: `bos/core/harness.py` sits on the import path of a base install with no extras, so a dict of classes would force `import bos.sdk` itself to pull in both 97 MB vendor wheels — the shipped `EXTERNAL_AGENT_KINDS: dict[str, str]` (`harness.py:64-67`) holds `"module:Class"` strings that `_load_external_runtime` (`:72-91`) resolves with `importlib` on first build; and `.pop` would erase `external_runtime` from the very config object that becomes the runtime's own `cfg`, which `boscli inspect` reads back to answer "which vendor backs `george`?" — the shipped dispatch (`:440-453`) uses `.get`. **§3.8** described one `mcp.server.mcpserver.MCPServer` with per-agent filtering in an HTTP middleware; that mechanism cannot work at all, because a middleware sees a request and a response byte-stream, never the MCP-level `tools/list` body it would have to filter, so every agent would have seen every tool. The shipped `BosToolMcpServer` (`mcp_egress.py`) instead builds an `mcp.server.lowlevel.Server` with `on_list_tools`/`on_call_tool` handlers that read the caller's bearer token off `ServerRequestContext.request` — attached per inbound message by `mcp` 2.x's streamable-HTTP session runner (`streamable_http.py`'s `_message_metadata`, read back in `runner.py`'s `_make_context`), not per connection, which is what lets one kept-alive HTTP connection carry many agents' calls correctly. `MCPServer.streamable_http_app` was also the wrong class named for the method actually called — both classes expose a method by that name, but the object served is the lowlevel one — and `MCPServer.add_tool` turns out to have no schema parameter at all (it introspects the wrapped function's signature), which is the concrete reason a `**kwargs` closure over `ep_tool.invoke` could never have worked through it. **§3.9**'s table claimed `max_iterations` maps to Claude Code's `options.max_turns` while having no Codex counterpart; as shipped, `_shared.py`'s `_DROPPED_KEYS` — one set, read for both runtimes — drops it unconditionally, and its own comment calls it a key "with no counterpart in either runtime," contradicting the table's Claude Code column. This is not a considered reversal so much as Layer 2 arriving before Layer 4: the mapping stays the design target, but it will only be true once `ClaudeCodeAgent.__init__` pops `max_iterations` and translates it before handing the rest of its config to `parse_external_config`; a note beside the table row says so.

  The two passages flagged for verification rather than assumed correction held up. §3.4's `george.md` example (`_parent: codex`, a `cwd`, a `permission`, and an `mcp_tools` YAML block) parses exactly as shown — `_parse_simple_yaml_mapping`/`_parse_frontmatter_block` (`workspace.py:229-262,284-300`) support both the scalar and the indented-list form used there — and every other claim in §3.4's prose (the `_EXTERNAL_RUNTIME_SPECS` pseudo-factory table at `workspace.py:77-80`, its merge into `_resolve_agent_inheritance` at `:664`, the `externally_backed` check gating the `[agent.defaults]` skip at `:683`) matches the shipped `Workspace.bootstrap_platform()` line for line. One small, independently-verified correction rode along: §3.3.1 named the branch `_agent_info`; it shipped as `_agent_capabilities` (`inspect.py:163`), and the cited line range for the internals it reaches past has moved to `:204,219-223`. Both are fixed in place.

  Beyond the four corrections, the ledger and the code together surface findings the spec did not anticipate. `AgentPort` is a `Protocol` with a `name` **property**, so `issubclass(Agent, AgentPort)` raises `TypeError` — Python refuses `issubclass` on a Protocol with non-method members — and every conformance check in the suite had to become `isinstance` against a real, constructed `Agent` instead. `mcp`'s `ServerRequestContext` is generic over *two* type parameters (`LifespanContextT`, `RequestT`), and `RequestT` defaults to `Any`; annotating the handler context as `ServerRequestContext[Any]` type-checks but leaves `ctx.request` silently `Any`, so pyright verifies nothing about the one authorization-relevant attribute in the file — caught by a deliberate `.headerz` typo probe that zero-errored under `[Any]` and one-errored under `[Any, Request]`, and confirmed independently by a second reviewer reading the vendored type definitions. The reference sketches for `mcp_tools` and `native_options` in the Layer 2 plan both coerced a bare string into per-character list entries wherever `dict()`/iteration met an author's TOML typo; both are now explicit `ValueError`s (`_shared.py`'s two "must be a list"/"must be a table" checks) rather than silent corruption. `read_native_session_id`'s first cut called `get_messages(active_only=True)` — BOS's normal default — which would have made a session's `native_session_id` permanently unrecoverable the day anything wrote a compaction summary over an external chat; it now scans with `active_only=False`, proven by `test_the_session_id_survives_a_summary_written_after_the_turn`. A same-day fix for a blank-vs-whitespace session id introduced a worse bug — `session_id.strip() or None` strips the *returned* id, so a vendor id of `"  thread_abc  "` would come back edited — caught before merge and now covered by `test_a_session_id_with_surrounding_whitespace_is_returned_unchanged`, which is the more interesting result: `.strip()` is used only to test for blankness, never to transform the value handed back to the vendor. And the isolation guard written for §7.27 was itself shown to be weak before it shipped: two of its three assertions passed vacuously under the exact mutation they existed to catch, because clearing both tracked fields unconditionally in `__aexit__` satisfies an equality check regardless of whether a new object was actually built; only a direct `servers[0] is not servers[1]` identity check closes that gap, and it is what ships.

  What Layers 1–3 leave for Layer 4/5 is enumerated in §8.1 and §8.2 rather than here: `setting_sources` absent from `_shared.py`'s known-key set, `inspect`'s configured-not-absolute `cwd`, `_load_external_runtime`'s inability to yet distinguish a missing extra from a broken installed module, the duck-typed `agent.cfg` read in `inspect`, the `mcp`-is-dev-group-only packaging gap (`starlette`/`uvicorn` already ship via `bos-ai[gateway]`), and the child-process halves of §7.7/§7.27 that no fake can exercise.

- 2026-09-24 — Draft, third pass. Reverses this morning's `_parent` decision and settles how a prompt reaches a runtime. **`_parent = "codex"` now works and is the recommended shape**: an agent that inherits a reserved kind is a named instance of it, with its own name, prompt, `cwd` and permission, reachable from `[runtime.actors.*]`, `AskSubagent`, `boscli ask --agent` and `app.agent()`. The earlier reason for blocking it — keeping dispatch keyed purely on the kind's name — turned out to cost more than it saved once the `.md` authoring route was on the table, because the alternative was one prompt per runtime per workspace. Dispatch is now "the reserved name, or the `external_runtime` the resolver wrote from `_parent`", which is one pseudo-factory table merged into the existing `_resolve_agent_inheritance` call; the reserved kinds are deliberately kept out of the registration enumeration so no project gains a phantom `codex` agent. The `[agent.defaults]` skip (§3.2.1) had to widen with it, since `george` is not a reserved name. **`system_prompt` keeps its name and means append**, with `base_instructions` as the explicit replace — the previous pass renamed it to `instructions` and rejected `system_prompt`, which would have broken the file route where the markdown body lands in exactly that key and nobody writes it by hand. **No path-valued prompt key**: `resolve_agents()` reads files and everything below it sees a resolved string, because a path would have to resolve against something a multi-workspace host cannot supply unambiguously. New §3.12 states what is per-harness and what is process- or user-global for a host that manages several workspaces: everything this BEP adds is per harness (including one MCP server per harness on an ephemeral port with per-agent tokens), while `ep_tool`, `~/.claude/projects/`, `CODEX_HOME` and the environment are shared — and concurrent multi-workspace hosting is blocked today by `BosApp`'s one-per-process guard and `bootstrap`'s rewriting of `AgentRegistry` / `[exts]` defaults / `os.environ`, which is a BEP 18 / BEP 6 problem this BEP names, does not solve, and does not deepen (§7.27). Also grounded: the `.md` frontmatter parser is a small YAML subset (`_parse_simple_yaml_mapping`), so frontmatter keys must stay scalars or simple indented blocks.

- 2026-09-24 — Draft, second pass, after review. Two changes. `_parent = "codex"` / `_parent = "claude-code"` stay errors: the previous draft made registering the reserved kinds as implicit parents a Layer 1 task, and that is dropped — `_parent` means "inherit a spec", a reserved kind has no spec, and an agent dispatching on its parent's name would make §3.2's name-is-the-switch rule conditional. Multiple profiles of one runtime were the reason for wanting it, and they already work two ways that need no code: `[runtime.actors.<name>].agent_cfg` (`ActorConfig.agent_cfg`, `schema.py:123-137`, already passed to `create_agent` by `ActorManager._start_record`, `actor_manager.py:130-133`) and `build_agent(kind, agent_cfg=…)`. And §3.4.1 is new: the previous draft had one table row mapping `system_prompt` to each runtime's append slot, which was wrong twice over. On a BOS `Agent` that key *is* the whole prompt, so carrying the name over would mean one key with opposite consequences depending on the agent kind — it is now rejected in favour of `instructions` (append / `developer_instructions`) and `base_instructions` (replace). More seriously, `ClaudeAgentOptions.system_prompt` defaults to `None` and `SubprocessCLITransport._build_command` turns `None` into `--system-prompt ""` — an empty prompt, not Claude Code's own — while the bare `{"type": "preset", "preset": "claude_code"}` form matches no branch and emits no flag, which is how the CLI default is actually obtained. An implementation that simply left the option alone would have shipped Claude Code's tools with none of the prompt that drives them, with no error anywhere; §7.13 therefore asserts the emitted CLI flags rather than the resolved config. Also grounded: `AGENTS.md` is controllable after all, via the `project_doc_max_bytes` / `project_doc_fallback_filenames` config keys, so the earlier worry that Codex project docs were unsuppressable is dropped — though whether `0` fully suppresses them is left as §7.25 rather than asserted.

- 2026-09-23 — Draft. Decisions: the seam is two reserved agent-kind names with no extension point (§3.2); `AgentPort` replaces `Agent` in four signatures rather than the runtimes inheriting `Agent` (§3.3); `permission` is a BOS-owned three-value enum precisely because the two runtimes' enforcement is not equivalent (§3.5); one streamable-HTTP MCP server serves both, selected by an explicit `mcp_tools` list with no registration-time flag (§3.8); BOS persists two messages per turn and reads full transcripts from the native runtime on demand, routed by stored metadata rather than a `chat_id` convention (§3.7). Grounded findings that changed the design: Claude Code's `SandboxSettings` covers bash commands only and its own docstring directs filesystem restriction to permission rules, so `cwd` confinement for Claude is BOS-built out of `can_use_tool` (§3.5.3); `CodexClient._default_approval_handler` auto-accepts command-execution and file-change escalations and `AsyncCodex` exposes no way to replace it, so `workspace-write` would have silently meant "anything that asks is allowed" (§3.5.4); `mcp` 2.x renamed `FastMCP` to `MCPServer` while `claude-agent-sdk` pins `>=1.23,<3`, so the extras pin `mcp>=2,<3` (§3.10.4); `ep_agent` returns config dicts, not runtimes, and so cannot be the seam (§3.2); `Agent` exposes no public `chat_store`, so the host's current read path is `harness.chat_store` and stays untouched (§3.7); `boscli inspect` is the repo's only reader of agent internals and needs a branch (§3.3.1); `_resolve_agent_inheritance` does not know the reserved kinds, so `_parent = "codex"` is an explicit implementation step rather than an assumed capability (§3.4).
