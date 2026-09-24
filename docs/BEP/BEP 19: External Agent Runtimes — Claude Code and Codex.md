# BEP 19: External Agent Runtimes — Claude Code and Codex

- **Status:** **Draft** — not implemented. See §9.
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

- Delegation: `_HarnessAgentRunner.run()` ([`harness.py:255-277`](../../src/bos/core/harness.py)) calls `create_agent(kind)`. The existing `AskSubagent(role=…)` tool therefore reaches an external runtime the day the runtime is an agent kind, with no new tool and no change to [`plugins/subagent.py`](../../src/bos/plugins/subagent.py).
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

`AgentHarness.create_agent` ([`harness.py:373-427`](../../src/bos/core/harness.py)) ends in `return _apply(Agent, kwargs)` and has no substitution point. This BEP adds one:

```python
EXTERNAL_AGENT_KINDS: dict[str, type] = {"claude-code": ClaudeCodeAgent, "codex": CodexAgent}
```

An agent is backed by one of them when **either** its kind is a reserved name, **or** its resolved spec carries `external_runtime` — which is what inheriting from a reserved name produces (§3.4). The branch goes immediately after `merged_cfg` is computed and **before** `_bind_plugins_for_agent`, because none of plugins, the local `ToolRegistry`, `ResolvedToolSet`, `_CompositePluginInterceptor` or `_PluginPromptProvider` applies to a runtime that owns its own tool loop:

```python
merged_cfg = _deep_merge(copy.deepcopy(agent_defaults), agent_cfg or {})
runtime = kind if kind in EXTERNAL_AGENT_KINDS else merged_cfg.pop("external_runtime", None)
if runtime is not None:
    agent = EXTERNAL_AGENT_KINDS[runtime](
        kind=kind, cfg=merged_cfg, chat_store=self.chat_store, workspace=self._workspace,
        mcp=self._ensure_tool_mcp_server,   # §3.8; an accessor, not a server — calling it is
                                            # what starts the loopback server, and a runtime
                                            # whose mcp_tools is empty never calls it
    )
    self._owned.append(agent)           # _aclose() on harness exit — §3.1
    return agent
```

`kind` stays the agent's own name, so a Codex-backed agent called `george` reports itself as `george` through `AgentPort.name`, in events, and in `boscli inspect` — the runtime is how it is built, not who it is.

There is no extension point and no adapter abstraction. Two names, two classes, one dict. A third runtime is a third entry; if a fourth ever needs to come from outside the repo, *that* is when an extension point earns its keep.

**`ep_agent` is not the seam.** It is a factory for agent *config dicts*, not agent objects — [`extensions/agents/bos_config.py:174-195`](../../src/bos/extensions/agents/bos_config.py) returns `{"description": …, "system_prompt": …, "tools": …, "plugins": …}`, which `bootstrap_platform` folds into `AgentRegistry`. It cannot return a runtime.

#### 3.2.1 Externally-backed agents do not inherit `[agent.defaults]`

`bootstrap_platform` merges `[agent.defaults]` under every registered agent ([`workspace.py:644-646`](../../src/bos/config/workspace.py)). A project with `[agent.defaults] model = "gpt-4o"` would silently hand `"gpt-4o"` to Codex as a *native* model name, along with `max_tokens`, `plugins` and the rest.

The registration loop therefore starts from `{}` instead of `agent_defaults` when the name is reserved **or** the inheritance-resolved spec carries `external_runtime` — the second half is what keeps `george` covered, since `george` is not a reserved name. `config_specs` is already inheritance-resolved at that point in the loop, so the check is available where it is needed. Each runtime then validates its own config strictly (§3.4).

### 3.3 `AgentPort` — widening four signatures

`Agent` is a concrete class, and four signatures name it:

| Site | Today |
|---|---|
| [`harness.py:373`](../../src/bos/core/harness.py) | `create_agent(...) -> Agent` |
| [`sdk/_app.py:123`](../../src/bos/sdk/_app.py) | `BosApp.agent(kind) -> Agent` |
| [`sdk/_app.py:140`](../../src/bos/sdk/_app.py) | `BosApp.build_agent(kind) -> Agent` |
| [`gateway/actors/agent_actor.py:105`](../../src/bos/gateway/actors/agent_actor.py) | `AgentActor(agent: Agent, ...)` |

