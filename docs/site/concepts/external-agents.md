# External agents: Claude Code & Codex

An **external agent** is a BOS agent whose turn loop is not BOS's. BOS hands the turn
to a vendor's own agent harness — **Claude Code**, through the official
`claude-agent-sdk` and the `claude` CLI it bundles, or **Codex**, through the official
`openai-codex` SDK and its `codex app-server` binary — and that harness brings its own
tools, context management, permission system and login.

To the rest of BOS it is still an agent. It is exposed through the same `AgentPort`
(`name`, `ask`, `run`, `request_stop`) as BOS's own agent, so it works everywhere an
agent kind works: `boscli ask --agent`, an actor in the gateway, a target for
`AskSubagent`, and `app.agent()` in [`bos.sdk`](../embedding/index.md).

This page is the reference. For a hands-on walkthrough, see
[Tutorial 7](../tutorials/external-agents.md); for the design reasoning, see
[BEP 19](https://github.com/bos-agent/bos-ai/blob/main/docs/BEP/BEP%2019%3A%20External%20Agent%20Runtimes%20%E2%80%94%20Claude%20Code%20and%20Codex.md).

---

## When to use one

Use an external agent when you want Claude Code's or Codex's own harness — its file
and shell tools, its context handling — and its subscription login, while BOS supplies
the name, the actors and channels, the persisted conversation record, delegation and
access to your application's own tools over MCP.

Use a BOS agent when you want what only BOS's loop has. None of these apply to an
external agent: BOS plugins (memory, skills, planning, sub-agents), BOS `tools`,
turn interceptors, compaction and the consolidator, `[agent.defaults]`, and any
LiteLLM `provider/model` string.

| | Claude Code | Codex |
|---|---|---|
| Vendor SDK (pinned) | `claude-agent-sdk` 0.2.159, bundling CLI 2.1.281 | `openai-codex` 0.156.1, with `codex-cli` 0.156.1 |
| Process | one `claude` CLI child **per turn**, closed when the turn ends | one `codex app-server` child **per agent**, started on its first turn, closed when the harness closes |
| Filesystem confinement | built by BOS: a tool allowlist, a path-checking hook for file tools, the CLI's OS sandbox for bash | Codex's own OS sandbox |
| Login check | none — the environment is checked for other credentials when the agent is built | the account is checked on the first turn |
| Project docs | BOS appends the root `CLAUDE.md` | Codex reads `AGENTS.md` itself |

!!! note "Status"
    The Codex runtime has been validated against a real ChatGPT subscription. The
    Claude Code runtime's behaviour is pinned in CI against the real bundled CLI with a
    scripted model; its live-login validation is tracked in BEP 19 §8.1.

---

## Install

Each runtime is its own extra. Neither is in `bos-ai[all]`, because each carries a
vendor binary (the Claude Code wheel is about 230 MB installed):

```bash
pip install 'bos-ai[claude-code]'
pip install 'bos-ai[codex]'
```

Both extras pull in `mcp` 2.x and `bos-ai[gateway]`, which BOS needs to expose your
tools to the runtime. With the CLI, the extra must be installed into the same
environment as `boscli` — for example `uv tool install boscli --with 'bos-ai[codex]'`.
Without it, building the agent fails with *"The 'codex' agent runtime needs its
optional dependency. Install it with: pip install 'bos-ai[codex]'"*.

Then log in, once, as the OS user BOS runs as — BOS uses the runtime's own login and
stores no token:

- **Claude Code** — the bundled CLI uses that user's Claude Code login (run `claude`,
  then `/login`). For a headless host, `claude setup-token` makes a subscription token
  to export as `CLAUDE_CODE_OAUTH_TOKEN`.
- **Codex** — `codex login`.

