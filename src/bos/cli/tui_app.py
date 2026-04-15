"""Textual Chat Application — connects to a running agent via channel.

This TUI is a pure external client. It communicates with the agent process
exclusively through the ``Mailbox`` protocol (backed by ``HttpChannelClient``
over WebSocket). It never imports or references the agent, harness, or actor
directly.

Slash commands that need server-side data (``/history``, ``/compact``, etc.)
send a ``content_type="command"`` envelope and wait for a ``command_result``
response from the channel server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Header, Input, RichLog, Static

from bos.core import Envelope, Mailbox

logger = logging.getLogger(__name__)


# ── Textual messages ───────────────────────────────────────────


class AgentStepEvent(Message):
    """Real-time step info forwarded from the agent process via the channel."""

    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__()
        self.info = info


class AgentReplyEvent(Message):
    """Final reply envelope from the agent."""

    def __init__(self, content: str, conversation_id: str | None = None) -> None:
        super().__init__()
        self.content = content
        self.conversation_id = conversation_id


class CommandResultEvent(Message):
    """Result of a slash command executed on the server side."""

    def __init__(self, name: str, data: Any) -> None:
        super().__init__()
        self.name = name
        self.data = data


class SystemEvent(Message):
    """System event emitted by the channel infrastructure."""

    def __init__(self, content: str, conversation_id: str | None = None) -> None:
        super().__init__()
        self.content = content
        self.conversation_id = conversation_id


# ── ChatApp ────────────────────────────────────────────────────


class ChatApp(App):
    """Full-screen agent chat — channel-mode only.

    Communicates with the agent process via ``mailbox`` (which satisfies the
    ``Mailbox`` protocol — typically an ``HttpChannelClient``).
    """

    TITLE = "bos tui"
    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 1fr;
    }

    #conversation {
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
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("escape", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_log", "Clear", show=True),
        Binding("ctrl+n", "new_conversation", "New Conversation", show=True),
    ]

    theme = "tokyo-night"

    def __init__(
        self,
        mailbox: Mailbox,
        tui_address: str = "client@tui",
    ) -> None:
        super().__init__()
        self._mailbox = mailbox
        self._tui_address = tui_address
        self._conversation_id = uuid.uuid4().hex
        self._busy = False
        self._buffer: list[str] = []

    # ── compose ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            yield RichLog(
                id="conversation",
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
        yield Footer()

    # ── lifecycle ──────────────────────────────────────────────

    async def on_mount(self) -> None:
        self.sub_title = "→ HttpChannel"

        # Start reply polling worker
        self._poll_task = asyncio.create_task(self._poll_replies())

        # Welcome
        log = self.query_one("#conversation", RichLog)
        log.write("[bold $primary]Agent CLI ready.[/]")
        log.write(f"[dim]Channel: HttpChannel  ·  Conversation: {self._conversation_id}[/]")
        log.write("[dim]Type /help for commands · Ctrl+C to quit[/]\n")

        self.query_one("#prompt", Input).focus()

    async def _poll_replies(self) -> None:
        """Background task: await envelopes from the channel."""
        while True:
            try:
                env = await self._mailbox.receive()
                if env.content_type == "command_result":
                    # Server-side slash command response
                    try:
                        data = json.loads(env.content) if isinstance(env.content, str) else env.content
                    except json.JSONDecodeError:
                        data = env.content
                    cmd_name = data.get("name", "?") if isinstance(data, dict) else "?"
                    self.post_message(CommandResultEvent(cmd_name, data))
                elif env.content_type == "agent_step":
                    # Real-time step info from the agent process
                    try:
                        info = json.loads(env.content) if isinstance(env.content, str) else {}
                    except json.JSONDecodeError:
                        info = {}
                    self.post_message(AgentStepEvent(info))
                elif env.content_type == "echo":
                    # User input from another channel — display it
                    log = self.query_one("#conversation", RichLog)
                    log.write(f"\n[bold dim cyan]❯ User ({env.sender})[/]")
                    log.write(f"  {env.content}")
                elif env.content_type == "system":
                    self.post_message(SystemEvent(env.content, env.conversation_id))
                else:
                    # Normal reply
                    self.post_message(AgentReplyEvent(env.content, env.conversation_id))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Poll error", exc_info=True)

    # ── event handlers ────────────────────────────────────────

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

            env = Envelope(
                sender=self._tui_address,
                recipient="",
                content=text,
                conversation_id=self._conversation_id,
            )
            try:
                await self._mailbox.send(env)
            except Exception as exc:
                self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")
            return

        # Write user message
        log = self.query_one("#conversation", RichLog)
        log.write("\n[bold cyan]❯ You[/]")
        log.write(f"  {text}")

        # Send to actor
        self._busy = True
        self._update_status()
        env = Envelope(
            sender=self._tui_address,
            recipient="",
            content=text,
            conversation_id=self._conversation_id,
        )
        try:
            await self._mailbox.send(env)
        except Exception as exc:
            self._busy = False
            self._update_status()
            self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")

    async def on_agent_step_event(self, event: AgentStepEvent) -> None:
        """Handle real-time step info from the agent process."""
        info = event.info
        log = self.query_one("#conversation", RichLog)
        detail = info.get("detail", "")

        if detail == "thinking":
            log.write("[dim italic]  🤔 thinking…[/]")

        elif detail == "tool_calls":
            for tc in info.get("tool_calls", []):
                args_str = ", ".join(f"{k}={v!r}" for k, v in tc["arguments"].items())
                log.write(f"[dim]  ⚡ tool: [bold]{tc['name']}[/bold]({args_str})[/]")

        elif detail == "tool_result":
            name = info.get("tool_name", "?")
            result = info.get("tool_result", "")
            preview = result[:120].replace("\n", " ")
            log.write(f"[dim]  ↳ {name} → {preview}[/]")

        elif detail == "max_iteration":
            log.write("[yellow]  ⚠ max iterations reached[/]")

    async def on_agent_reply_event(self, event: AgentReplyEvent) -> None:
        """Handle the final reply from the actor."""
        log = self.query_one("#conversation", RichLog)
        content = event.content or "(no response)"

        # Visual mark for replies from a non-current conversation
        is_current = not event.conversation_id or event.conversation_id == self._conversation_id
        conv_mark = "" if is_current else f" [dim](conv {event.conversation_id[:8]}…)[/]"
        log.write(f"\n[bold green]▸ Assistant{conv_mark}[/]")
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
        if event.conversation_id:
            self._conversation_id = event.conversation_id
            self._update_status()
        self._write_system(f"[green]{event.content}[/]")

    # ── slash commands ────────────────────────────────────────

    async def _handle_slash_command(self, text: str) -> None:
        cmd = text.strip().lower()

        if cmd == "/help":
            self._write_system(
                "[bold]Commands:[/]\n"
                "  /help     — show this help\n"
                "  /new      — start a new conversation\n"
                "  /history  — show conversation history\n"
                "  /compact  — compact conversation\n"
                "  /tokens   — rough token estimate\n"
                "  /conversations  — list all conversations\n"
                "  /memory   — list agent memories\n"
                "  /clear    — clear the log"
            )

        elif cmd == "/new":
            await self._send_channel_command("new_conversation", conversation_id=uuid.uuid4().hex)

        elif cmd == "/clear":
            self.query_one("#conversation", RichLog).clear()

        elif cmd in ("/history", "/compact", "/tokens", "/conversations", "/memory"):
            # Delegate to the server via a command envelope
            await self._send_command(cmd.lstrip("/"))

        else:
            self._write_system(f"[yellow]Unknown command: {cmd}[/]")

    async def _send_command(self, command_name: str) -> None:
        """Send a slash command to the channel server for execution."""
        env = Envelope(
            sender=self._tui_address,
            recipient="",
            content=f"/{command_name}",
            content_type="command",
            conversation_id=self._conversation_id,
        )
        try:
            await self._mailbox.send(env)
        except Exception as exc:
            self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")
            return
        self._write_system(f"[dim]  ⏳ /{command_name}…[/]")

    async def _send_channel_command(self, command_name: str, conversation_id: str | None = None) -> None:
        env = Envelope(
            sender=self._tui_address,
            recipient="",
            content=command_name,
            content_type="channel_command",
            conversation_id=conversation_id,
        )
        try:
            await self._mailbox.send(env)
        except Exception as exc:
            self._write_system(f"[yellow]⚠ Send failed — reconnecting: {exc}[/]")

    # ── actions ────────────────────────────────────────────────

    def action_clear_log(self) -> None:
        self.query_one("#conversation", RichLog).clear()

    def action_new_conversation(self) -> None:
        self._conversation_id = uuid.uuid4().hex
        self._write_system(f"[green]✓ New conversation: {self._conversation_id}[/]")
        self._update_status()

    # ── helpers ────────────────────────────────────────────────

    def _write_system(self, text: str) -> None:
        self.query_one("#conversation", RichLog).write(text)

    def _status_text(self) -> str:
        state = "● thinking" if self._busy else "○ ready"
        return f"  HttpChannel  ·  {self._conversation_id}  ·  {state}"

    def _update_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_text())


# ── entrypoint ─────────────────────────────────────────────────


async def run_chat_tui(mailbox: Mailbox) -> None:
    """Launch the TUI connected to a running agent via channel.

    ``mailbox`` must satisfy the ``Mailbox`` protocol — typically an
    ``HttpChannelClient`` that has already called ``connect()``.
    """
    app = ChatApp(mailbox=mailbox)
    await app.run_async()