A repo-wide scan of what those consumers actually *call* returns four members, and no more:

| Member | Call sites |
|---|---|
| `ask(chat_id, content, interrupt=, ctx_metadata=, llm_args=, event_sink=, turn_id=, commit_observer=)` | [`agent_actor.py:358`](../../src/bos/gateway/actors/agent_actor.py) |
| `run(chat_id, content, *, …, schema=, max_schema_retries=)` | [`harness.py:274`](../../src/bos/core/harness.py), [`cli/commands/agent.py:442`](../../src/bos/cli/commands/agent.py) |
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

[`cli/commands/inspect.py:184-204`](../../src/bos/cli/commands/inspect.py) reaches past the public surface into `agent._prompt_provider._plugins`, `agent._kind`, `agent._name`, `agent._model` and `agent._tools`. Against an external runtime those raise `AttributeError`. `_agent_info` gains a branch: for an object that is not an `Agent`, report `kind`, `name`, the resolved runtime, the resolved `cwd`, the `permission` level, and the resolved `mcp_tools`, with `plugins` and `skills` empty. This is the operator's inspection path (§4.3) and is the only place in the repo that reads an agent's internals.

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

The mechanism is the existing resolver plus one table. `_resolve_agent_inheritance(specs, factory_specs)` already accepts any parent present in `specs` or `factory_specs` and deep-merges the parent's resolved spec underneath the child ([`workspace.py:428-472`](../../src/bos/config/workspace.py)), so the two reserved kinds are supplied as pseudo-factory specs at that one call site:

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

