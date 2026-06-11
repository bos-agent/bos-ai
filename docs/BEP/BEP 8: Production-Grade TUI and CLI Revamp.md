# BEP 8: Production-Grade TUI and CLI Revamp

Status: **accepted — implemented on `tui-rewrite`** (reconciled with the implementation 2026-06-10)

---

## Core Insight

The previous terminal UI (`ChatApp` in `src/bos/cli/tui_app.py`) served as a basic WebSocket verification client rather than a production-grade tool. It had several fundamental limitations:
1. **Raw, noisy transcript**: User inputs, replies, status logs, tool outputs, and thinking cycles were dumped to a single log with inconsistent formatting and emoji noise.
2. **Clunky navigation**: Operations like switching chats required typing raw slash commands (e.g., `/chats`, `/resume <id>`) and reading tabular text dumped to the console.
3. **No BEP 7 integration**: The client did not natively understand optimistic concurrency (revisions), leading to potential silent transcript conflicts, nor did it adapt to server-side turn states and event flows gracefully.
4. **Confusing CLI interface**: Interactive mode was split between `boscli ask -i` (spawning an in-process local TUI) and `boscli tui` (connecting the TUI to a running background gateway), with two divergent code paths (`LocalClient` vs `GatewayClient`).

This BEP revamps the TUI into a clean, full-width terminal application built with `Textual`, reorganizes the CLI command surface around the gateway, and integrates with the concurrency and connection resiliency patterns established in BEP 7.

To maximize the available horizontal screen width for reading code diffs, logs, and files, the design rejects narrow side panels. Navigation that needs server-side data (chat selection) uses centered modal overlays.

---

## Goals

1. **Clean, gateway-backed Textual TUI**:
   - A flat, readable transcript: user/assistant turns rendered with Markdown, tool calls paired with their results, thinking previews, and inline task-list rendering.
   - A chat selection modal (`Ctrl+R` or `/resume` without arguments) listing chats by recency for resuming.
   - A permanently multiline prompt widget that grows with content.
   - Client-side prompt buffering showing stacked queued inputs above the prompt, and a minimal modal input for active-turn steering.
2. **Unified, gateway-only CLI organization**:
   - `boscli ask [MESSAGE]` is the non-interactive oneshot entrypoint; `boscli tui` is the interactive entrypoint. Both are pure gateway clients and auto-start the gateway in the background when none is running.
   - Lifecycle commands are grouped under `boscli gateway`.
   - The in-process interactive mode (`boscli ask -i`, `LocalClient`, `_run_interactive`) is removed entirely.
3. **Optimistic Concurrency & Rehydration (BEP 7)**:
   - Track and send `base_revision` values in all outbound client requests.
   - Server-side preflight rejects stale sends with structured events carrying `missing_messages`.
4. **Resiliency and Connection State**:
   - A persistent reconnection mechanism for WebSocket failures with exponential backoff.
   - Visible connection status (`● connected` / `○ reconnecting…`) in the status bar.
5. **Interactive Autocomplete**:
   - Autocomplete dropdowns for slash commands and `@mention` actor routing.

---

## Non-Goals

1. **Multi-User Authentication**: The TUI and gateway remain single-user applications. A permissive single-user API key (optional) is sufficient.
2. **Web Frontends**: This proposal covers the terminal-based interactive experience only.
3. **Generic HTTP REST Sends**: Live conversational exchange and server-side commands use the WebSocket channel.
4. **Card-based transcript UI**: Focusable message cards, sticky user cards, trace-card replacement transitions, and a detailed turn-history page are out of scope. The transcript is a single scrollable log.
5. **Feed navigation & copy shortcuts**: Keyboard focus never leaves the prompt; there is no card selection or `c`-to-copy mechanism. Terminal-native selection/copy is used instead.
6. **TUI configuration section**: There is no `[cli.tui]` config (themes, history files, prompt sizing). The TUI ships with fixed, sensible defaults.

---

## CLI Command Surface

### 1. `boscli ask [MESSAGE]`
Non-interactive oneshot execution against the gateway:
- Connects to the running gateway (discovered via `gateway.state`); if none is running, starts one in the background and leaves it running.
- Each invocation uses a unique channel id (`ask:<user>-<rand>`), giving the task a fresh chat.
- Streams turn-event progress (tool calls, task board) to **stderr** when it is a terminal; prints the agent's final reply to **stdout**, so output stays pipeable.
- `--stdin` appends stdin content to the message; `-w/--workspace` overrides the workspace directory.
- The invocation directory (or `-w` override) is stamped as `workdir` metadata on each message, so the agent knows where the user is working.

