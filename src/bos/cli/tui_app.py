"""Textual Chat Application — connects to a running BOS gateway.

This TUI is a pure external client. It communicates with the gateway over
WebSocket through ``GatewayClient``. It never imports or references the
agent, harness, or actor directly.

Slash commands that need server-side data (``/chats``, ``/resume``, etc.)
send a ``content_type="command"`` envelope and wait for a ``command_result``
response from the gateway channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Footer, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option
from textual_autocomplete import AutoComplete, DropdownItem, TargetState

from bos.core.actor import MessageType
from bos.core.agent import MessageContent, TurnEvent, content_to_plain_text
from bos.gateway import WS_TAKEOVER_CLOSE_REASON
from bos.gateway.client import GatewayClient

logger = logging.getLogger(__name__)


# ── Textual messages ───────────────────────────────────────────


class TurnEventMessage(Message):
    """Structured runtime event forwarded from the gateway channel."""

    def __init__(self, event: TurnEvent) -> None:
        super().__init__()
        self.event = event


class AgentReplyEvent(Message):
    """Final reply envelope from the agent."""

    def __init__(self, content: str, chat_id: str | None = None) -> None:
        super().__init__()
        self.content = content
        self.chat_id = chat_id


class CommandResultEvent(Message):
    """Result of a slash command executed on the server side."""

    def __init__(self, name: str, data: Any, metadata: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.name = name
        self.data = data
        self.metadata = metadata or {}


class SystemEvent(Message):
    """System event emitted by the channel infrastructure."""

    def __init__(self, content: str, chat_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.content = content
        self.chat_id = chat_id
        self.metadata = metadata or {}


SLASH_COMMANDS = [
    "/help",
    "/new",
    "/resume",
    "/clear",
    "/plan",
    "/workdir",
]


def _indent(text: str) -> str:
    """Indent every line of a (possibly multiline) message for log display."""
    return "\n".join(f"  {line}" for line in text.splitlines()) or "  "


def _fmt_tokens(count: int) -> str:
    """Format a token count compactly (842, 12.4k, 1.2M)."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


def _message_text(content: Any) -> str:
    """Extract display text from an LLM message content (string or parts list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _compose_send_content(text: str, attachments: list[dict[str, Any]]) -> MessageContent:
    if not attachments:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}, *attachments]
    return cast("MessageContent", parts)


def _render_attachment_tags(names: list[str]) -> str:
    if not names:
        return ""
    return "   ".join(f"📎 {name}" for name in names)


def _resolve_typed_path(raw: str) -> tuple[str, str | None]:
    candidate = raw.strip()
    if not candidate:
        return ("invalid", None)
    path = Path(candidate).expanduser()
    if path.is_file():
        return ("dismiss", str(path.resolve()))
    if path.is_dir():
        return ("reroot", str(path.resolve()))
    return ("invalid", None)


def _relative_time(iso: str | None) -> str:
    """Render an ISO timestamp as a short relative age (e.g. '3h ago')."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    seconds = (datetime.now(dt.tzinfo) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


