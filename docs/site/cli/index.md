# CLI

`boscli` is the command-line entry point for BOS. Commands operate on the workspace
discovered from the current directory (see [Configuration](../configuration/index.md#how-the-config-is-discovered)).

```bash
boscli --help
```

## Global options

| Option | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `-c`, `--config <path\|preset>` | `BOS_CONFIG` | _discovered_ | A config file path **or** a built-in preset name (e.g. `team`). |
| `-l`, `--log-level <LEVEL>` | `BOS_LOG_LEVEL` | `ERROR` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Logs go to stderr. |

## Commands

| Command | What it does |
| --- | --- |
| [`ask`](#ask) | Run a single agent turn in-process and print the reply. |
| [`init`](#init) | Guided project scaffolding. |
| [`gateway`](#gateway) | Start/stop/inspect the runtime (subcommands). |
| [`tui`](#tui) | Connect the terminal UI to a running gateway. |
| [`doctor`](#doctor) | Health-check config, paths, environment, and credentials. |
| [`inspect`](#inspect) | Introspect harness/config/runtime state. |
| [`memory`](#memory) | Administer the memory backend. |

Commands are lazy-loaded, and installed packages can add their own via the
`boscli.commands` entry point — see [Packaging & entry points](../extending/entry-points.md).

---

### `ask`

Run one agent turn **in-process** (no gateway) and print the final reply to stdout. Progress
streams to stderr when attached to a TTY. The model is honored on every invocation.

```bash
OPENAI_API_KEY=<key> boscli ask "summarize README.md" --model openai/gpt-4o
cat data.txt | boscli ask "extract the action items" --stdin
boscli ask "research the auth flow" --agent researcher
```

| Option | Meaning |
| --- | --- |
| `--stdin` | Read additional input from standard input. |
| `--model <provider/model>` | Override the model for this run (else `BOS_MODEL` / config). |
| `--agent <kind>` | Run a specific registered agent kind instead of the main one. |
| `-w`, `--workspace <dir>` | Use a specific workspace directory. |

`ask` works even without a project: with no workspace found it can run against a preset/model
directly, which is what the one-line quick start does.

---

### `init`

Scaffold a new BOS project in the current directory through a guided wizard.

```bash
boscli init                # guided: purpose -> archetype -> provider/model
boscli init --minimal      # write the fully-commented reference config only
```

The wizard walks through:

1. **Purpose** — a one-line description that seeds the main agent's prompt.
2. **Archetype** — `workspace` (a plain project that loads Python from `./extensions`) or
   `package` (an installable `src/<pkg>/` whose extensions register via the `bos.exts` entry
   point).
3. **Provider/model** — detects available API keys and offers a model menu.
4. **Scaffolding** — writes `.bos/config.toml`, `.env`, `agents/`, `extensions/`/`src/`, etc.
5. **Credential probe** — one quick LLM call to confirm the key works (skip with `--no-probe`).
6. **Git init** — offered if the directory is not already a repo.

| Option | Meaning |
| --- | --- |
| `--minimal` | Emit the commented template config and skip the wizard. |
| `--name <name>` | Package/distribution name (package archetype). |
| `--no-probe` | Skip the credential probe LLM call. |

See [Create a project](../tutorials/create-a-project.md) for a full walkthrough.

---

### `gateway`

Manage the runtime process that hosts actors and channels.

```bash
boscli gateway start       # boot the runtime (hosts actors + channels + HTTP control plane)
boscli gateway status      # is it running? on which port? (reads gateway.state)
boscli gateway stop        # shut it down
boscli gateway restart     # stop then start
```

`gateway start` builds the harness, registers an actor per `[runtime.actors.<name>]`, starts
each `[[runtime.channels]]`, and serves the HTTP control plane on
`[runtime.gateway].host:port`. With `port = 0` the OS assigns a free port; the chosen port
and PID are written to `gateway.state` for discovery. See
[Runtime & gateway](../concepts/runtime.md).

---

### `tui`

Launch the terminal UI and connect it to a running gateway (discovered via `gateway.state`).
This is the interactive way to chat with your actors, switch between them, and attach files.

```bash
boscli tui
```

---

### `doctor`

Run health checks: config resolves and validates, paths exist, required environment
variables and credentials are present. Use it first when something will not start.

```bash
boscli doctor
```

---

### `inspect`

Introspect the resolved harness, config, and runtime state — useful for confirming which
extensions, agents, and actors are actually registered.

```bash
boscli inspect --help
```

---

### `memory`

Administer the memory backend (list, show, and manage stored memories). The memory system
itself is covered in [Memory & skills](../concepts/memory-and-skills.md).

```bash
boscli memory --help
```

---

## Environment variables

| Variable | Purpose |
| --- | --- |
| `BOS_HOME` | BOS home directory (default `~/.bos`). |
| `BOS_CONFIG` | Config file path or preset name. |
| `BOS_MODEL` | Default model id (`provider/model`). |
| `BOS_CONSOLIDATOR_MODEL` | Model for off-turn memory consolidation. |
| `BOS_GATEWAY_API_KEY` | Default env var name for the gateway control-plane key. |
| `BOS_CAPABILITY_LIMIT` | Max skills/sub-agents listed in the system prompt (default 50). |
| `BOS_LOG_LEVEL` | Default log level. |
| `<PROVIDER>_API_KEY` | Provider credentials (e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`). |