### 2. `boscli tui`
The interactive entrypoint:
- Connects to the running gateway as a dynamic WebSocket channel client (`GatewayClient`); if none is running, starts one in the background and leaves it running.
- Uses a stable channel id (`tui:<username>` by default; `--channel-id` overrides), so reconnecting resumes the same channel cursor. If another TUI client holds the channel, the user is prompted to take over.
- `--host` / `--port` explicitly target a gateway endpoint, bypassing discovery and auto-start.

### 3. `boscli gateway [start|stop|status|restart]`
Manages the background gateway process (process or Docker runtime).

There is no in-process chat mode: both `ask` and `tui` always go through the gateway, so there is exactly one execution path (`GatewayClient` → WS channel → actor).

---

## TUI Layout

A clean vertical layout preserves maximum terminal width for transcript content. A single status bar sits directly above the prompt (there is no separate status header/footer pair); the Textual footer shows the active key bindings.

```
┌───────────────────────────────────────────────────────────────────┐
│ boscli tui — ● Gateway | chat-id                                  │ <= Header
├───────────────────────────────────────────────────────────────────┤
│ ❯ You                                                             │
│   please refactor the auth module to use tokens                   │
│                                                                   │
│ ● analyzing auth module dependencies…                             │
│   ReadFile(path='src/auth.py')                                    │
│    └ 142 lines read                                               │
│                                                                   │
│ ▸ Assistant                                                       │
│ I've refactored the auth module. Here are the changes: …          │
│                                                                   │
├───────────────────────────────────────────────────────────────────┤
│ ⏳ queued message (sent when the current turn finishes)            │ <= queue stack (when non-empty)
├───────────────────────────────────────────────────────────────────┤
│  ● connected · Chat: chat-id · Channel: tui:jerry · ○ ready       │ <= Status bar
│ Send a message… (Enter to send, Ctrl+J for newline)               │ <= Prompt
│ Esc Interrupt · ^/ Interject · ^G Drop queued · ^C Quit · …       │ <= Footer (bindings)
└───────────────────────────────────────────────────────────────────┘
```

### Turn trace rendering

Runtime events stream into the transcript as the turn executes:
- **LLM responses**: a dimmed one-line preview of thinking/response content (`● <preview>`).
- **Tool calls**: each tool result is paired with its originating call and rendered as `Name(args)` followed by an indented, dimmed result preview. Task-management tool calls (`TaskCreate`, `TaskUpdate`, …) are filtered out of the trace.
- **Task board**: `task_state` events render the task list inline (`✔` completed, `■` in progress, `□` pending), sorted by status.
- **Errors / iteration limits** render as colored system lines.

The oneshot `boscli ask` progress display uses the same formatting rules on stderr.

---

## Chat Selection (Resume Picker)

Typing `/resume` without arguments or pressing **`Ctrl+R`** opens a centered modal listing all chats for selection:

```
┌──────────────────────────────────────────────────────────────┐
│ Resume a chat — Enter to select, Esc to cancel               │
│                                                              │
│ >   3h ago  please do a criticism review of BEP 7… · 12 msg │
│     1d ago  bring the BOS default agent to the level… · 41 msg │
│    12d ago  code review on the staged changes for BEP 4 · 7 msg │
└──────────────────────────────────────────────────────────────┘
```

- The list is requested from the server via the internal `chats` command envelope and is sorted by last activity (most recent first). Each row shows a relative age, the first user prompt preview, the message count, and a `(current)` marker for the active chat.
- `Up`/`Down` move the selection; **`Enter`** resumes the selected chat (`/resume <chat-id>` is sent server-side, which moves the channel cursor); **`Escape`** dismisses the modal without changes.
- On a successful resume, the TUI **clears the viewport and re-renders the resumed chat's transcript** from the full-history payload attached to the command result. User messages and final assistant replies render in full; assistant messages that carry tool calls render as dim one-line previews (matching the live turn trace); tool traffic and summaries are skipped.
- Selecting the already-active chat is a no-op.
- `/resume <chat-id>` with an explicit id (or alias) resumes directly without opening the picker, with the same history rehydration.

Text search and workspace/CWD filtering inside the picker are intentionally not included; the recency-sorted list is sufficient.

---

## Prompt Input