class PromptHistory:
    """In-memory prompt history for a single TUI session (not persisted)."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._pos: int | None = None
        self._draft = ""

    def record(self, text: str) -> None:
        """Store a submitted prompt and reset the navigation cursor."""
        text = text.strip()
        if text and (not self._items or self._items[-1] != text):
            self._items.append(text)
        self._pos = None
        self._draft = ""

    def previous(self, current: str) -> str | None:
        """Step back in history; saves *current* as the draft on first step."""
        if not self._items:
            return None
        if self._pos is None:
            self._draft = current
            self._pos = len(self._items) - 1
        elif self._pos > 0:
            self._pos -= 1
        return self._items[self._pos]

    def next(self) -> str | None:
        """Step forward; returns the saved draft when stepping past the end."""
        if self._pos is None:
            return None
        if self._pos < len(self._items) - 1:
            self._pos += 1
            return self._items[self._pos]
        self._pos = None
        return self._draft


class PromptInput(TextArea):
    """Multiline prompt: Enter submits, Ctrl+J inserts a newline."""

    MAX_VISIBLE_LINES = 8

    class Submitted(Message):
        def __init__(self, prompt_input: PromptInput, value: str) -> None:
            super().__init__()
            self.prompt_input = prompt_input
            self.value = value

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Not named `history`: TextArea already owns an undo/redo EditHistory
        # under that attribute.
        self.prompt_history = PromptHistory()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            # With the autocomplete dropdown open, Enter completes instead of
            # submitting (AutoComplete sees the key via the message signal,
            # which fires after this dispatch even for stopped events).
            if not self._autocomplete_open():
                self.prompt_history.record(self.text)
                self.post_message(self.Submitted(self, self.text))
            return
        # Ctrl+J works on every terminal (legacy ones transmit it as \n).
        # Ctrl+Shift+J is distinguishable only on kitty-protocol terminals;
        # legacy ones drop shift and deliver plain ctrl+j, same action.
        if event.key in ("ctrl+j", "ctrl+shift+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        # Up/Down on the edge lines cycle prompt history; inside a multiline
        # draft they keep their normal cursor-movement behavior.
        if event.key == "up" and not self._autocomplete_open() and self.cursor_location[0] == 0:
            if (recalled := self.prompt_history.previous(self.text)) is not None:
                event.stop()
                event.prevent_default()
                self._replace_text(recalled)
                return
        if (
            event.key == "down"
            and not self._autocomplete_open()
            and self.cursor_location[0] == self.document.line_count - 1
        ):
            if (recalled := self.prompt_history.next()) is not None:
                event.stop()
                event.prevent_default()
                self._replace_text(recalled)
                return
        await super()._on_key(event)

    def _replace_text(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)
        self._fit_height()

    def _autocomplete_open(self) -> bool:
        for ac in self.screen.query(AutoComplete):
            try:
                if ac.display and ac.option_list.option_count:
                    return True
            except Exception:
                continue
        return False

    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._fit_height()

    def _fit_height(self) -> None:
        """Grow with content up to MAX_VISIBLE_LINES, accounting for soft wrap."""
        try:
            height = self.wrapped_document.height
        except Exception:
            height = self.document.line_count
        self.styles.height = max(1, min(height, self.MAX_VISIBLE_LINES))


class SlashAutoComplete(AutoComplete):
    """AutoComplete adapted to a TextArea target; activates for slash commands and @mentions."""

    @property
    def target(self) -> PromptInput:  # type: ignore[override]
        if isinstance(self._target, PromptInput):
            return self._target
        target = self.screen.query_one(self._target)  # pyright: ignore[reportArgumentType, reportCallIssue]
        assert isinstance(target, PromptInput)
        return target

    def _get_target_state(self) -> TargetState:
        target = self.target
        return TargetState(
            text=target.text,
            cursor_position=target.document.get_index_from_location(target.cursor_location),  # pyright: ignore[reportAttributeAccessIssue]
        )

    def _listen_to_messages(self, event: events.Event) -> None:
        super()._listen_to_messages(event)
        if isinstance(event, TextArea.Changed):
            self._handle_target_update()

    def should_show_dropdown(self, search_string: str) -> bool:
        if not (search_string.startswith("/") or search_string.startswith("@")):
            return False
        return super().should_show_dropdown(search_string)

    def apply_completion(self, value: str, state: TargetState) -> None:
        """Replace the prompt content with the completion (TextArea-aware)."""
        if value.startswith("@"):
            value = value + " "
        target = self.target
        with self.prevent(TextArea.Changed):
            target.text = value
            target.move_cursor(target.document.end)
        target._fit_height()
        new_state = self._get_target_state()
        self._rebuild_options(new_state, self.get_search_string(new_state))


class InterruptModal(ModalScreen[str | None]):
    """Modal prompt for an interrupt message: Enter submits, Escape cancels."""

    DEFAULT_CSS = """
    InterruptModal {
        align: center middle;
    }

    #interrupt-dialog {
        width: 60%;
        max-width: 80;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #interrupt-title {
        color: $text-muted;
        padding-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="interrupt-dialog"):
            yield Static("Interrupt message — Enter to send, Esc to cancel", id="interrupt-title")
            yield Input(placeholder="Inject a message into the current turn…", id="interrupt-input")

    def on_mount(self) -> None:
        self.query_one("#interrupt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChatPickerModal(ModalScreen[str | None]):
    """Modal chat list for /resume: Enter selects, Escape cancels."""

    DEFAULT_CSS = """
    ChatPickerModal {
        align: center middle;
    }

    #chat-picker {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #chat-picker-title {
        color: $text-muted;
        padding-bottom: 1;
    }

    #chat-picker-list {
        height: auto;
        max-height: 20;
        border: none;
        background: transparent;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, chats: list[dict[str, Any]], current_chat_id: str | None = None) -> None:
        super().__init__()
        self._chats = chats
        self._current_chat_id = current_chat_id

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-picker"):
            yield Static("Resume a chat — Enter to select, Esc to cancel", id="chat-picker-title")
            yield OptionList(
                *[Option(self._render_chat(chat), id=chat.get("chat_id")) for chat in self._chats],
                id="chat-picker-list",
            )

    def _render_chat(self, chat: dict[str, Any]) -> Text:
        age = _relative_time(chat.get("last_activity"))
        description = str(chat.get("description") or "").replace("\n", " ").strip() or "(empty chat)"
        count = chat.get("message_count")
        row = Text()
        row.append(f"{age:>10}  ", style="dim")
        row.append(description)
        if count:
            row.append(f"  · {count} msg", style="dim")
        if chat.get("chat_id") == self._current_chat_id:
            row.append("  (current)", style="dim italic")
        return row

    def on_mount(self) -> None:
        self.query_one("#chat-picker-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilePickerModal(ModalScreen[str | None]):
    """File browser for attaching a file. Enter on a file selects it; Esc cancels.

    A path Input at the top retargets the tree (directory) or selects directly (file).
    """

    DEFAULT_CSS = """
    FilePickerModal {
        align: center middle;
    }

    #file-picker {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #file-picker-title {
        color: $text-muted;
        padding-bottom: 1;
    }

    #file-tree {
        height: auto;
        max-height: 20;
        border: none;
        background: transparent;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="file-picker"):
            yield Static(
                "Attach a file — browse + Enter to select, type a path, Esc to cancel",
                id="file-picker-title",
            )
            yield Input(placeholder="Path (file selects, directory re-roots)…", id="file-path")
            yield DirectoryTree(str(Path.cwd()), id="file-tree")

    def on_mount(self) -> None:
        self.query_one("#file-tree", DirectoryTree).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(str(event.path))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        action, value = _resolve_typed_path(event.value)
        if action == "dismiss":
            self.dismiss(value)
        elif action == "reroot" and value is not None:
            self.query_one("#file-tree", DirectoryTree).path = value
            self.query_one("#file-path", Input).value = ""
        else:
            self.query_one("#file-path", Input).value = ""
            self.notify("No such file or directory.", severity="warning")

    def action_cancel(self) -> None:
        self.dismiss(None)