To bill an API key instead, set `auth = "api_key"` ([Auth](#auth)).

---

## Declaring one

`claude-code` and `codex` are **reserved agent kinds**. An agent that inherits from one
with `_parent` is a named instance of that runtime, with its own name, prompt, `cwd` and
permission — the recommended shape:

```toml
[agents.reviewer]
_parent = "claude-code"
permission = "read-only"
cwd = "services/api"
system_prompt = "You review this service's code and report problems. You never change files."
```

The same in an agent file, where the Markdown body is `system_prompt` —
`.bos/agents/implementer.md`:

```markdown
---
_parent: codex
permission: workspace-write
cwd: services/api
timeout_seconds: 1200
mcp_tools:
  - CreateTicket
---
You are the implementer for the payments service. Your reply is read by the team's
planner agent, not by a person: end with DONE or BLOCKED and one line of reason.
```

`reviewer` and `implementer` report under their own names — in events, in
`boscli inspect agent`, in `AgentPort.name`. The runtime is how they are built, not who
they are. Check what an agent resolved to without running a turn:

```bash
boscli inspect agent implementer   # runtime, absolute cwd, permission, mcp_tools, unmatched names
```

**The bare kinds.** `[agents.codex]` (or `[agents.claude-code]`) defines an agent named
after the runtime, and its settings are inherited by every agent whose `_parent` names
that runtime — so shared `cwd`, `permission` or `model` can be set once. Without such a
table the reserved names are not registered agents: `boscli ask --agent codex` and
`app.agent("codex")` answer *"Unknown agent 'codex'"*, and the programmatic route below
is how to build one. A named external agent can itself be a `_parent`, for a variant
that overrides only what differs.

**Programmatically.** `agent_cfg` takes the same keys:

```python
coder = await app.build_agent("codex", agent_cfg={"permission": "read-only", "cwd": "services/api"})
second = await app.build_agent("martha", agent_cfg={"_parent": "codex", "permission": "read-only"})
agent = await harness.create_agent(kind="claude-code", agent_cfg={"permission": "read-only"})
```

**`[agent.defaults]` is not inherited.** An externally-backed agent starts from nothing:
a project-wide LiteLLM `model`, `max_tokens` or plugin list would be meaningless, or
wrong, to a runtime that reads `model` as a native model name. From lowest to highest
precedence, an external agent's config is: the reserved kind → `[agents.<runtime>]` if
written → the agent's own spec (`[agents.<name>]` or its agent file) →
`[runtime.actors.<name>].agent_cfg` → the `agent_cfg` passed to `build_agent` /
`create_agent`.

**Actors.** Bind one like any agent — `[runtime.actors.coder] agent = "implementer"` —
and override per actor in `agent_cfg` (e.g. `permission = "read-only"`). `_parent` is
refused in an actor's `agent_cfg`: declare the variant in `[agents.*]` and select it.

**When it is built.** An agent your config names is built when `BosApp` opens, when an
actor starts, or when `boscli ask` runs. Every construction-time refusal on this page
surfaces there, before any turn and before any vendor process starts.

---

## Configuration reference

Each runtime validates its own config strictly: an unknown key fails at build time,
naming the key and listing the known ones.

| Key | Runtime | Required / default | Meaning |
|---|---|---|---|
| `permission` | both | **required** — no default | `read-only`, `workspace-write` or `full-access`. What the runtime may do to the filesystem; see [Permission](#permission). |
| `cwd` | both | `"."` | The runtime's working directory and confinement root: a string path, relative to the harness's workspace root (in a project, the directory holding `.bos/`), resolved with symlinks, and required to stay inside it. |
| `system_prompt` | both | — | The agent's instructions, **appended** to the runtime's own prompt, so its tool guidance survives. Claude Code: appended to its `claude_code` system prompt. Codex: `developer_instructions`. In an agent file, the body. |
| `base_instructions` | both | — | **Replaces** the runtime's own prompt, and with it the harness's tool guidance. Setting both prompt keys is an error. |
| `model` | both | the runtime's default | A native model name, passed through verbatim — not a LiteLLM `provider/model`. A turn's `llm_args["model"]` (e.g. `boscli ask --model`) overrides it for that turn. |
| `auth` | both | `"subscription"` | `"subscription"` or `"api_key"`; see [Auth](#auth). |
| `timeout_seconds` | both | none — no deadline | A number bounding each turn attempt; see [Stop, abort, timeout](#stop-abort-timeout). |
| `mcp_tools` | both | `[]` | A list of `@ep_tool` names to expose to the runtime over MCP; see [Exposing BOS tools](#exposing-bos-tools). `"*"` and a bare string are refused. |
| `native_options` | both | `{}` | A table of vendor settings BOS does not decide itself (below). |
| `setting_sources` | Claude Code | `[]` | Which Claude Code settings files load, from `"user"`, `"project"`, `"local"`. `"project"` or `"local"` loads the repository's own `.claude/settings.json` / `.claude/settings.local.json`: construction logs a WARNING, because the hooks, `apiKeyHelper` and other commands named there run on the host outside the sandbox and outside `permission`. A Codex config naming this key fails as unknown. |
| `max_iterations` | Claude Code | none — no limit | A positive integer, sent as the CLI's `max_turns`. A turn that spends it closes with `(max iterations reached)` and is committed. Codex has no counterpart and drops it. |

**`native_options`, per runtime.**

- **Claude Code** allows exactly `fallback_model`, `max_budget_usd`, `betas`, `thinking`,
  `max_thinking_tokens` and `task_budget` — each a field of the SDK's
  `ClaudeAgentOptions`, forwarded verbatim. Anything else, a misspelt key included, is
  refused when the agent is built, each key with its reason: a setting BOS derives from
  your config (`permission_mode`, `sandbox`, `setting_sources`, `tools`, `resume`, …), or
  one unsafe to forward (`cli_path`, `add_dirs`, `plugins`, `agents`, `user`, …).
- **Codex** passes each entry to `thread_start` / `thread_resume` as a keyword, except a
  `config` sub-table, which is merged into the config override Codex layers over the
  operator's `~/.codex/config.toml`. Refused when the agent is built: `sandbox`,
  `approval_mode`, `cwd`, `model`, `developer_instructions`, `base_instructions`, and
  `config.mcp_servers` / `config.sandbox_workspace_write`, whether written as a nested
  table or as a dotted key. A keyword Codex does not have is not caught at build time; it
  fails the first turn with the vendor's `TypeError`.

`native_options` is trusted config, written by whoever writes `permission`. It cannot
reach what `permission` decides through the settings above, but for Codex the refusal
list was found by probing and is not proven complete.

**Dropped on purpose.** These BOS-agent keys have no counterpart in either runtime and
are ignored, with one DEBUG log line: `tools` (and `exclude_tools`, tool `usages`),
`plugins`, `plugin-bindings`, `max_tokens`, `max_iteration_handoff`, `shutdown_handoff`,
`tool_noise_filter`, `history_attribution`, `reasoning_effort` (the config key — a
turn's `llm_args["reasoning_effort"]` is honoured), `agent_name`, and `description`.
`description` is still used by BOS's own agent registry, which is what `AskSubagent`
lists.

---

## Permission

`permission` has three values and no default. It bounds **the filesystem**, not the
tools: an agent can call every BOS tool its `mcp_tools` lists at every level, mutating
ones included, because those tools are the host's own and are gated by that list, not by
`permission` ([Exposing BOS tools](#exposing-bos-tools)).

Neither runtime asks anyone anything. BOS has no channel to carry an approval request to
a person and back, so every permission decision is made by policy, at once, and an
unattended run cannot hang waiting on one.

### Claude Code

`permission_mode` is only an approval policy, so BOS builds the confinement itself: a
per-level allowlist of the CLI's tools (a tool not offered is not callable, even if the
model asks), a `PreToolUse` hook that resolves every file-tool path and denies it outside
`cwd`, and — at `workspace-write` — the CLI's OS sandbox for bash.

| | `read-only` | `workspace-write` | `full-access` |
|---|---|---|---|
| Built-in tools offered | `Read` | `Read`, `Write`, `Edit`, `NotebookEdit`, `Bash` | every tool except `ListAgents`, `SendMessage`, `AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode` |
| File tools | `Read` only, path-checked to `cwd` | path-checked to `cwd`: reads and writes outside it are denied | not confined |
| Bash | not offered | runs in the OS sandbox: writes are confined to `cwd` (and a temp directory BOS gives the turn); a command asking to leave the sandbox still runs inside it | no sandbox |
| Web tools (`WebFetch`, `WebSearch`) | not offered | not offered | offered |
| CLI `permission_mode` | `default` | `acceptEdits` | `bypassPermissions` |

Below `full-access` the hook also denies file-tool paths inside the CLI's own
configuration directory (`CLAUDE_CONFIG_DIR`, `~/.claude` by default), and — when
`setting_sources` opts into the repository's settings — paths under `<cwd>/.claude/`,
so an agent cannot plant settings for its own next turn. The tools reaching other
local Claude sessions or a user at a terminal are never offered, at any level.

**`workspace-write` needs the bash sandbox, and BOS refuses rather than run bash
unsandboxed.** On Linux the sandbox needs `bwrap` and `socat` on `PATH`
(`apt install bubblewrap socat`); on macOS, `/usr/bin/sandbox-exec`. Where they are
missing, and always on Windows, building a `workspace-write` Claude Code agent fails,
naming what to install. If the CLI ever reports its sandbox disabled during a turn, BOS
ends that turn with an error. Each `workspace-write` turn gets a temp directory of its
own (`TMPDIR`), removed after the turn, so the agent's commands keep a working temp
directory while the `/tmp/claude-<uid>` shared by the user's other Claude sessions is not
writable.

!!! warning "What Claude Code's confinement does not cover"
    - **Bash reads.** At `workspace-write` the sandbox confines bash *writes*, not
      reads: a command such as `cat` can read a file outside `cwd`.
    - **`@path` in a prompt.** The CLI reads a file named with `@<path>` in the prompt
      itself, before any tool call, and sends it to the model — outside `cwd`, whatever
      the level.
    - **Agents sharing a `cwd` under `workspace-write`.** The sandbox's mount points under
      `<cwd>/.claude/` are shared, so a sandboxed command can rarely fail (never escape)
      when several such agents run at once. Give each concurrent `workspace-write` agent
      its own `cwd`.
    - **`full-access`** confines nothing on the filesystem.

The CLI inherits BOS's environment. BOS turns off the variables that would load plugins,
hooks, settings, MCP servers or skills its default leaves out, and the CLI's auto-memory
(BEP 19 §3.12).

### Codex

`permission` chooses Codex's own OS sandbox, and that sandbox is the whole boundary.
Every level runs with Codex's approval policy at `never`: an escalation past the sandbox
is refused by Codex itself, before the command runs, and the agent carries on with what
the sandbox allows.

| `permission` | Codex sandbox | Writes | Reads |
|---|---|---|---|
| `read-only` | `read-only` | none | not confined |
| `workspace-write` | `workspace-write` | `cwd` — and, by Codex's default, `/tmp` and `$TMPDIR` | not confined |
| `full-access` | `danger-full-access` | anywhere | anywhere |

Reads outside `cwd` are not confined at any level: neither of Codex's sandbox policies
restricts reads. Codex also reads the operator's `~/.codex/config.toml`, so that
machine's Codex configuration reaches the runtime, where Claude Code's default keeps the
equivalent files out.

---

## Auth

`auth = "subscription"` (the default) means the runtime's existing login;
`auth = "api_key"` opts out of BOS's check and lets the runtime use whatever credential
it finds, billed to that credential. BOS stores no token either way.

**Claude Code** checks at build time, and it checks the environment, not the login: the
CLI inherits BOS's whole environment, so `create_agent` refuses `auth = "subscription"`
while anything there would move the run off the subscription login, naming each one —
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`,
`ANTHROPIC_PROFILE`, `ANTHROPIC_CONFIG_DIR`, `ANTHROPIC_FEDERATION_RULE_ID`,
`ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_UNIX_SOCKET`, `CLAUDE_CODE_SIMPLE`, and the
`CLAUDE_CODE_USE_*` provider switches (Bedrock, Vertex, Foundry, the AWS and Google Cloud
platforms, Mantle, a gateway) — or the key file `/home/claude/.claude/remote/.api_key`
exists. An empty value is not refused; `CLAUDE_CODE_OAUTH_TOKEN` (a subscription token)
and `ANTHROPIC_BASE_URL` are not refused. There is no login check: a missing login
surfaces at the first turn, from the CLI.

!!! tip "A project `.env` counts"
    `[platform].envfile` and `[platform.envs]` write into BOS's environment, which the
    CLI inherits. A project that keeps `ANTHROPIC_API_KEY` in `.bos/.env` for its LiteLLM
    agents will have its subscription Claude Code agents refused — remove the key from
    the environment BOS runs in, or set `auth = "api_key"` to bill it deliberately.

**Codex** checks the login itself, on the agent's first turn (or first native transcript
read) rather than at build time, because the check is a call to the `codex app-server`
child, which BOS starts lazily. With no account logged in, that turn fails with
*"auth="subscription" but no Codex account is logged in. Run `codex login`, or set
auth="api_key" to opt out of this check."* The check runs once per agent and is bounded
at 30 seconds.

**Refused at build time, and what to do** — each fails `create_agent` (and so
`BosApp` entry, an actor's start, or `boscli ask`) with a message naming the cause:

| Refusal | Runtime | What to do |
|---|---|---|
| A credential or provider variable in the environment, or the well-known key file, under `auth = "subscription"` | Claude Code | Remove it from the environment BOS runs in, or set `auth = "api_key"`. |
| `workspace-write` without the bash sandbox (missing `bwrap`/`socat`, missing `sandbox-exec`, or Windows) | Claude Code | Install what the message names, or use `read-only` / `full-access`. |
| An enterprise MCP config, `managed-mcp.json`, exists (`/etc/claude-code/managed-mcp.json` on Linux) | Claude Code | The CLI then refuses the `--strict-mcp-config` BOS always sends. BOS does not work around the administrator's policy: run the agent on a host without that file. |
| `native_options` naming a key outside the allowlist | Claude Code | Remove it; the message gives each key's reason. |
| `native_options` naming a setting BOS decides | Codex | Remove it; set `permission`, `cwd`, `model` or the prompt keys instead. |
| No `permission`, an unknown key, a `cwd` outside the workspace, both prompt keys | both | Fix the config as the message says. |

---

## Sessions and what BOS stores

**One native session per `chat_id`.** The first turn on a `chat_id` starts a Claude Code
session or a Codex thread. BOS records its id in the metadata of the assistant message it
commits, and every later turn on that `chat_id` resumes it — after a process restart
too, since the id is read back from the chat store. Use your own conversation key as the
`chat_id`. (`boscli ask` and `AskSubagent` make a new `chat_id` on every call, so each
call is a new session.)

- **A session that cannot be resumed raises.** If the runtime no longer has it —
  deleted, archived, pruned — the turn fails with *"… could not be resumed, and BOS does
  not silently start a fresh session under the same chat_id"*. Continue on a new
  `chat_id`.
- **Switching a chat's runtime starts a new session**, with a WARNING naming the chat and
  both runtimes. Nothing carries over, and switching back starts yet another one.
- **One turn at a time per chat.** A second concurrent turn on the same `chat_id` raises
  *"Agent '…' already has a turn running on chat '…'"*; different chats run concurrently.

**Two messages per turn.** BOS commits the user message and the final answer — the
assistant message carrying `external_runtime`, `native_session_id`, `native_turn_id` and
`usage` in its metadata. It does not store the runtime's tool calls, tool results or
thinking; those stream live to an `event_sink` ([Running a turn](#running-a-turn)) and
live on in the runtime's own transcript. That thin record is what `AgentActor`,
`AskSubagent` and `list_chats` see, and it is the part BOS guarantees.

A turn that fails, times out, is aborted or exhausts its schema retries commits
nothing — but on a chat that already has a session, whatever reached the runtime stays in
that session, and the next turn's model sees it. A turn BOS stops with `request_stop()`
commits what it had produced.

**Reading the runtime's own transcript.** `BosApp.get_messages` reads either record:

```python
await app.get_messages(chat_id, source="bos")     # BOS's two-messages-per-turn record
await app.get_messages(chat_id, source="native")  # the runtime's own transcript
await app.get_messages(chat_id)                   # "auto": native for an external chat, else bos
```

`source="native"` is the conversation, not the work: user and assistant messages only,
each carrying `metadata["source"]` (the runtime) and `metadata["native_item_id"]`.
It is a live read of a store BOS does not own, and **it promises nothing** — the runtime
compacts and prunes on its own schedule, so a read can return less than before, or fail.
`source="bos"` is the record BOS stands behind.

- **Claude Code** reads the session's JSONL under `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/`
  directly — no CLI starts. `native_turn_id` is `None` on each message; the CLI's own
  `[Request interrupted by user]` entries appear as ordinary user text. A session on
  record whose transcript is gone raises.
- **Codex** asks the agent's `codex app-server` (starting it, with its login check, if
  needed), bounded by `timeout_seconds`. `commentary` messages are left out; a turn Codex
  did not fully load appears as one `system` message marking the gap, with
  `metadata["items_view"]`.

The read goes to the one agent built on the `BosApp` that declares the chat's runtime.
With none it raises, telling you to build one; with more than one it raises rather than
guess, because the wrong one can read a different transcript — call
`app.agent("<kind>").native_messages(chat_id)` on the one you mean.

---

## Project docs: `CLAUDE.md` and `AGENTS.md`

**Claude Code.** By default the CLI loads no settings file and no memory file, so BOS
reads `<cwd>/CLAUDE.md` itself and appends it to the system prompt, after
`system_prompt`, under the heading `# CLAUDE.md in the working directory`:

- Only that one file, as text: no `@` import is expanded, and no `CLAUDE.md` in a parent
  or subdirectory, no `CLAUDE.local.md` and no user-level file is read.
- It must be a regular file inside `cwd`. A symlink leaving `cwd`, a directory or a FIFO
  is not read, with a WARNING naming the path. Past 40,000 bytes it is cut off, with a
  line saying so, and a WARNING. Each WARNING is logged once per agent.
- It is read again at every turn.
- It is not appended to `base_instructions`, which means you own the whole prompt, nor
  when `setting_sources` includes `"project"` — the CLI then loads it itself, with the
  rest of its memory loading and the repository's settings.
- **To turn it off**, set `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` in the environment BOS runs
  in — the CLI's own switch, which BOS's read obeys (as it does `CLAUDE_CODE_SAFE_MODE`
  and `CLAUDE_CODE_SIMPLE`).

**Codex** reads `AGENTS.md` itself. BOS offers no way to suppress it:
`project_doc_max_bytes = 0` sent through `native_options.config` reaches Codex, but did
not stop the file reaching the model in live runs.

---

## Exposing BOS tools

A runtime has its own tools and runs in another process, so it cannot call your
`@ep_tool`s directly. List the ones it may call:

```toml
[agents.implementer]
_parent = "codex"
permission = "read-only"
mcp_tools = ["CreateTicket", "GetOrder"]   # default []; "*" is not accepted
```

- **One server, per harness.** BOS serves the listed tools from one MCP server on
  `127.0.0.1`, an ephemeral port, started the first time an agent has something to
  serve. Each agent gets its own bearer token, and the server lists and runs only that
  agent's tools.
- **Names are checked.** A name with no registered `@ep_tool` is skipped with one WARNING
  per name at the agent's first turn; `boscli inspect agent <name>` shows such names as
  unavailable before any turn. An agent with nothing to serve starts no server.
- **The tool runs in BOS.** A call runs your tool in the BOS process, with its
  `[exts.ep_tool.<Name>]` config; a tool that raises becomes an error result for the
  model.
- **`permission` does not gate them.** A `read-only` agent can still call a mutating tool
  you listed. `mcp_tools` is the setting that decides what the agent can change through
  BOS.
- **Only BOS's server.** Claude Code loads only the MCP servers BOS passes — never a
  repository's `.mcp.json` or the operator's own — and sees BOS's tools as
  `mcp__bos-tools__<Tool>`. Codex runs with its approval policy at `never`, so BOS
  pre-approves its own `bos-tools` server; an MCP server from the operator's
  `~/.codex/config.toml` whose calls need approval is refused.
- **The token is in the child's environment**, where the agent's own shell can read it.
  It opens only that agent's own tools.

Expose your application's domain API. File, shell and web search are better left to the
runtime's own tools.

---

## Running a turn

`ask()` and `run()` take the same arguments as a BOS agent's, and `run()` returns an
`AgentResult`: `output` (the final text, or the validated object for a `schema` turn),
`structured`, `finish_reason`, `usage` and `turn_id`. `iterations` is always `1` — one
turn handed to the runtime.

- **Content.** A string, or text/image/file parts. Claude Code gets an image path read
  and base64-encoded by BOS, and a file part as a line of text,
  `[attachment: <path or url> (<mime type>)]`, for the model to open with its own tools.
  Codex gets images by URL or local path and a file part as a mention of a local path;
  a file part given by URL raises.
- **`llm_args`.** `model` sets the turn's native model; `reasoning_effort` becomes the
  runtime's effort setting.
- **`usage`** uses the same keys for both: `input_tokens` (every input token, cached ones
  included), `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
  `total_tokens`, and `reasoning_output_tokens` (Claude Code: only when the CLI reports
  thinking).

### Streaming

Pass an `event_sink` to watch the turn as it runs. Both runtimes emit BOS `TurnEvent`s:

| Event | Claude Code | Codex |
|---|---|---|
| `tool` / `start` | each tool call; `tool_name` is the tool (`Bash`, `mcp__bos-tools__CreateTicket`, …) | each command run (`tool_name` is the command line) and each MCP tool call |
| `tool` / `finish`, `fail` | its result — `fail` when the result is an error | the item completing (always `finish`) |
| `response` / `finish` | every text block the model writes | every agent message — commentary and the final answer look alike here |
| `turn` / `finish` | the end of each attempt | the end of each attempt |
| `turn` / `fail` | a turn that spent `max_iterations` (`stage` and `detail` `max_iteration`) | — |

Anything else — thinking, file-change items, a sub-agent's own messages, the CLI's
internal structured-output tool — is skipped rather than half-mapped. The stream is not
the answer: read `AgentResult.output` for that. A `schema` turn emits one `turn`/`finish`
per attempt, unlike a BOS agent.

### Structured output

`run(..., schema=…)` asks the runtime for JSON matching the schema (Claude Code through
its own structured-output tool, Codex as `output_schema`), then validates the reply
locally with the same validator every BOS agent uses. A reply that fails validation gets
a correction message, up to `max_schema_retries` (default 1); exhausting them raises
`StructuredOutputError` and commits nothing. On Claude Code a turn that spends
`max_iterations` is not validated and closes as `(max iterations reached)` — and the
CLI's own nudge to comply already spends a turn, so `max_iterations = 1` with a schema
can run out before BOS retries.

### Stop, abort, timeout

| What happens | What the caller gets | Committed? |
|---|---|---|
| The `interrupt` callback returns a message | The message is delivered into the running turn (Claude Code folds it into the turn; Codex steers the turn), which carries on | — |
| The `interrupt` callback raises `AbortTurn` | The runtime is told to stop; `(turn aborted before completion) …`, `finish_reason="aborted"` | no |
| `request_stop()` during a turn | The runtime is interrupted; the text produced so far (or `""`) | yes |
| A turn started after `request_stop()` | `(interrupted: the agent is shutting down)`, `finish_reason="shutdown"`, before any vendor call | no |
| `timeout_seconds` expires | `TimeoutError` naming the phase — mid-turn, after the runtime is interrupted | no |

`timeout_seconds` bounds **each attempt**, not the whole call — a schema retry gets a
window of its own — and also bounds startup: Claude Code's CLI start (*"… at startup
(connect)"*), and Codex's thread setup and turn request, each named in the error. A
timeout during Codex's turn request cannot stop a native turn that already started; it
ends when the client closes. Unset, nothing is bounded by it. `request_stop()` is
one-way. Closing the harness stops every in-flight turn, gives them ten seconds to wind
down, then closes their clients regardless.

`finish_reason` is the runtime's own, verbatim:

| Outcome | Claude Code | Codex |
|---|---|---|
| Completed | the CLI's terminal reason (e.g. `completed`), or its stop reason when it gives none | `completed` |
| `max_iterations` spent | `max_turns`, with output `(max iterations reached)` | — |
| Stopped by `request_stop()` | `aborted_tools` (a tool was running) or `aborted_streaming` (the model was answering) | `interrupted` |
| `AbortTurn` / after a stop | `aborted` / `shutdown` | `aborted` / `shutdown` |

An interrupted or failed turn that BOS did not ask for raises, and commits nothing.

---

## Troubleshooting

| Message (or symptom) | Cause | Fix |
|---|---|---|
| *The '…' agent runtime needs its optional dependency.* | The extra is not installed where BOS runs. | `pip install 'bos-ai[claude-code]'` / `'bos-ai[codex]'` into that environment. |
| *Unknown agent 'codex'* (or `'claude-code'`) | The reserved kinds are not registered agents on their own. | Add `[agents.codex]`, a named agent with `_parent = "codex"`, or `await app.build_agent("codex", agent_cfg=…)`. |
| *… must set `permission` to one of […]; got None.* | `permission` has no default. | Set it. |
| *Unknown config key(s) for the '…' runtime: […]* | A key the runtime does not know — `setting_sources` on Codex, say, or a typo. | Remove or correct it; the message lists the known keys. |
| *`cwd` resolves to …, which is outside the workspace …* | `cwd` escapes the workspace root. | Use a path inside the directory holding `.bos/`. |
| *Agent '…' sets `external_runtime`, which BOS writes, not config.* | A hand-written `external_runtime`. | Use `_parent = "<runtime>"`. |
| *`auth = "subscription"` (the default), but the Claude Code CLI inherits this process's environment …* | A credential or provider variable is set — often from a project `.env`. | Remove what it names, or set `auth = "api_key"`. |
| *`permission = "workspace-write"` is refused on this host: …* | No bash sandbox: `bwrap`/`socat` missing, or Windows. | Install what it names (`apt install bubblewrap socat`), or change `permission`. |
| *… managed-mcp.json exists: this host's administrator gives an enterprise MCP config exclusive control …* | An enterprise MCP config on the host. | Run the Claude Code agent on another host. |
| *`native_options` for the 'claude-code' runtime may carry only […]* / *`native_options` may not set […]* | A refused `native_options` key. | Remove it; the message gives the reason. |
| *auth="subscription" but no Codex account is logged in.* | No Codex login, at the first turn. | `codex login`, or `auth = "api_key"`. |
| *… could not be resumed, and BOS does not silently start a fresh session under the same chat_id* | The runtime no longer has the chat's session. | Continue on a new `chat_id`. |
| *Agent '…' already has a turn running on chat '…'* | Two turns at once on one chat. | Serialize turns per `chat_id`. |
| *… exceeded timeout_seconds=… at startup (connect)* | The Claude Code CLI did not finish starting in time. | Raise `timeout_seconds`, or check the CLI starts on this host. |
| *… turn … was ended because the CLI reported its bash sandbox disabled under workspace-write* | The sandbox degraded mid-run. | Check `bwrap` can create a sandbox on this host (unprivileged user namespaces). |
| WARNING *mcp_tools names '…', which is not a registered tool; it is not exposed.* | A name with no `@ep_tool`. | Fix the name, or load the extension that registers it. |
| WARNING *the CLI reports BOS's MCP server 'bos-tools' as … so the agent has none of its mcp_tools this turn* | Claude Code could not connect to BOS's server, or an MCP allow/deny list in its settings dropped it. | Check the administrator's managed settings, or the settings files your `setting_sources` loads. |
| Every Codex turn fails with *url is not supported for stdio in `mcp_servers.bos-tools`* (or rejects `bearer_token`) | The operator's `~/.codex/config.toml` has its own `[mcp_servers.bos-tools]`, which Codex merges with BOS's. | Rename that entry. |
| WARNING *Chat '…' last ran on the '…' runtime, so it has no '…' session* | The chat switched runtimes. | Expected: the turn starts a new session. |
| *get_messages(…, source="native") has no runtime to ask* | No external turn is recorded for that chat. | Use `source="bos"`. |
| *Chat '…' was served by the '…' runtime and 2 built agents declare it* | Two built agents share the runtime. | Call `app.agent("<kind>").native_messages(chat_id)`. |

`boscli` logs at `ERROR` by default, which hides every WARNING above: run
`boscli -l WARNING …` to see them, or `-l DEBUG` for the line naming dropped keys.
