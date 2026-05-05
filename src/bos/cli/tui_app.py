"""Textual Chat Application — connects to a running agent via channel.

This TUI is a pure external client. It communicates with the agent process
exclusively through ``HttpChannelClient`` over WebSocket. It never imports or
references the agent, harness, or actor directly.

Slash commands that need server-side data (``/history``, ``/compact``, etc.)
send a ``content_type="command"`` envelope and wait for a ``command_result``
response from the channel server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Header, Input, RichLog, Static
from textual_autocomplete import AutoComplete, DropdownItem

from bos.extensions.channels.http import WS_TAKEOVER_CLOSE_REASON
from bos.extensions.channels.http_client import HttpChannelClient
from bos.protocol import MessageType, TurnEvent

logger = logging.getLogger(__name__)


def _turn_event_label(event: TurnEvent) -> str:
    if event.parent_agent_name and event.agent_name and event.agent_name != event.parent_agent_name:
        return f"{event.parent_agent_name} -> {event.agent_name}"
    return event.agent_name or "agent"


# ── Textual messages ───────────────────────────────────────────


class TurnEventMessage(Message):
    """Structured runtime event forwarded from the agent process via the channel."""

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

    def __init__(self, name: str, data: Any) -> None:
        super().__init__()
        self.name = name
        self.data = data


class SystemEvent(Message):
    """System event emitted by the channel infrastructure."""

    def __init__(self, content: str, chat_id: str | None = None) -> None:
        super().__init__()
        self.content = content
        self.chat_id = chat_id


SLASH_COMMANDS = [
    "/help",
    "/new",
    "/resume",
    "/alias",
    "/aliases",
    "/unalias",
    "/history",
    "/compact",
    "/tokens",
    "/chats",
    "/memory",
    "/clear",
    "/restart",
]


class SlashAutoComplete(AutoComplete):
    """AutoComplete that activates for slash commands and @mentions."""

    def should_show_dropdown(self, search_string: str) -> bool:
        if not (search_string.startswith("/") or search_string.startswith("@")):
            return False
        return super().should_show_dropdown(search_string)

    def apply_completion(self, value: str, state: Any) -> None:
        """Apply the completion, appending a space after @mentions for routing."""
        if value.startswith("@"):
            value = value + " "
        super().apply_completion(value, state)


# ── ChatApp ────────────────────────────────────────────────────


class ChatApp(App):
    """Full-screen agent chat — channel-mode only.

    Communicates with the agent process via ``HttpChannelClient``.
    """

    TITLE = "bos tui"
    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 1fr;
    }

    #chat {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 1 1;
    }

    #sidebar {
        width: 35;
        height: 1fr;
        dock: right;
        border-left: solid $primary-background;
        padding: 0 1;
        display: none;
    }

    #chat-status {
        height: 1;
        dock: top;
        background: $primary-background;
        color: $text-muted;
        padding: 0 2;
    }

    #status-bar {
        height: 1;
        dock: bottom;
        background: $primary-background;
        color: $text-muted;
        padding: 0 2;
    }

    #prompt {
        dock: bottom;
        padding: 0 1;
    }

    Input {
        border: none;
    }

    Input:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("escape", "interrupt_turn", "Interrupt", show=True, priority=True),
        Binding("ctrl+enter", "interrupt_message", "Interject", show=True, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+l", "clear_log", "Clear", show=True),
        Binding("ctrl+n", "reset_chat", "New Chat", show=True),
        Binding("ctrl+r", "restart_bos", "Restart", show=True),
    ]

    theme = "tokyo-night"

    def __init__(
        self,
        client: HttpChannelClient,
    ) -> None:
        super().__init__()
        self._client = client
        if not client.chat_id:
            raise ValueError("HttpChannelClient must be connected and session-acknowledged before launching ChatApp.")
        self._chat_id = client.chat_id
        self._busy = False
        self._buffer: list[str] = []
        self._conn_status: str = "connected"
        self._known_actors: list[str] = []

    # ── compose ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._chat_status_text(), id="chat-status")
        with Horizontal(id="main-container"):
            yield RichLog(
                id="chat",
                highlight=True,
                markup=True,
                wrap=True,
                auto_scroll=True,
            )
            yield RichLog(
                id="sidebar",
                highlight=True,
                markup=True,
                wrap=False,
                auto_scroll=True,
            )
        yield Static(self._status_text(), id="status-bar")
        yield Input(placeholder="Send a message…", id="prompt")
        yield SlashAutoComplete("#prompt", candidates=self._get_candidates)
        yield Footer()

    # ── lifecycle ──────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._update_status()

        # Start reply polling worker
        self._poll_task = asyncio.create_task(self._poll_replies())
        self._conn_poll_task = asyncio.create_task(self._poll_connection_status())

        # Welcome
        log = self.query_one("#chat", RichLog)
        log.write("[bold $primary]Agent CLI ready.[/]")
        log.write("[dim]Type /help for commands · Escape to abort · Ctrl+Enter to interject · Ctrl+C to quit[/]\n")

        # Fetch actor list for @mention autocomplete
        try:
            actors = await self._client.list_actors()
            self._known_actors = list(actors.keys())
        except Exception:
            logger.debug("Failed to fetch actor list", exc_info=True)

        self.query_one("#prompt", Input).focus()

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
                    self.post_message(CommandResultEvent(cmd_name, data))
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
                    self.post_message(SystemEvent(env.content, env.chat_id))
                else:
                    # Normal reply
                    self.post_message(AgentReplyEvent(env.content, env.chat_id))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Poll error", exc_info=True)

    # ── event handlers ────────────────────────────────────────

    def on_key(self, event: Input.Changed | Any) -> None:
        """Redirect keystrokes to the prompt unless it already has focus."""
        prompt = self.query_one("#prompt", Input)
        if self.focused is not prompt:
            prompt.focus()
            # Forward printable characters into the input
            if event.character and event.is_printable:
                prompt.value += event.character
                # Defer cursor move so it isn't reset by the focus change
                self.call_after_refresh(
                    setattr, prompt, "cursor_position", len(prompt.value)
                )
                event.prevent_default()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()

        # Handle slash commands
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        if self._busy:
            self._buffer.append(text)
            sidebar = self.query_one("#sidebar", RichLog)
            sidebar.display = True
            sidebar.write("\n[bold cyan]❯ You (buffered)[/]")
            sidebar.write(f"  {text}")

            try:
                await self._client.send(text, chat_id=self._chat_id)
            except Exception as exc:
                self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")
            return

        # Write user message
        log = self.query_one("#chat", RichLog)
        log.write("\n[bold cyan]❯ You[/]")
        log.write(f"  {text}")

        # Send to actor
        self._busy = True
        self._update_status()
        try:
            await self._client.send(text, chat_id=self._chat_id)
        except Exception as exc:
            self._busy = False
            self._update_status()
            self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")

    async def on_turn_event_message(self, message: TurnEventMessage) -> None:
        """Handle structured runtime events from the agent process."""
        event = message.event
        log = self.query_one("#chat", RichLog)
        label = _turn_event_label(event)

        if event.event_type == "llm" and event.detail == "thinking":
            log.write(f"[dim italic]  🤔 {label} thinking…[/]")

        elif event.event_type == "llm" and event.detail == "tool_calls":
            for tc in event.tool_calls or []:
                args_str = ", ".join(f"{k}={v!r}" for k, v in tc["arguments"].items())
                prefix = f"{label}: " if label else ""
                log.write(f"[dim]  ⚡ {prefix}[bold]{tc['name']}[/bold]({args_str})[/]")

        elif event.event_type == "tool" and event.detail == "tool_result":
            name = event.tool_name or "?"
            preview = str(event.content or "")[:120].replace("\n", " ")
            log.write(f"[dim]  ↳ {label}: {name} → {preview}[/]")

        elif event.detail == "max_iteration":
            log.write(f"[yellow]  ⚠ {label} max iterations reached[/]")

        elif event.detail == "error":
            log.write(f"[red]  ⚠ {label} error: {event.content or 'unknown error'}[/]")

    async def on_agent_reply_event(self, event: AgentReplyEvent) -> None:
        """Handle the final reply from the actor."""
        log = self.query_one("#chat", RichLog)
        content = event.content or "(no response)"

        # Visual mark for replies from a non-current chat
        is_current = not event.chat_id or event.chat_id == self._chat_id
        chat_mark = "" if is_current else f" [dim](chat {event.chat_id[:8]}…)[/]"
        log.write(f"\n[bold green]▸ Assistant{chat_mark}[/]")
        try:
            md = Markdown(content)
            log.write(md)
        except Exception:
            log.write(f"  {content}")

        if self._buffer:
            log.write("\n[bold cyan]❯ You (buffered)[/]")
            for txt in self._buffer:
                log.write(f"  {txt}")

            sidebar = self.query_one("#sidebar", RichLog)
            sidebar.clear()
            sidebar.display = False
            self._buffer.clear()

            self._busy = True
        else:
            self._busy = False

        self._update_status()
        self.query_one("#prompt", Input).focus()

    async def on_command_result_event(self, event: CommandResultEvent) -> None:
        """Handle a slash command result from the server."""
        data = event.data
        if isinstance(data, dict):
            if event.name in {"new", "resume"} and data.get("ok"):
                chat_id = data.get("chat_id")
                if isinstance(chat_id, str):
                    self._set_chat_id(chat_id)
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
        if event.chat_id:
            self._set_chat_id(event.chat_id)
        self._write_system(f"[green]{event.content}[/]")

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
                "  /new      — start a new chat\n"
                "  /resume   — resume a chat by id or alias\n"
                "  /alias    — give the current chat an alias\n"
                "  /aliases  — list chat aliases\n"
                "  /unalias  — remove a chat alias\n"
                "  /history  — show chat history\n"
                "  /compact  — compact chat\n"
                "  /tokens   — rough token estimate\n"
                "  /chats    — list all chats\n"
                "  /memory   — list agent memories\n"
                "  /clear    — clear the log\n"
                "  /restart  — restart the agent process\n"
                "\n"
                "[bold]Hot keys:[/]\n"
                "  Escape      — abort the current turn\n"
                "  Ctrl+Enter  — inject a message into the current turn\n"
                "  Ctrl+C      — quit\n"
                "  Ctrl+L      — clear the log\n"
                "  Ctrl+N      — start a new chat\n"
                "  Ctrl+R      — restart the agent process"
            )

        elif normalized_cmd == "/new":
            await self._send_command(command_text)

        elif normalized_cmd == "/clear":
            self.query_one("#chat", RichLog).clear()

        elif normalized_cmd == "/restart":
            await self.action_restart_bos()

        elif normalized_cmd in (
            "/resume",
            "/alias",
            "/aliases",
            "/unalias",
            "/history",
            "/compact",
            "/tokens",
            "/chats",
            "/memory",
        ):
            # Delegate to the server via a command envelope
            await self._send_command(command_text)

        else:
            self._write_system(f"[yellow]Unknown command: {normalized_cmd}[/]")

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

    def action_reset_chat(self) -> None:
        asyncio.create_task(self._send_command("/new"))

    async def action_restart_bos(self) -> None:
        """Restart the background BOS agent."""
        self._write_system("[yellow]↻ Restarting agent process…[/]")
        import sys
        try:
            process = await asyncio.create_subprocess_exec(
                sys.argv[0], "restart",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            asyncio.create_task(process.wait())
        except Exception as exc:
            self._write_system(f"[red]⚠ Failed to restart agent: {exc}[/]")

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
        sidebar = self.query_one("#sidebar", RichLog)
        sidebar.clear()
        sidebar.display = False
        self._busy = False
        self._update_status()
        self._write_system("[yellow]⏹ Turn interrupt requested.[/]")
        self.query_one("#prompt", Input).focus()

    async def action_interrupt_message(self) -> None:
        """Send the current input as an interrupt message (inject into the ongoing turn)."""
        prompt = self.query_one("#prompt", Input)
        text = prompt.value.strip()
        if not text:
            return
        prompt.clear()

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
            log.write(f"  {text}")
            self._write_system("[yellow]⏎ Interrupt message sent.[/]")
        else:
            log.write("\n[bold cyan]❯ You[/]")
            log.write(f"  {text}")
            self._busy = True
            self._update_status()
            try:
                await self._client.send(text, chat_id=self._chat_id)
            except Exception as exc:
                self._busy = False
                self._update_status()
                self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")

    # ── autocomplete ───────────────────────────────────────────

    def _get_candidates(self, state: Any) -> list[str]:
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

    def _set_chat_id(self, chat_id: str) -> None:
        self._chat_id = chat_id
        self._client.update_chat_id(chat_id)
        self._update_status()

    def _connection_indicator(self) -> str:
        if self._conn_status == "connected":
            return "[green]●[/] connected"
        return "[yellow]○[/] reconnecting…"

    def _chat_status_text(self) -> str:
        conn = self._connection_indicator()
        return f"  {conn}  |  Chat: {self._chat_id}  |  Client: {self._client.client_id}"

    def _header_subtitle(self) -> str:
        conn = "●" if self._conn_status == "connected" else "○ reconnecting"
        return f"{conn}  HttpChannel | {self._chat_id}"

    def _status_text(self) -> str:
        state = "● thinking" if self._busy else "○ ready"
        return f"  HttpChannel  ·  {self._chat_id}  ·  {state}"

    def _update_status(self) -> None:
        self.sub_title = self._header_subtitle()
        self.query_one("#chat-status", Static).update(self._chat_status_text())
        self.query_one("#status-bar", Static).update(self._status_text())

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


async def run_chat_tui(
    client: HttpChannelClient,
) -> None:
    """Launch the TUI connected to a running agent via channel.

    ``client`` must be an ``HttpChannelClient`` that has already called
    ``connect()``.
    """
    app = ChatApp(client=client)
    await app.run_async()