_PLAN_LIST_SECTIONS = [
    ("constraints", "Constraints"),
    ("current_context", "Current context"),
    ("risks", "Risks"),
    ("non_goals", "Non-goals"),
    ("breakdown", "Breakdown"),
    ("verification", "Verification"),
    ("open_questions", "Open questions"),
]


def _render_plan_text(plan: dict[str, Any]) -> Text:
    """Render a full plan payload as plain Rich text (no markup injection)."""
    text = Text()

    def scalar(label: str, value: Any) -> None:
        if not value:
            return
        text.append(f"{label}\n", style="bold")
        text.append(f"  {value}\n\n")

    scalar("Objective", plan.get("objective"))
    scalar("User value", plan.get("user_value"))
    scalar("Appetite", plan.get("appetite"))
    scalar("Shaped solution", plan.get("shaped_solution"))
    for key, label in _PLAN_LIST_SECTIONS:
        items = plan.get(key) or []
        if not items:
            continue
        text.append(f"{label}\n", style="bold")
        for item in items:
            text.append(f"  • {item}\n")
        text.append("\n")
    return text


class PlanModal(ModalScreen[None]):
    """Full view of the current structured plan: Escape closes."""

    DEFAULT_CSS = """
    PlanModal {
        align: center middle;
    }

    #plan-view {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }

    #plan-title {
        color: $text-muted;
        padding-bottom: 1;
    }

    #plan-body {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self, plan: dict[str, Any]) -> None:
        super().__init__()
        self._plan = plan

    def compose(self) -> ComposeResult:
        status = self._plan.get("status", "?")
        with Vertical(id="plan-view"):
            yield Static(f"Current plan ({status}) — Esc to close", id="plan-title")
            yield Static(_render_plan_text(self._plan), id="plan-body")

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── ChatApp ────────────────────────────────────────────────────


class ChatApp(App):
    """Full-screen agent chat — channel-mode only.

    Communicates with the gateway via ``GatewayClient``.
    """

    TITLE = "boscli tui"
    CSS = """
    #chat {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 1 1;
    }

    #queued {
        height: auto;
        max-height: 8;
        padding: 0 2;
        background: $boost;
        color: $text-muted;
        display: none;
    }

    #status-bar {
        height: 1;
        background: $primary-background;
        color: $text-muted;
        padding: 0 2;
    }

    #prompt {
        height: 1;
        padding: 0 1;
        border: none;
        background: transparent;
    }

    #attachments {
        height: auto;
        padding: 0 1;
        color: $accent;
        background: transparent;
    }

    #prompt:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("escape", "interrupt_turn", "Interrupt", show=True, priority=True),
        Binding("ctrl+underscore", "interrupt_message", "Interject", show=True, priority=True, key_display="ctrl+/"),
        Binding("ctrl+slash", "interrupt_message", "Interject", show=False, priority=True),
        Binding("ctrl+g", "cancel_queued", "Drop queued", show=True, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+l", "clear_log", "Clear", show=True),
        Binding("ctrl+n", "reset_chat", "New Chat", show=True),
        Binding("ctrl+r", "resume_chat", "Resume", show=True),
        Binding("ctrl+p", "show_plan", "Plan", show=True),
        Binding("ctrl+o", "attach_file", "Attach", show=True),
    ]

    _TASK_TOOL_NAMES = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}
    _PLAN_TOOL_NAMES = {"PlanCreate", "PlanUpdate", "PlanGet", "PlanClear"}

    def __init__(
        self,
        client: GatewayClient,
    ) -> None:
        super().__init__()
        self._client = client
        if not client.chat_id:
            raise ValueError("Gateway client must be connected and session-acknowledged before launching ChatApp.")
        self._chat_id = client.chat_id
        self._busy = False
        self._buffer: list[str] = []
        self._pending_attachments: list[dict[str, Any]] = []
        self._attachment_names: list[str] = []
        self._conn_status: str = "connected"
        self._pending_tool_calls: list[tuple[str, str]] = []
        self._known_actors: list[str] = []
        self._awaiting_chat_list = False
        self._session_count = 0
        self._last_sent_text = ""
        self._context_tokens = 0
        self._session_tokens = 0
        self._plan_by_chat: dict[str, dict[str, Any]] = {}

    # ── compose ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield RichLog(
            id="chat",
            highlight=False,
            markup=True,
            wrap=True,
            auto_scroll=True,
        )
        yield Static(self._status_text(), id="status-bar")
        yield Static(id="queued")
        yield PromptInput(
            placeholder="Send a message… (Enter to send, Ctrl+J for newline)",
            id="prompt",
            show_line_numbers=False,
            compact=True,
        )
        attachments = Static(id="attachments")
        attachments.display = False
        yield attachments
        yield SlashAutoComplete("#prompt", candidates=self._get_candidates)
        yield Footer()

    # ── lifecycle ──────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._update_status()

        # Start reply polling worker; the queued session ack envelope writes
        # the welcome banner and hydrates the transcript.
        self._poll_task = asyncio.create_task(self._poll_replies())
        self._conn_poll_task = asyncio.create_task(self._poll_connection_status())

        # Fetch actor list for @mention autocomplete
        try:
            actors = await self._client.list_actors()
            self._known_actors = list(actors.keys())
        except Exception:
            logger.debug("Failed to fetch actor list", exc_info=True)

        self.query_one("#prompt", PromptInput).focus()

    async def _poll_replies(self) -> None:
        """Background task: await envelopes from the channel."""
        while True:
            try:
                env = await self._client.receive()
                if env.content_type == MessageType.COMMAND_RESULT:
                    # Server-side slash command response
                    try:
                        data = json.loads(env.content) if isinstance(env.content, str) else env.content
                    except json.JSONDecodeError:
                        data = env.content
                    cmd_name = data.get("name", "?") if isinstance(data, dict) else "?"
                    self.post_message(CommandResultEvent(cmd_name, data, env.metadata))
                elif env.content_type == MessageType.TURN_EVENT:
                    try:
                        data = json.loads(env.content) if isinstance(env.content, str) else {}
                    except json.JSONDecodeError:
                        data = {}
                    if data:
                        self.post_message(TurnEventMessage(TurnEvent.from_payload(data)))
                elif env.content_type == MessageType.ECHO:
                    # User input from another channel — display it
                    log = self.query_one("#chat", RichLog)
                    log.write(f"\n[bold dim cyan]❯ User ({env.sender})[/]")
                    log.write(f"  {env.content}")
                elif env.content_type == MessageType.SYSTEM:
                    self.post_message(SystemEvent(content_to_plain_text(env.content), env.chat_id, env.metadata))
                else:
                    # Normal reply
                    self.post_message(AgentReplyEvent(content_to_plain_text(env.content), env.chat_id))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Poll error", exc_info=True)

    # ── event handlers ────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable app-level interrupt hotkeys while a modal dialog is open."""
        if action in ("interrupt_turn", "interrupt_message", "resume_chat") and isinstance(self.screen, ModalScreen):
            return False
        if action == "cancel_queued" and not self._buffer:
            return None  # hide from the footer while nothing is queued
        return True

    def on_key(self, event: events.Key) -> None:
        """Redirect keystrokes to the prompt unless it already has focus."""
        if isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one("#prompt", PromptInput)
        if self.focused is not prompt:
            prompt.focus()
            # Forward printable characters into the input
            if event.character and event.is_printable:
                prompt.insert(event.character)
                event.prevent_default()

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.prompt_input.clear()

        # Handle slash commands
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        if self._busy:
            # Queue locally; the gateway rejects messages while a turn is in
            # flight, so the queue is flushed when the reply arrives.
            self._buffer.append(text)
            self._refresh_queued()
            self._update_status()
            return

        # Write user message
        log = self.query_one("#chat", RichLog)
        log.write("\n[bold cyan]❯ You[/]")
        log.write(_indent(text))

        # Send to actor
        self._busy = True
        self._last_sent_text = text
        self._update_status()
        try:
            await self._client.send(_compose_send_content(text, self._pending_attachments), chat_id=self._chat_id)
            self._clear_attachments()
        except Exception as exc:
            self._busy = False
            self._pending_tool_calls.clear()
            self._update_status()
            self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")

    async def on_turn_event_message(self, message: TurnEventMessage) -> None:
        """Handle structured runtime events from the gateway channel."""
        event = message.event
        log = self.query_one("#chat", RichLog)

        if event.event_type == "llm" and event.detail == "thinking":
            pass

        elif event.event_type == "llm" and event.detail in ("tool_calls", "response_ready"):
            self._ingest_usage((event.metadata or {}).get("usage"))
            # Unified LLM response event — show thinking content if present
            content = event.content or ""
            if content:
                preview = content[:240].replace("\n", " ")
                log.write(f"\n[dim italic]● {preview}[/]")

        elif event.event_type == "tool" and event.detail == "tool_call":
            name = event.tool_name or "?"
            if name in self._TASK_TOOL_NAMES or name in self._PLAN_TOOL_NAMES:
                return
            args_str = ""
            if event.content:
                try:
                    args = json.loads(event.content) if isinstance(event.content, str) else event.content
                    if isinstance(args, dict):
                        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                except (json.JSONDecodeError, TypeError):
                    pass
            self._pending_tool_calls.append((name, args_str))

        elif event.event_type == "tool" and event.detail == "tool_result":
            name = event.tool_name or "?"
            if name in self._TASK_TOOL_NAMES or name in self._PLAN_TOOL_NAMES:
                return
            preview = str(event.content or "")[:240].replace("\n", " ")
            if self._pending_tool_calls:
                call_name, call_args = self._pending_tool_calls.pop(0)
                log.write(f"  [bold]{call_name}[/]({call_args})")
                log.write(f"   └ [dim]{preview}[/]")
            else:
                log.write(f"  {name}: [dim]{preview}[/]")

        elif event.event_type == "task" and event.detail == "task_state":
            tasks = (event.metadata or {}).get("tasks", [])
            if tasks:
                order = {"completed": 0, "in_progress": 1, "pending": 2}
                ordered = sorted(tasks, key=lambda t: order.get(t.get("status"), 3))
                lines = ["[bold]• Tasks[/]"]
                for i, t in enumerate(ordered):
                    prefix = "└" if i == 0 else " "
                    subject = t.get("subject", "")
                    if t.get("status") == "completed":
                        lines.append(f"  {prefix} [dim][s]✔ {subject}[/s][/dim]")
                    elif t.get("status") == "in_progress":
                        lines.append(f"  {prefix} [bold]■ {subject}[/]")
                    else:
                        lines.append(f"  {prefix} □ {subject}")
                log.write("\n" + "\n".join(lines))

        elif event.event_type == "plan" and event.detail == "plan_state":
            chat_id = event.chat_id or self._chat_id
            plan = (event.metadata or {}).get("plan")
            if plan:
                self._plan_by_chat[chat_id] = plan
            else:
                self._plan_by_chat.pop(chat_id, None)
            if chat_id == self._chat_id:
                self._write_plan_card(log, plan)

        elif event.detail == "max_iteration":
            log.write("[yellow]  max iterations reached[/]")

        elif event.detail == "error":
            log.write(f"[red]  error: {event.content or 'unknown error'}[/]")

    def _write_plan_card(self, log: RichLog, plan: dict[str, Any] | None) -> None:
        """Write a compact plan summary to the chat log; full view via Ctrl+P."""
        if not plan:
            log.write("\n[bold]◆ Plan[/] [dim]cleared[/]")
            return
        status = plan.get("status", "?")
        card = Text("\n")
        card.append("◆ Plan", style="bold")
        card.append(f" ({status}) · Ctrl+P for details", style="dim")
        card.append(f"\n└ {plan.get('objective', '')}")
        if status != "in_progress":
            for step in plan.get("breakdown") or []:
                card.append(f"\n  □ {step}")
        for question in plan.get("open_questions") or []:
            card.append(f"\n  ? {question}", style="yellow")
        log.write(card)

    async def on_agent_reply_event(self, event: AgentReplyEvent) -> None:
        """Handle the final reply from the actor."""
        self._last_sent_text = ""
        log = self.query_one("#chat", RichLog)
        content = event.content or "(no response)"

        # Visual mark for replies from a non-current chat
        is_current = not event.chat_id or event.chat_id == self._chat_id
        chat_mark = "" if is_current else f" [dim](chat {(event.chat_id or '')[:8]}…)[/]"
        log.write(f"\n[bold green]▸ Assistant{chat_mark}[/]")
        try:
            md = Markdown(content)
            log.write(md)
        except Exception:
            log.write(f"  {content}")

        if self._buffer:
            # Flush the queued messages as the next turn.
            merged = "\n\n".join(self._buffer)
            self._buffer.clear()
            self._refresh_queued()
            self._pending_tool_calls.clear()

            log.write("\n[bold cyan]❯ You[/]")
            log.write(_indent(merged))

            self._busy = True
            self._last_sent_text = merged
            try:
                await self._client.send(_compose_send_content(merged, self._pending_attachments), chat_id=self._chat_id)
                self._clear_attachments()
            except Exception as exc:
                self._busy = False
                self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")
        else:
            self._busy = False
            self._pending_tool_calls.clear()

        self._update_status()
        self.query_one("#prompt", PromptInput).focus()

    async def on_command_result_event(self, event: CommandResultEvent) -> None:
        """Handle a slash command result from the server."""
        data = event.data
        if isinstance(data, dict):
            if event.name == "chats" and self._awaiting_chat_list:
                self._awaiting_chat_list = False
                if data.get("ok"):
                    self._open_chat_picker(data.get("result") or [])
                    return
            if event.name in {"new", "resume"} and data.get("ok"):
                chat_id = data.get("chat_id")
                if isinstance(chat_id, str):
                    self._set_chat_id(chat_id)
                if event.name == "resume":
                    # Replace the viewport with the resumed chat's transcript.
                    log = self.query_one("#chat", RichLog)
                    log.clear()
                    self._render_history(event.metadata.get("missing_messages") or [])
            result = data.get("result")
            error = data.get("error")
            if error:
                self._write_system(f"[red]Error: {error}[/]")
            elif result is not None:
                if isinstance(result, str):
                    self._write_system(f"[dim]{result}[/]")
                else:
                    self._write_system(f"[dim]{json.dumps(result, indent=2, default=str)}[/]")
            else:
                self._write_system(f"[dim]{json.dumps(data, indent=2, default=str)}[/]")
        else:
            self._write_system(f"[dim]{data}[/]")

    async def on_system_event(self, event: SystemEvent) -> None:
        """Handle infrastructure-level events from the channel layer."""
        if event.content == WS_TAKEOVER_CLOSE_REASON:
            self._write_system(f"[yellow]{event.content} Exiting.[/]")
            self.exit()
            return
        meta_event = event.metadata.get("event")
        if meta_event in ("turn_aborted", "turn_error"):
            self._busy = False
            self._pending_tool_calls.clear()
            self._update_status()
            if meta_event == "turn_aborted":
                self._write_system("[yellow]■ Turn aborted.[/]")
            else:
                self._write_system(f"[red]✗ {event.content or 'Turn failed.'}[/]")
            return
        if meta_event == "session":
            self._handle_session_event(event)
            return
        if meta_event == "stale_chat":
            self._handle_stale_rejection(event.metadata.get("payload") or {})
            return
        if meta_event == "active_turn":
            self._handle_active_turn_rejection()
            return
        if event.chat_id:
            self._set_chat_id(event.chat_id)
        self._write_system(f"[green]{event.content}[/]")

    def _handle_stale_rejection(self, payload: dict[str, Any]) -> None:
        """The send was rejected: the chat moved on under another client."""
        self._busy = False
        self._pending_tool_calls.clear()
        missing = payload.get("missing_messages") or []
        if missing:
            self._write_system("\n[yellow]⚠ This chat was updated from another client — missed messages:[/]")
            self._render_history(missing)
        restored = self._restore_last_prompt()
        notice = "Review the messages above and resubmit"
        if restored:
            notice += " — your text is back in the prompt"
        self._write_system(f"\n[yellow]⚠ Send rejected (stale revision). {notice}.[/]")
        self._update_status()

    def _handle_active_turn_rejection(self) -> None:
        """The send was rejected: another client's turn is in flight."""
        self._busy = False
        self._pending_tool_calls.clear()
        restored = self._restore_last_prompt()
        notice = "⚠ Another turn is in progress — message not sent"
        if restored:
            notice += "; your text is back in the prompt"
        self._write_system(f"[yellow]{notice}.[/]")
        self._update_status()

    def _restore_last_prompt(self) -> bool:
        """Put the last rejected text back into the prompt; True if restored."""
        text, self._last_sent_text = self._last_sent_text, ""
        if not text:
            return False
        prompt = self.query_one("#prompt", PromptInput)
        if prompt.text.strip():
            return False  # don't clobber text the user typed meanwhile
        prompt._replace_text(text)
        return True

    def _handle_session_event(self, event: SystemEvent) -> None:
        """Hydrate the viewport from a session acknowledgement (connect/reconnect)."""
        self._session_count += 1
        if event.chat_id:
            self._set_chat_id(event.chat_id)
        if self._conn_status != "connected":
            self._conn_status = "connected"
            self._update_status()
        log = self.query_one("#chat", RichLog)
        log.clear()
        self._write_banner(log)
        self._render_history(event.metadata.get("missing_messages") or [])
        if self._session_count > 1:
            self._write_system("\n[green]↻ Reconnected — transcript refreshed.[/]")
        # Adopt the server's view of the in-flight turn so a freshly started
        # client can interrupt a turn it did not send (e.g. after Ctrl+C).
        self._busy = bool(event.metadata.get("active_turn"))
        if self._busy:
            self._write_system("\n[dim]⏳ A turn is in progress — Esc to abort.[/]")
        else:
            self._pending_tool_calls.clear()
        self._update_status()

    @staticmethod
    def _write_banner(log: RichLog) -> None:
        log.write("[bold $primary]Agent CLI ready.[/]")
        log.write(
            "[dim]Type /help for commands · Escape to abort · Ctrl+J for newline"
            " · Ctrl+/ to interject · Ctrl+C to quit[/]\n"
        )

    # ── slash commands ────────────────────────────────────────

    async def _handle_slash_command(self, text: str) -> None:
        stripped = text.strip()
        cmd, sep, rest = stripped.partition(" ")
        normalized_cmd = cmd.lower()
        command_text = normalized_cmd + (sep + rest if sep else "")

        if normalized_cmd == "/help":
            self._write_system(
                "[bold]Commands:[/]\n"
                "  /help     — show this help\n"
                "  /resume   — pick a chat to resume, or /resume <chat-id>\n"
                "  /new      — start a new chat\n"
                "  /clear    — clear the log\n"
                "  /plan     — show the current plan for this chat\n"
                "  /workdir  — show, set (/workdir <path>), or unset (/workdir unset)\n"
                "              the working directory stamped on outgoing messages\n"
                "\n"
                "[bold]Hot keys:[/]\n"
                "  Escape      — abort the current turn\n"
                "  Up / Down   — cycle prompt history (cursor on first/last line)\n"
                "  Ctrl+J / Ctrl+Shift+J — insert a newline in the prompt\n"
                "  Ctrl+/      — inject a message into the current turn\n"
                "  Ctrl+G      — drop queued messages without sending\n"
                "  Ctrl+C      — quit\n"
                "  Ctrl+L      — clear the log\n"
                "  Ctrl+N      — start a new chat\n"
                "  Ctrl+R      — pick a chat to resume\n"
                "  Ctrl+P      — show the current plan"
            )

        elif normalized_cmd == "/new":
            await self._send_command(command_text)

        elif normalized_cmd == "/clear":
            self.query_one("#chat", RichLog).clear()

        elif normalized_cmd == "/plan":
            self.action_show_plan()

        elif normalized_cmd == "/workdir":
            self._handle_workdir_command(rest.strip())

        elif normalized_cmd == "/resume":
            if rest.strip():
                # Delegate to the server via a command envelope
                await self._send_command(command_text)
            else:
                await self._request_chat_picker()

        else:
            self._write_system(f"[yellow]Unknown command: {normalized_cmd}[/]")

    def _handle_workdir_command(self, arg: str) -> None:
        """Show, set, or unset the workdir stamped on outgoing messages."""
        if not arg:
            current = self._client.workdir
            if current:
                self._write_system(f"[dim]workdir: {current}[/]")
            else:
                self._write_system("[dim]workdir: (unset — the gateway workspace is used)[/]")
            return
        if arg.lower() in ("unset", "none", "off"):
            self._client.update_workdir(None)
            self._write_system("[dim]workdir unset — the gateway workspace is used.[/]")
            return
        path = Path(arg).expanduser()
        if path.is_dir():
            resolved = str(path.resolve())
            self._client.update_workdir(resolved)
            self._write_system(f"[dim]workdir set: {resolved}[/]")
        else:
            # The gateway may run on another filesystem (e.g. Docker); set anyway.
            self._client.update_workdir(str(path))
            self._write_system(f"[yellow]workdir set: {path} (not found on this machine)[/]")

    async def _send_command(self, command_text: str) -> None:
        """Send a slash command to the channel server for execution."""
        label = command_text.split(None, 1)[0]
        try:
            await self._client.send(
                command_text,
                content_type=MessageType.COMMAND,
                chat_id=self._chat_id,
            )
        except Exception as exc:
            self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")
            return
        self._write_system(f"[dim]  ⏳ {label}…[/]")

    # ── actions ────────────────────────────────────────────────

    def action_clear_log(self) -> None:
        self.query_one("#chat", RichLog).clear()

    def action_show_plan(self) -> None:
        plan = self._plan_by_chat.get(self._chat_id)
        if plan:
            self.push_screen(PlanModal(plan))
        else:
            self._write_system("[dim]No plan for this chat yet.[/]")

    def action_attach_file(self) -> None:
        self.push_screen(FilePickerModal(), callback=self._on_file_picked)

    async def _on_file_picked(self, path: str | None) -> None:
        if path is None:
            return
        try:
            part = await self._client.upload_attachment(path)
        except Exception as exc:
            self._write_system(f"[yellow]⚠ Couldn't attach {Path(path).name}: {exc}[/]")
            return
        self._pending_attachments.append(part)
        self._attachment_names.append(Path(path).name)
        self._update_status()

    def action_reset_chat(self) -> None:
        asyncio.create_task(self._send_command("/new"))

    def action_resume_chat(self) -> None:
        asyncio.create_task(self._request_chat_picker())

    async def _request_chat_picker(self) -> None:
        """Ask the server for the chat list; the picker opens on the result."""
        self._awaiting_chat_list = True
        await self._send_command("/chats")

    def _open_chat_picker(self, chats: list[dict[str, Any]]) -> None:
        if not chats:
            self._write_system("[dim]No chats to resume.[/]")
            return

        def _on_dismiss(chat_id: str | None) -> None:
            if chat_id and chat_id != self._chat_id:
                asyncio.create_task(self._send_command(f"/resume {chat_id}"))

        self.push_screen(ChatPickerModal(chats, current_chat_id=self._chat_id), callback=_on_dismiss)

    async def action_interrupt_turn(self) -> None:
        """Abort the in-flight turn for the current chat."""
        if not self._busy:
            self._write_system("[dim]No active turn to interrupt.[/]")
            return

        try:
            await self._client.send(
                "",
                content_type=MessageType.INTERRUPT_ABORT,
                chat_id=self._chat_id,
                metadata={"reason": "user_hotkey"},
            )
        except Exception as exc:
            self._write_system(f"[yellow]⚠ Interrupt failed — reconnecting: {exc}[/]")
            return

        self._buffer.clear()
        self._refresh_queued()
        self._busy = False
        self._update_status()
        self._write_system("[yellow]⏹ Turn interrupt requested.[/]")
        self.query_one("#prompt", PromptInput).focus()

    def action_cancel_queued(self) -> None:
        """Drop queued messages without sending them."""
        if not self._buffer:
            return
        count = len(self._buffer)
        self._buffer.clear()
        self._refresh_queued()
        self._update_status()
        plural = "s" if count > 1 else ""
        self._write_system(f"[yellow]✗ Dropped {count} queued message{plural}.[/]")

    def action_interrupt_message(self) -> None:
        """Open a modal dialog to compose an interrupt message for the ongoing turn."""

        def _on_dismiss(text: str | None) -> None:
            if text:
                asyncio.create_task(self._send_interrupt_message(text))

        self.push_screen(InterruptModal(), callback=_on_dismiss)

    async def _send_interrupt_message(self, text: str) -> None:
        """Inject a message into the ongoing turn (or send normally when idle)."""
        log = self.query_one("#chat", RichLog)

        if self._busy:
            try:
                await self._client.send(
                    text,
                    content_type=MessageType.INTERRUPT_MESSAGE,
                    chat_id=self._chat_id,
                )
            except Exception as exc:
                self._write_system(f"[yellow]⚠ Interrupt message failed — reconnecting: {exc}[/]")
                return

            log.write("\n[bold yellow]❯ You (interrupt)[/]")
            log.write(_indent(text))
            self._write_system("[yellow]⏎ Interrupt message sent.[/]")
        else:
            log.write("\n[bold cyan]❯ You[/]")
            log.write(_indent(text))
            self._busy = True
            self._last_sent_text = text
            self._update_status()
            try:
                await self._client.send(text, chat_id=self._chat_id)
            except Exception as exc:
                self._busy = False
                self._update_status()
                self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")

    # ── autocomplete ───────────────────────────────────────────

    def _get_candidates(self, state: Any) -> list[DropdownItem]:
        """Return candidate completions based on the current input prefix."""
        text = state.text[: state.cursor_position]
        if text.startswith("/"):
            return [DropdownItem(cmd) for cmd in SLASH_COMMANDS]
        if text.startswith("@"):
            return [DropdownItem(f"@{a}") for a in self._known_actors]
        return []

    # ── helpers ────────────────────────────────────────────────

    def _write_system(self, text: str) -> None:
        self.query_one("#chat", RichLog).write(text)

    def _render_history(self, messages: list[dict[str, Any]]) -> None:
        """Render persisted chat messages (a hydration payload) into the log.

        Only user and assistant text is shown; tool traffic and summaries are
        skipped. Assistant messages that carry tool calls are intermediate
        thinking steps and render as dim previews, like the live turn trace.
        """
        log = self.query_one("#chat", RichLog)
        for message in messages:
            if not isinstance(message, dict) or message.get("is_summary"):
                continue
            llm_message = message.get("llm_message") or {}
            role = llm_message.get("role")
            text = _message_text(llm_message.get("content"))
            if not text:
                continue
            if role == "user":
                log.write("\n[bold cyan]❯ You[/]")
                log.write(_indent(text))
            elif role == "assistant":
                if llm_message.get("tool_calls"):
                    preview = text[:240].replace("\n", " ")
                    log.write(f"\n[dim italic]● {preview}[/]")
                    continue
                log.write("\n[bold green]▸ Assistant[/]")
                try:
                    log.write(Markdown(text))
                except Exception:
                    log.write(f"  {text}")

    def _refresh_queued(self) -> None:
        """Render queued messages stacked above the prompt input."""
        queued = self.query_one("#queued", Static)
        if not self._buffer:
            queued.update("")
            queued.display = False
        else:
            queued.update(Text("\n".join(self._buffer)))
            queued.display = True
        self.refresh_bindings()

    def _ingest_usage(self, usage: Any) -> None:
        """Track token usage from an LLM response event."""
        if not isinstance(usage, dict):
            return
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            self._context_tokens = total
            self._session_tokens += total
            self._update_status()

    def _set_chat_id(self, chat_id: str) -> None:
        if chat_id != self._chat_id:
            # Token counters are per chat.
            self._context_tokens = 0
            self._session_tokens = 0
        self._chat_id = chat_id
        self._client.update_chat_id(chat_id)
        self._update_status()

    def _connection_indicator(self) -> str:
        if self._conn_status == "connected":
            return "[green]●[/] connected"
        return "[yellow]○[/] reconnecting…"

    def _status_text(self) -> str:
        conn = self._connection_indicator()
        if self._busy:
            state = "● thinking"
            if self._buffer:
                state += f"  ·  {len(self._buffer)} queued"
        else:
            state = "○ ready"
        tokens = ""
        if self._context_tokens:
            tokens = f"  ·  ctx {_fmt_tokens(self._context_tokens)} · total {_fmt_tokens(self._session_tokens)} tok"
        header = f"  {conn}  ·  Chat: {self._chat_id}  ·  Channel: {self._client.client_id}"
        return f"{header}  ·  {state}{tokens}"

    def _update_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_text())
        tags = self.query_one("#attachments", Static)
        tag_text = _render_attachment_tags(self._attachment_names)
        tags.update(tag_text)
        tags.display = bool(tag_text)

    def _clear_attachments(self) -> None:
        self._pending_attachments = []
        self._attachment_names = []
        self._update_status()

    async def _poll_connection_status(self) -> None:
        """Periodically check the WebSocket connection and update the status bar."""
        while True:
            try:
                new_status = "connected" if self._client.connected else "reconnecting"
                if new_status != self._conn_status:
                    self._conn_status = new_status
                    self._update_status()
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Connection status poll error", exc_info=True)


# ── entrypoint ─────────────────────────────────────────────────


async def run_chat_tui(client: GatewayClient) -> None:
    """Launch the TUI connected to a running gateway.

    ``client`` must be an ``GatewayClient`` that has already called
    ``connect()``.
    """
    app = ChatApp(client=client)
    await app.run_async()