**1. A file, per named agent — the authoring route.** `resolve_agents()` scans `[platform.agent_dirs]` (default `./agents`, resolved against that workspace's `bos_dir`) for `.toml` and `.md`. In a `.md` file the YAML frontmatter is the agent config and **the body is `system_prompt`**; the agent's name is the filename stem ([`workspace.py:173-193`, `:518-553`, `:939-947`](../../src/bos/config/workspace.py)). Combined with `_parent` (§3.4), one file is one named instance of a runtime — `agents/george.md`, `agents/martha.md` — and the prompt is just the body, where a prompt belongs.

This needs **no change to the loader**: the body lands in `system_prompt`, which is the key §3.4.1 defines. It is also why that key could not be renamed or rejected — nobody writes it by hand on this route.

One frontmatter constraint, which the existing parser imposes rather than this BEP: `_parse_simple_yaml_mapping` is a small subset of YAML, so keys must be plain scalars or simple indented blocks ([`workspace.py:218-251`](../../src/bos/config/workspace.py)). The list form shown in §3.4 for `mcp_tools` is within it; anything more structured belongs in a `.toml` agent file or in `[agents.*]`.

**2. `agent_cfg`, programmatically — the embedding route.** A host that keeps prompts in its own store (per tenant, in a database, templated per request) passes the resolved string:

```python
coder = await app.build_agent("george", agent_cfg={"system_prompt": prompts.for_tenant(t)})
```

`agent_cfg` is the override layer, so it wins over the `.md` file, `[agents.george]` and the inherited `[agents.codex]` alike. The TOML equivalent for a per-actor profile is a section — which, unlike an inline table, can hold a multi-line string:

```toml
[runtime.actors.coder.agent_cfg]
permission = "workspace-write"
system_prompt = """
You are the implementer for this service. …
"""
```

Precedence, lowest to highest: the reserved kind's `{"external_runtime": …}` → `[agents.<reserved>]` if written → the named agent's own spec (`.md` frontmatter and body, or `[agents.george]`) → `[runtime.actors.*].agent_cfg` → the `agent_cfg` passed to `build_agent` / `create_agent`. This is BEP 6 merge order with the §3.4 parent link slotted in; nothing new.

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
| `workspace-write` | `Sandbox.workspace_write` + `ApprovalMode.auto_review` + §3.5.4 handler | `permission_mode="acceptEdits"`, `can_use_tool` path check, `sandbox={"enabled": True}` |
| `full-access` | `Sandbox.full_access` | `permission_mode="bypassPermissions"`, no path check |

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

`CodexAgent` therefore installs its own handler on the constructed client and records why:

```python
# BEP 19 §3.5.4. The SDK's default handler auto-accepts every escalation and
# AsyncCodex exposes no way to replace it, so we reach the sync client that owns
# the transport. test_codex_approval_handler_attribute_exists fails loudly if a
# future openai-codex renames this, rather than silently restoring auto-accept.
codex._client._sync._approval_handler = self._approve
```

`_approve` denies anything the configured `permission` level does not already allow, and never blocks. The pinned test is what makes the private reach-in acceptable: an SDK upgrade that moves the attribute breaks CI instead of quietly re-opening the hole.

#### 3.5.5 Verification is behavioural

Per-runtime acceptance (§7) asserts observed effects — a write outside the root fails, a read outside the root fails — not that a mode name was passed. Mode names are evidence of intent, not of enforcement.

### 3.6 Session continuity

`ask(chat_id, …)` must resume the same native conversation on the next turn.

| | Resume | Id returned by |
|---|---|---|
| Claude Code | `ClaudeAgentOptions.resume = <session_id>` | `ResultMessage.session_id` |
| Codex | `AsyncCodex.thread_resume(thread_id)` | `AsyncThread.id` |

The mapping is stored in the **metadata of the assistant message BOS commits each turn** (§3.7): `{"external_runtime": "codex", "native_session_id": "…"}`. It is rewritten every turn, so the newest committed message always carries the freshest id; recovery after a restart is `chat_store.get_messages(chat_id)` scanned from the end, with an in-process cache in front. No new store, no `ChatStore` schema change, no separate mapping file.

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
- `source="native"` — Claude Code: `get_session_messages(session_id, directory=…)`, which parses the JSONL under `~/.claude/projects/` and chains it by `parentUuid`. Codex: `thread.read(include_turns=True)`. Both are projected into BOS `Message` objects (`role`, `content`, `tool_calls`, plus `metadata["source"] = "<runtime>"`) so a host's rendering code does not fork.
- `source="auto"` — read the BOS record, and if its metadata names an external runtime, return the native transcript; otherwise return the BOS record.

**Routing is by stored metadata, not by a `chat_id` naming convention.** The `external_runtime` key is already required for §3.6, so the lookup costs nothing extra, it works for chat ids a host created before this BEP, and it avoids a second id convention colliding with the existing one (`INTERNAL_CHAT_SEPARATOR = "~"`, shape `{parent}~{tag}~{uuid}`, [`_chat_store_utils.py:12-38`](../../src/bos/core/_chat_store_utils.py)).

`ChatStore` is **not** modified and not wrapped. A host reading `app.harness.chat_store.get_messages(chat_id)` directly — which is what the first host does today, and the only public path, since `Agent` exposes no `chat_store` property — keeps getting exactly what it gets now: the BOS record. `app.get_messages` is additive.

**What `source="native"` does not promise.** It is a live read of data BOS does not own. Each runtime compacts and prunes on its own schedule; a Codex thread can be archived or deleted; Claude Code's transcripts live under the invoking user's `~/.claude/projects/` and move with that home directory. So a native read can return fewer messages than it did before, or fail. `source="bos"` is the only read BOS stands behind. The known upgrade path for Claude Code is `ClaudeAgentOptions.session_store`, an official port whose contract is *"every transcript line written locally is also passed to `session_store.append()`, and `resume` can materialize from the store when the local file is absent"* — implementing it over `.bos/` would make transcripts workspace-local and portable. Not in this BEP. Codex exposes no equivalent port.

### 3.8 The MCP egress

A host application's own API is registered as BOS tools via `ep_tool`. A BOS agent calls them directly. An external runtime cannot: it has its own tool loop in another process. MCP is the only wire either vendor offers, and both accept **streamable HTTP**:

- Claude Code: `McpServerConfig` is `McpStdioServerConfig | McpSSEServerConfig | McpHttpServerConfig | McpSdkServerConfig`; the HTTP form is `{"type": "http", "url": …, "headers": {…}}`.
- Codex: `[mcp_servers.<name>]` supports STDIO and Streamable HTTP with `url`, plus `bearer_token_env_var` / `http_headers` / `env_http_headers`. `AsyncCodex.thread_start(config=…)` injects config overrides per thread.

So there is **one server, not one per vendor**:

- `BosToolMcpServer` builds an `mcp.server.mcpserver.MCPServer`, registers one MCP tool per selected `ep_tool` entry (schema from `ToolRegistry.build_openai_schema`, description from the extension, execution straight through `ep_tool.invoke(name, kwargs)`), and serves `MCPServer.streamable_http_app(stateless_http=True, host="127.0.0.1")` under an in-process `uvicorn.Server` on an ephemeral loopback port.
- Each external agent gets its own bearer token, checked by one ASGI middleware, which also selects that agent's allowed tool subset. A token is never written to config or to a transcript; Codex receives it through `http_headers`, Claude Code through `headers`.
- Claude Code's in-process `create_sdk_mcp_server()` is deliberately **not** used: it would be cheaper for Claude and unusable for Codex, producing two tool-serving code paths. A Codex stdio shim is also rejected — a shim process still needs IPC back into BOS, and HTTP *is* that IPC.

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
| `interrupt` callback | `ClaudeSDKClient.interrupt()` | `AsyncTurnHandle.interrupt()` |
| `request_stop()` | same, raced against the native turn | same |
| `event_sink` | `receive_response()`: `ToolUseBlock`→`tool`/`start`, `ToolResultBlock`→`tool`/`finish`, `TextBlock`→`response`, `ResultMessage`→`turn`/`finish` | `AsyncTurnHandle.stream()`: `item/started`·`item/completed`→`tool`, `turn/completed`→`turn`/`finish` |
| `turn_id`, `ctx_metadata`, `commit_observer` | BOS-side, unchanged | same |
| cfg `system_prompt` — appends (§3.4.1) | `system_prompt={"type": "preset", "preset": "claude_code", "append": …}` | `thread_start(developer_instructions=…)` |
| cfg `base_instructions` — replaces (§3.4.1) | `system_prompt=<str>`, the plain-str form | `thread_start(base_instructions=…)` |
| *neither set* | `system_prompt={"type": "preset", "preset": "claude_code"}` — **never left at `None`**, §3.4.1.3 | both omitted; runtime defaults stand |
| cfg `cwd` | `options.cwd` | `thread_start(cwd=…)` |
| cfg `max_iterations` | `options.max_turns` | **no counterpart — dropped** |

`AgentResult` is populated from `ResultMessage` (`usage`, `total_cost_usd`, `num_turns`, `terminal_reason`) or `TurnResult` (`usage`, `status`, `error`, `duration_ms`). `finish_reason` carries the native terminal reason verbatim.

**Dropped on purpose**, with one `logger.debug` line at construction listing whichever were set: BOS `tools` / `exclude_tools` (the runtime has its own; the host's reach it via §3.8), BOS plugins, `consolidator` and all compaction, `interceptor`, `max_tokens`, `tool_noise_filter`, `history_attribution`. None of these has a counterpart, and forwarding them would be a lie about what the runtime honours.

### 3.10 Lifecycle, concurrency, timeouts, auth

#### 3.10.1 Clients and concurrency

Codex: one `AsyncCodex` per `CodexAgent`, started lazily, owning one `codex app-server` child; threads are per `chat_id`. Claude Code: one `ClaudeSDKClient` per turn with `resume=`, disconnected in `finally`.

```python
# ponytail: a client per turn costs one CLI spawn (~1s). A per-chat_id session pool
# is the upgrade if that latency shows up; resume= makes the stateless version correct.
```

Two concurrent turns on one `chat_id` are rejected with a busy error rather than queued — the native session is single-threaded and BOS does not hide that.

#### 3.10.2 Timeouts and shutdown

`timeout_seconds` wraps the turn in `asyncio.timeout`; on expiry the native turn is interrupted, then the error is raised. `aclose()` interrupts any in-flight turn, closes the client, and reaps the child. Cancellation never leaves a child writing to the workspace after the turn has been given up on.

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

**The pre-existing blocker, named rather than solved.** A host cannot hold two workspaces open *concurrently* today, and BEP 19 does not change that. `BosApp.__aenter__` refuses a second live instance in one process, and its own comment gives the reason: `bootstrap_platform()` writes `os.environ` and rebuilds the agent registry, so two would overwrite each other ([`sdk/_app.py:20-27`, `:60-66`](../../src/bos/sdk/_app.py)). `open_harness` has the same hazard, because it calls the same `bootstrap` ([`sdk/_bootstrap.py:14-31`](../../src/bos/sdk/_bootstrap.py)), and the shared state is `AgentRegistry`'s class-level dict, the `[exts]` defaults merged into process-global `ExtensionPoint` objects, and `[platform.envs]`. Sequential use — open, use, close, open the next — works today and is what §4.1 shows.

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

A host holding several workspaces opens them **one at a time** (§3.12) and picks the prompt route that matches where its prompts live — the workspace's own `agents/george.md` when the prompt belongs to the project, or `agent_cfg` when the host keeps prompts per tenant:

```python
for ws in tenant_workspaces:                       # sequential: BosApp is one-per-process
    async with BosApp(ws) as app:
        coder = await app.build_agent("george", agent_cfg={
            "system_prompt": prompts.for_tenant(ws.name),   # a resolved string, never a path
        })
        await coder.ask(chat_id_for(ws), task)
```

### 4.2 End user

Through a BOS agent, unchanged: the main agent calls `AskSubagent(role="coder", task=…)` and the external runtime executes, because `AgentRunner` resolves a role to an agent kind (§1.2). Directly: `boscli ask --agent codex "…"`.

### 4.3 Operator

- `boscli inspect --agent codex` reports the resolved runtime, the absolute `cwd`, the `permission` level, the resolved `mcp_tools` with any unmatched names, and whether the extra is installed and the native login present (§3.3.1).
- A missing extra, an absent login, an escaping `cwd`, an unknown config key, and `workspace-write` on Claude Code where the bash sandbox is unavailable all fail at `create_agent` with a message naming the fix.
- Unmatched `mcp_tools` names appear as warnings, once, at build.
- Native transcripts are where the runtime puts them; `app.get_messages(chat_id, source="bos")` is the record BOS guarantees (§3.7).

### 4.4 Background / automated

An external agent bound to an actor runs under the gateway like any other: turn admission, interrupt, event fan-out and shutdown drain all work through `AgentPort`. Shutdown calls `request_stop()`, which interrupts the native turn; `aclose()` reaps the child. Nothing waits on an approval (§3.5.4), so an unattended run cannot hang on one.

---

## 5. Compatibility and fallout

### 5.1 Breaking: four signatures widen from `Agent` to `AgentPort`

`create_agent`, `BosApp.agent`, `BosApp.build_agent`, `AgentActor.__init__`. At runtime nothing changes — `Agent` satisfies the protocol. For an embedder type-checking against `bos.sdk`, a call to an `Agent` member outside `AgentPort` on the *result* of these now fails. No such member is called anywhere in this repo (§3.3), and `bos.sdk.__all__` grows `AgentPort`. BEP 18 §3.8's contract list and its drift test both need the new name, and `test_promised_ports_are_implementable_from_the_contract_alone` must pass for `AgentPort`.

### 5.2 Breaking: `[agent.defaults]` no longer reaches the two reserved kinds

Only observable in a project that both sets `[agent.defaults]` and uses a reserved kind — impossible before this BEP, since the kinds do not exist. Recorded because the registration loop's uniformity is what changes (§3.2.1).

### 5.3 Not breaking

`Agent` is unmodified. `ChatStore` is unmodified (§3.7). `ep_tool`, `ep_agent`, `SubagentPlugin` and the plugin lifecycle are unmodified. `boscli inspect` gains a branch; its output for a BOS agent is unchanged. `BosApp.build_agent`'s new parameter is optional. A base install with no extras imports `bos.sdk` and builds BOS agents exactly as before.

### 5.4 Third-party impact

Two new optional dependencies, each pinned exactly, each carrying a vendor CLI binary. Both vendors ship these SDKs under their own release cadence and Codex's app-server protocol is marked experimental upstream — hence exact pins and a §7 criterion that the pinned versions are the ones tested. An extension that subclasses `Agent` is unaffected. An extension that type-annotates against `create_agent`'s return type must widen with it.

---

## 6. Implementation plan (dependency-ordered)

**Layer 1 — the seam, no SDKs.**
1. `AgentPort` in `bos/core/agent/contract.py`; export from `bos/core` and `bos.sdk.__all__`; widen the four signatures (§3.3, §5.1). Update BEP 18 §3.8's list. Tests: `test_the_contract_surface_is_importable_and_identical`'s hardcoded `expected` set gains `"AgentPort"` (34 names → 35), and `test_promised_ports_are_implementable_from_the_contract_alone` passes for it; existing suites unchanged.
2. `EXTERNAL_AGENT_KINDS` and the `create_agent` branch (§3.2), against a `FakeExternalAgent` registered only in tests. Register the agent in `_owned`. Then the `_parent` half: `_EXTERNAL_RUNTIME_SPECS` merged into the `_resolve_agent_inheritance` call only (§3.4), and the `[agent.defaults]` skip keyed on the reserved name **or** a resolved `external_runtime` (§3.2.1). Tests: `george` with `_parent: codex` dispatches to the runtime while `AgentPort.name` stays `george`; `[agents.codex]` terms reach `george`; a hand-written `external_runtime` is rejected; no phantom `codex` in `AgentRegistry.describe()` when nothing names it.
3. `boscli inspect`'s non-`Agent` branch (§3.3.1).
4. `BosApp.build_agent(kind, agent_cfg=None)` and `BosApp.get_messages(...)` with `source="bos"` only (§3.7).

**Layer 2 — shared runtime scaffolding, still no SDKs.**
5. Config parsing and strict validation shared by both runtimes: `cwd` resolution and containment, `permission`, `system_prompt` / `base_instructions` (mutually exclusive), `external_runtime` rejected when hand-written, `timeout_seconds`, `auth`, `mcp_tools`, `native_options`, unknown-key rejection (§3.4, §3.4.1, §3.5.1).
6. Session mapping: write on commit, read back from `chat_store`, in-process cache (§3.6). Tested against the fake.
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
9. `boscli inspect --agent <external>` succeeds and reports runtime, absolute `cwd`, `permission`, and resolved `mcp_tools`.
10. `import bos.sdk` on a base install with no extras succeeds; `create_agent("codex")` there raises an error naming `bos-ai[codex]`.
11. An `agents/george.md` whose frontmatter is `_parent: codex` and whose body is a prompt builds a Codex-backed agent whose `name` is `george` and whose `system_prompt` is the file body. `[agents.codex] cwd = "x"` reaches `george` unless `george` overrides it. A workspace naming neither reserved kind has no `codex` entry in `AgentRegistry.describe()`, and `resolve_default_agent()` is unaffected.
12. A hand-written `external_runtime` key raises, and the message names `_parent` (§3.4). Setting both `system_prompt` and `base_instructions` raises (§3.4.1).
13. With `system_prompt` set, the Claude Code command carries `--append-system-prompt` and **not** `--system-prompt`; with neither prompt key set, it carries **neither** flag; with `base_instructions` set, it carries `--system-prompt <text>`. Asserted on the built command, not on the resolved config, because the defaulting trap in §3.4.1.3 is invisible at the config layer.
14. `uv run pytest -q`, `uv run ruff check src tests examples`, and `npx -y pyright src` are green, pyright at zero errors.

**Requires a real subscription login (Layer 4–5, per runtime, recorded in §8.1):**

15. A turn starts, streams `TurnEvent`s that a host renders, and returns an `AgentResult` with non-empty `usage`.
16. A second `ask()` on the same `chat_id` continues the same native session — asserted by the native session/thread id, not by the model's reply.
17. After a BOS process restart, a third `ask()` on that `chat_id` resumes it, recovered from `ChatStore` metadata alone.
18. Under `permission = "workspace-write"`, a write inside `cwd` succeeds and a write outside it **fails** — observed, per §3.5.5. Under `read-only`, every write fails.
19. `request_stop()` mid-turn ends the native turn and reaps the child; nothing writes to the workspace afterwards.
20. `timeout_seconds` expiry interrupts the native turn and raises.
21. `schema=` returns validated structured output through each runtime's native mechanism.
22. With `mcp_tools` set, the runtime lists and successfully calls the exposed BOS tool, and an unexposed `ep_tool` is not callable.
23. `app.get_messages(chat_id)` returns the native transcript including tool activity, while `source="bos"` returns the two-message-per-turn record.
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
| Layers 1–3 (seam, scaffolding, MCP egress) | Implementable now. Every dependency verified in-repo. |
| Layer 4 Codex | Implementable now; §7.15–26 **unverified** until run against a real ChatGPT login. |
| Layer 4 Claude Code | Same, plus §3.5.3's confinement is BOS-built and must be validated behaviourally before `workspace-write` is documented as safe. |
| Concurrent multi-workspace hosting | **Blocked, and not by this BEP.** `BosApp` refuses a second live instance per process and `bootstrap` rewrites process-global state (§3.12). Sequential use works. Everything BEP 19 adds is already per harness, so lifting the blocker elsewhere does not require revisiting this BEP. |
| `workspace-write` on Windows | **Blocked.** Claude Code's bash sandbox is macOS/Linux only; the runtime refuses the combination until there is an enforcement story. |

### 8.2 Unresolved

- **Whether `full-access` should exist at all.** Kept because a trusted local dev loop is a real use case, but it is the one level where BOS enforces nothing. Candidate for requiring an explicit second opt-in.
- **Where process-global BOS state should live so a host can hold several workspaces at once.** `AgentRegistry`'s class dict, the `[exts]` defaults merged into `ExtensionPoint` objects, and `[platform.envs]` writing `os.environ` are the three (§3.12). A BEP 18 / BEP 6 question, listed here because the first host wants it and because BEP 19's §7.27 is the guard that this BEP does not add a fourth.
- **Claude Code `session_store`.** Would make transcripts workspace-local and portable (§3.7). Deferred; the argument for doing it is that `~/.claude/projects/` is the wrong home for a server's data.
- **Codex config isolation.** Codex reads `~/.codex/config.toml`, so the operator's machine config reaches the runtime the way `setting_sources` prevents for Claude Code. `CODEX_HOME` would relocate it, but `auth.json` lives there too, so relocating breaks subscription login. Unresolved; the asymmetry stands and is documented.

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

- 2026-09-24 — Draft, third pass. Reverses this morning's `_parent` decision and settles how a prompt reaches a runtime. **`_parent = "codex"` now works and is the recommended shape**: an agent that inherits a reserved kind is a named instance of it, with its own name, prompt, `cwd` and permission, reachable from `[runtime.actors.*]`, `AskSubagent`, `boscli ask --agent` and `app.agent()`. The earlier reason for blocking it — keeping dispatch keyed purely on the kind's name — turned out to cost more than it saved once the `.md` authoring route was on the table, because the alternative was one prompt per runtime per workspace. Dispatch is now "the reserved name, or the `external_runtime` the resolver wrote from `_parent`", which is one pseudo-factory table merged into the existing `_resolve_agent_inheritance` call; the reserved kinds are deliberately kept out of the registration enumeration so no project gains a phantom `codex` agent. The `[agent.defaults]` skip (§3.2.1) had to widen with it, since `george` is not a reserved name. **`system_prompt` keeps its name and means append**, with `base_instructions` as the explicit replace — the previous pass renamed it to `instructions` and rejected `system_prompt`, which would have broken the file route where the markdown body lands in exactly that key and nobody writes it by hand. **No path-valued prompt key**: `resolve_agents()` reads files and everything below it sees a resolved string, because a path would have to resolve against something a multi-workspace host cannot supply unambiguously. New §3.12 states what is per-harness and what is process- or user-global for a host that manages several workspaces: everything this BEP adds is per harness (including one MCP server per harness on an ephemeral port with per-agent tokens), while `ep_tool`, `~/.claude/projects/`, `CODEX_HOME` and the environment are shared — and concurrent multi-workspace hosting is blocked today by `BosApp`'s one-per-process guard and `bootstrap`'s rewriting of `AgentRegistry` / `[exts]` defaults / `os.environ`, which is a BEP 18 / BEP 6 problem this BEP names, does not solve, and does not deepen (§7.27). Also grounded: the `.md` frontmatter parser is a small YAML subset (`_parse_simple_yaml_mapping`), so frontmatter keys must stay scalars or simple indented blocks.

- 2026-09-24 — Draft, second pass, after review. Two changes. `_parent = "codex"` / `_parent = "claude-code"` stay errors: the previous draft made registering the reserved kinds as implicit parents a Layer 1 task, and that is dropped — `_parent` means "inherit a spec", a reserved kind has no spec, and an agent dispatching on its parent's name would make §3.2's name-is-the-switch rule conditional. Multiple profiles of one runtime were the reason for wanting it, and they already work two ways that need no code: `[runtime.actors.<name>].agent_cfg` (`ActorConfig.agent_cfg`, `schema.py:123-137`, already passed to `create_agent` by `ActorManager._start_record`, `actor_manager.py:130-133`) and `build_agent(kind, agent_cfg=…)`. And §3.4.1 is new: the previous draft had one table row mapping `system_prompt` to each runtime's append slot, which was wrong twice over. On a BOS `Agent` that key *is* the whole prompt, so carrying the name over would mean one key with opposite consequences depending on the agent kind — it is now rejected in favour of `instructions` (append / `developer_instructions`) and `base_instructions` (replace). More seriously, `ClaudeAgentOptions.system_prompt` defaults to `None` and `SubprocessCLITransport._build_command` turns `None` into `--system-prompt ""` — an empty prompt, not Claude Code's own — while the bare `{"type": "preset", "preset": "claude_code"}` form matches no branch and emits no flag, which is how the CLI default is actually obtained. An implementation that simply left the option alone would have shipped Claude Code's tools with none of the prompt that drives them, with no error anywhere; §7.13 therefore asserts the emitted CLI flags rather than the resolved config. Also grounded: `AGENTS.md` is controllable after all, via the `project_doc_max_bytes` / `project_doc_fallback_filenames` config keys, so the earlier worry that Codex project docs were unsuppressable is dropped — though whether `0` fully suppresses them is left as §7.25 rather than asserted.

- 2026-09-23 — Draft. Decisions: the seam is two reserved agent-kind names with no extension point (§3.2); `AgentPort` replaces `Agent` in four signatures rather than the runtimes inheriting `Agent` (§3.3); `permission` is a BOS-owned three-value enum precisely because the two runtimes' enforcement is not equivalent (§3.5); one streamable-HTTP MCP server serves both, selected by an explicit `mcp_tools` list with no registration-time flag (§3.8); BOS persists two messages per turn and reads full transcripts from the native runtime on demand, routed by stored metadata rather than a `chat_id` convention (§3.7). Grounded findings that changed the design: Claude Code's `SandboxSettings` covers bash commands only and its own docstring directs filesystem restriction to permission rules, so `cwd` confinement for Claude is BOS-built out of `can_use_tool` (§3.5.3); `CodexClient._default_approval_handler` auto-accepts command-execution and file-change escalations and `AsyncCodex` exposes no way to replace it, so `workspace-write` would have silently meant "anything that asks is allowed" (§3.5.4); `mcp` 2.x renamed `FastMCP` to `MCPServer` while `claude-agent-sdk` pins `>=1.23,<3`, so the extras pin `mcp>=2,<3` (§3.10.4); `ep_agent` returns config dicts, not runtimes, and so cannot be the seam (§3.2); `Agent` exposes no public `chat_store`, so the host's current read path is `harness.chat_store` and stays untouched (§3.7); `boscli inspect` is the repo's only reader of agent internals and needs a branch (§3.3.1); `_resolve_agent_inheritance` does not know the reserved kinds, so `_parent = "codex"` is an explicit implementation step rather than an assumed capability (§3.4).