The `#prompt` input is a permanently multiline `TextArea`:
- **Dynamic growth**: starts at one line and grows with content (soft-wrap aware) up to 8 visible lines, then scrolls internally.
- **Keys**:
  - `Enter`: submits the current content (when the autocomplete dropdown is open, `Enter` completes instead).
  - `Ctrl+J` (and `Ctrl+Shift+J` on kitty-protocol terminals): inserts a newline. `Shift+Enter` is not used because legacy terminals cannot distinguish it from `Enter`.
- **Focus capture**: keystrokes anywhere in the app are redirected into the prompt; printable characters are inserted directly. Keyboard focus never moves into the transcript.
- **Autocomplete**: typing `/` offers slash commands; typing `@` offers known actor names (fetched from `/api/actors`) for per-message actor routing.

---

## Prompt Queueing & Turn Interruptions

### 1. Input buffering (queue stack)
- Pressing **`Enter`** while a turn is running queues the input client-side (the gateway rejects mid-turn normal sends): the text is appended to a local buffer and rendered in a stack widget directly above the prompt.
- When the in-flight turn's reply arrives, all queued messages are **merged (joined by blank lines) and sent as a single next turn** with the latest revision.
- **`Ctrl+G`** drops all queued messages without sending them.

### 2. Live turn interjections (interrupt input modal)
- Pressing **`Ctrl+/`** opens a small centered modal input ("Interrupt message — Enter to send, Esc to cancel").
- **`Enter`** submits the text as an `INTERRUPT_MESSAGE` into the active turn and logs it inline as `❯ You (interrupt)`. If no turn is active, the text is sent as a normal message instead.
- **`Escape`** cancels and dismisses the modal without sending.

### 3. Turn cancellation (abort)
- Pressing **`Escape`** while a turn is running sends an `INTERRUPT_ABORT` envelope, drops any queued messages, and logs `⏹ Turn interrupt requested`.

### 4. `Escape` key resolution
1. If a modal (interrupt input or chat picker) is open → the modal handles `Escape` and dismisses itself; app-level interrupt hotkeys are disabled while a modal is open.
2. If a turn is executing → send `INTERRUPT_ABORT`.
3. Otherwise → no-op (a notice that there is no active turn).

---

## Slash Commands Taxonomy

The TUI exposes a minimal, curated set of slash commands:

| Command | Type | Description |
|---|---|---|
| `/help` | Client | Show commands and hotkeys. |
| `/new` | Server | Resets the conversation cursor; returns a fresh `chat_id`. `Ctrl+N` is a convenience shortcut. |
| `/resume [chat-id]` | Server | Without args, opens the chat selection modal. With an id or alias, resumes that chat directly. |
| `/clear` | Client | Empties the transcript viewport (`Ctrl+L` equivalent). |
| `/workdir [path\|unset]` | Client | Show, set, or unset the working directory stamped on outgoing message metadata. |

`chats` remains a **server-side** command (used internally by the resume picker to fetch the chat list, and by non-TUI channels such as Telegram); it is not exposed as a TUI slash command. Gateway restart is performed from the shell (`boscli gateway restart`), not from inside the TUI.

### Keyboard shortcuts reference

| Shortcut | Action |
|---|---|
| `Escape` | Abort the active turn (modal-aware, see priority above). |
| `Ctrl+J` / `Ctrl+Shift+J` | Insert a newline in the prompt. |
| `Ctrl+/` | Open the interrupt-message modal. |
| `Ctrl+G` | Drop all queued messages. |
| `Ctrl+R` | Open the chat selection (resume) modal. |
| `Ctrl+N` | New chat. |
| `Ctrl+L` | Clear the transcript. |
| `Ctrl+C` | Quit the TUI. |

---

## Adaptations to BEP 7

The TUI is a standard WebSocket channel client conforming to the gateway conventions:

### 1. Client-side concurrency handling
- `GatewayClient` tracks the observed `current_revision` from every inbound envelope and the session acknowledgement, and stamps `base_revision` on every outbound send.
- The server preflight (`chat_coordinator.prepare_send`) rejects stale sends with structured SYSTEM events (`stale_chat`, `stale_channel_cursor`, `unobserved_chat`, `future_base_revision`) carrying `missing_messages`.
- **Remaining work (client)**: the TUI currently surfaces these events as raw system lines. Rendering `missing_messages` into the transcript, showing a "chat updated from another client" warning banner, and returning the unsubmitted prompt to the input field are not yet implemented.

