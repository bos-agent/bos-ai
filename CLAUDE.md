# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Tooling

- Use `uv run ...` for all Python entrypoints. The project targets Python `>=3.13`; the system `python3` may be too old and fail on `tomllib`.
- Run the full test suite: `uv run pytest -q`
- Run a single test file: `uv run pytest -q tests/test_harness.py`
- Run a single test: `uv run pytest -q tests/test_harness.py -k test_name`
- Lint: `uv run ruff check src tests`
- CLI help: `uv run boscli --help`
- Prefer `uv run boscli ...` for local CLI invocation (not system `boscli`).

## Repo Layout

```
src/bos/
  cli/          - Click CLI entrypoints (boscli init, start, stop, tui, auth)
  config/       - Workspace discovery, TOML config loading, agent resolution
  core/         - Runtime primitives: ReActAgent, AgentActor, AgentHarness,
                  ExtensionPoint, ToolRegistry, contracts, LLM client
  extensions/   - Channel, provider, tool, store, interceptor implementations
  protocol/     - Message envelope, content types, turn events
  runner/       - Runtime assembly: wires harness + actor + channels in-process
tests/           - pytest coverage mirroring the above
docs/architecture/ - Design docs for core, config, protocol, runner
```

## Architecture

### Extension Points (the core pattern)

The framework is built around named `ExtensionPoint` registries (defined in `src/bos/core/contract.py`). Each is a key-value registry of implementations that can be configured via TOML:

| EP | Protocol | Purpose |
|---|---|---|
| `ep_tool` | async fn → str | LLM-callable tools |
| `ep_provider` | async fn → LLMResponse | Model backends (litellm by default) |
| `ep_agent` | `Agent` protocol (`.ask()`) | Named agent factories |
| `ep_channel` | `Channel` protocol (`.run(mailbox)`) | External interfaces (HTTP, Telegram) |
| `ep_mail_route` | `MailRoute` protocol | Message transport between actors |
| `ep_message_store` | `MessageStore` protocol | Chat history persistence |
| `ep_memory_store` | `MemoryStore` protocol | Long-term memory persistence |
| `ep_consolidator` | `Consolidator` protocol | Chat history summarization |
| `ep_skills_loader` | `SkillsLoader` protocol | Skill discovery and loading |
| `ep_turn_interceptor` | `TurnInterceptor` protocol | Turn lifecycle hooks |
| `ep_actor_command` | async fn → str\|dict | Actor-level slash commands |

Extensions register via decorator (e.g., `@ep_tool(name="...", description="...", parameters={...})`) or via TOML config in `[platform.extensions]`.

### Agent Lifecycle

1. **Config loading** (`src/bos/config/workspace.py`): `Workspace` resolves `.bos/config.toml` via upward directory search → `BOS_DIR` env var. Inline agents in TOML load first, then external agents from `agent_dirs/` (`.toml` or `.md` files, alphabetical), with last-wins deduplication.

2. **Bootstrap** (`bootstrap_platform()`): Loads extensions, registers all named agents into `ep_agent`.

3. **Harness** (`AgentHarness` in `src/bos/core/harness.py`): Lifecycle container for shared services (mail route, message store, memory store, consolidator, skills loader, interceptors, LLM client). Created via `async with workspace.harness() as harness:`.

4. **Runner** (`src/bos/runner/runner.py`): Creates the `AgentActor` bound to the main agent's mailbox address (`agent@main`), then runs actor + channels concurrently via `asyncio.TaskGroup`.

### AgentActor (`src/bos/core/actor.py`)

The actor is the concurrency spine. It polls its bound `MailBox` for messages, maintains per-`chat_id` session state (pending/interrupt buffers, generation counters), and drives the `ReActAgent.ask()` loop in a per-chat task. It handles interrupt/abort semantics and merges multiple pending messages from the same chat.

### ReActAgent (`src/bos/core/agent.py`)

The turn loop implementation. Each `ask()` call:
1. Loads chat history, builds system prompt (base + memories + tools + skills + subagents + system info)
2. Calls interceptor hooks at each stage: `prepare` → `before_llm` → LLM call → `after_llm` → tool calls + `after_tool` → repeat → `final_response`
3. Emits `TurnEvent` objects through an optional `EventSink`
4. Supports interrupt callbacks for the actor to inject messages mid-turn
5. Registers three local tools: `AskSubagent`, `UpdateMemory`, `LoadSkill`

### Messaging (`src/bos/protocol/`)

`Envelope` carries messages with sender, content, content_type (MESSAGE, COMMAND, INTERRUPT_MESSAGE, INTERRUPT_ABORT, TURN_EVENT, SYSTEM, etc.), chat_id, and metadata. `MailRoute.bind(address)` returns a `MailBox`; actors and channels communicate exclusively through mailboxes.

### Agent Configuration

Agents are capabilities-deny by default. Omitted `tools`, `skills`, `memories`, `subagents` = empty allow-lists. Use `"*"` to allow all. `exclude_*` lists subtract from allow-lists. `_default` is the fallback agent name when none is configured.

### Channel Configuration

Channels target `agent@main` directly. Channel-to-channel routing is not supported. Each channel gets its own mailbox via `mail_route.bind(channel_bind_address)`. In Docker mode, HttpChannel host is normalized to `0.0.0.0`.

## Working Notes

- Keep package boundaries explicit. If config loading changes, check `README.md` and `docs/architecture/*.md`.
- Prefer small, reversible diffs. Extend existing patterns before adding new abstractions.
- Pull request titles must follow semantic/conventional format (`feat(config): ...`, `fix(runner): ...`).
- `uv run ruff check src tests` is a useful signal but the repo may contain pre-existing lint findings.
- `src/bos/core/__init__.py` is the public API surface. Internal helpers with `_` prefix are exported for use by extensions but are not considered stable.


## Code Search

Use `uv run semble search` to find code by describing what it does or naming a symbol/identifier, instead of grep:

```bash
uv run semble search "authentication flow" ./my-project
uv run semble search "save_pretrained" ./my-project
uv run semble search "save model to disk" ./my-project --top-k 10
```

Use `uv run semble find-related` to discover code similar to a known location (pass `file_path` and `line` from a prior search result):

```bash
uv run semble find-related src/auth.py 42 ./my-project
```

`path` defaults to the current directory when omitted; git URLs are accepted.

## Workflow

1. Start with `uv run semble search` to find relevant chunks.
2. Inspect full files only when the returned chunk is not enough context.
3. Optionally use `uv run semble find-related` with a promising result's `file_path` and `line` to discover related implementations.
4. Use grep only when you need exhaustive literal matches or quick confirmation of an exact string.