### 2. Reconnection resiliency
- If the WebSocket drops, `GatewayClient` reconnects with exponential backoff (0.5s → 10s cap), re-resolving the endpoint from `gateway.state` (so it follows gateway restarts to a new port) and re-authenticating with the API key.
- The status bar polls the connection and shows `● connected` / `○ reconnecting…`.
- The prompt stays editable while disconnected; `send()` waits up to 15s for reconnection before surfacing a "Send failed — reconnecting" warning. (The BEP originally called for disabling the input; keeping it editable was chosen so typed text is never lost.)
- On reconnect, the server's session acknowledgement carries `current_revision` and the `missing_messages` since the channel's cursor. **Remaining work (client)**: the TUI does not yet render this hydration payload into the transcript.

### 3. Prompt submission lifecycle
1. If idle and connected, the TUI renders the user message, sends it with the current `base_revision`, and enters the busy state.
2. If busy, normal `Enter` queues the prompt locally; nothing is sent until the active turn's reply arrives, at which point the queue is merged and sent as one turn.
3. If the active turn is aborted via `Escape`, queued prompts are dropped together with the abort.
4. `Ctrl+/` interjections are never queued; they send `INTERRUPT_MESSAGE` against the active turn immediately.

---

## Technical Details

### Server-side commands over the WebSocket channel

Conversation writes, live turn events, and server-side commands all use the WebSocket channel; read-only metadata (`/api/status`, `/api/actors`) and image upload use authenticated HTTP endpoints. Chat listing and resume are implemented as `content_type="command"` envelopes handled by the actor:

| Command envelope | Result payload |
|---|---|
| `/chats` | `{ "name": "chats", "ok": true, "result": [ChatListEntry…] }` — sorted by `last_activity`, most recent first |
| `/new` | `{ "name": "new", "ok": true, "chat_id": "…" }` |
| `/resume <id>` | `{ "name": "resume", "ok": true, "chat_id": "…" }` — the WS channel attaches the selected chat's **full transcript** as `missing_messages` envelope metadata and moves the channel cursor; the TUI clears its viewport and re-renders it |

`ChatListEntry`:
```json
{
  "chat_id": "abc123",
  "message_count": 12,
  "last_activity": "2026-06-06T14:00:00Z",
  "description": "first user prompt preview"
}
```

A dedicated HTTP read API (`GET /api/chats`, `GET /api/chats/{chat_id}/messages?from_revision=N`) was considered and may still be added later for non-WS clients, but the command-envelope path is the accepted mechanism for the TUI.

### Client

`GatewayClient` (in `src/bos/gateway/client.py`) is the single client implementation used by both `ask` and `tui`. It owns:
- WebSocket connect/reconnect, session acknowledgement, and takeover handling.
- Revision tracking (`current_revision` ingestion, `base_revision` stamping).
- `workdir` metadata stamping (set at construction; mutable via `/workdir` in the TUI).
- HTTP helpers (`list_actors`, `upload_image`).

`LocalClient` and `_run_interactive` are removed; there is no in-process client path.

---

## Remaining Work

Tracked follow-ups that stay within this BEP's intent but are not yet implemented:

1. **Client-side stale/rehydration UX**: render `missing_messages` from stale-send rejections and reconnect session acks into the transcript (resume results already rehydrate), show a warning banner, and preserve the user's unsubmitted prompt.
2. **Token/cost visibility**: surface per-chat token usage in the status bar once the gateway exposes it.
3. **Prompt history**: Up/Down cycling through previously submitted prompts, kept in memory for the current session only (no cross-session persistence).

---

## Revision History

| Date | Change | Intention |
|---|---|---|
| 2026-06-04 | Initial BEP 8 draft | Revamp TUI design and CLI command layout in accordance with BEP 7 |
| 2026-06-06 | Design review pass | Resolve Escape precedence, remove side panel references, fix shortcut conflicts, clarify /resume and /restart |
| 2026-06-06 | Implementation spec pass | Clarify CLI end state, add gateway read APIs, define prompt/reconnect lifecycles, and specify `[cli.tui]` config |
| 2026-06-10 | Implementation reconciliation | Align the doc with the `tui-rewrite` implementation: keep `ask` (oneshot) + `tui` instead of `boscli chat`; drop card-based transcript UI, feed navigation/copy, and `[cli.tui]` config; replace the HTTP read API with command envelopes; resume picker modal via `/resume`//`Ctrl+R`; remove `/chats` and `/restart` from the TUI; document actual keybindings (`Ctrl+J`, `Ctrl+/`, `Ctrl+G`) and merged queue-flush semantics; list remaining work explicitly |
