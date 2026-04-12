"""Shared Textual Chat Application for Agent interaction."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from typing import Any, Literal

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Header, Input, RichLog, Static

from bos.core import (
    AgentActor,
    Envelope,
    Mailbox,
    ReactAgent,
    ReactContext,
    ep_react_interceptor,
)

logger = logging.getLogger(__name__)

# Context variable used by TuiInterceptor to locate the active app and post messages to it.
CURRENT_TUI_APP: contextvars.ContextVar[App | None] = contextvars.ContextVar("current_tui_app", default=None)


class AgentStepEvent(Message):
    """Posted by the interceptor callback so the TUI can update live."""

    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__()
        self.info = info


class AgentReplyEvent(Message):
    """Posted when the actor finishes and sends its reply Envelope."""

    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content


class ChatApp(App):
    """Full-screen agent chat — inspired by Codex CLI / Claude Code."""

    TITLE = "td chat"
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
        agent: ReactAgent,
        mailbox: Mailbox,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._mailbox = mailbox
        self._conversation_id = uuid.uuid4().hex
        self._busy = False
        self._buffer: list[str] = []

        # Actor / mailbox addresses
        self._tui_address = "tui"
        self._agent_address = "tui_agent"

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
        self.sub_title = self._agent._model

        # Start reply polling worker
        self._poll_task = asyncio.create_task(self._poll_replies())

        # Welcome
        log = self.query_one("#conversation", RichLog)
        log.write("[bold $primary]Agent CLI ready.[/]")
        log.write(f"[dim]Model: {self._agent._model}  ·  Conversation: {self._conversation_id}[/]")
        log.write("[dim]Type /help for commands · Ctrl+C to quit[/]\n")

        self.query_one("#prompt", Input).focus()

    async def _poll_replies(self) -> None:
        """Background task: await reply envelopes from the actor."""
        while True:
            try:
                env = await self._mailbox.receive(self._tui_address)
                self.post_message(AgentReplyEvent(env.content))
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
                recipient=self._agent_address,
                content=text,
                conversation_id=self._conversation_id,
            )
            await self._mailbox.send(env)
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
            recipient=self._agent_address,
            content=text,
            conversation_id=self._conversation_id,
        )
        await self._mailbox.send(env)

    async def on_agent_step_event(self, event: AgentStepEvent) -> None:
        """Handle real-time interceptor events."""
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

        log.write("\n[bold green]▸ Assistant[/]")
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
            self._conversation_id = uuid.uuid4().hex
            self._write_system(f"[green]✓ New conversation: {self._conversation_id}[/]")
            self._update_status()

        elif cmd == "/history":
            try:
                messages = await self._agent._message_store.get_messages(self._conversation_id)
                dump = json.dumps([m.llm_message for m in messages], indent=2, default=str)
                self._write_system(f"[dim]{dump}[/]")
            except Exception as e:
                self._write_system(f"[red]Error: {e}[/]")

        elif cmd == "/compact":
            try:
                messages = await self._agent._message_store.get_messages(self._conversation_id)
                summary = await self._agent._consolidator.consolidate([m.llm_message for m in messages])
                await self._agent._message_store.save_summary(self._conversation_id, summary)
                self._write_system("[green]✓ Conversation compacted.[/]")
            except Exception as e:
                self._write_system(f"[red]Error: {e}[/]")

        elif cmd == "/tokens":
            try:
                messages = await self._agent._message_store.get_messages(self._conversation_id)
                char_count = sum(len(m.llm_message.get("content", "")) for m in messages)
                self._write_system(f"[dim]Approx chars: {char_count}  ·  ~{char_count // 4} tokens[/]")
            except Exception as e:
                self._write_system(f"[red]Error: {e}[/]")

        elif cmd == "/conversations":
            try:
                conversations = await self._agent._message_store.list_conversations()
                dump = json.dumps(conversations, indent=2, default=str)
                self._write_system(f"[dim]{dump}[/]")
            except Exception as e:
                self._write_system(f"[red]Error: {e}[/]")

        elif cmd == "/memory":
            try:
                memory = await self._agent._memory_store.list_memories()
                dump = json.dumps(memory, indent=2, default=str)
                self._write_system(f"[dim]{dump}[/]")
            except Exception as e:
                self._write_system(f"[red]Error: {e}[/]")

        elif cmd == "/clear":
            self.query_one("#conversation", RichLog).clear()

        else:
            self._write_system(f"[yellow]Unknown command: {cmd}[/]")

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
        return f"  {self._agent._model}  ·  {self._conversation_id}  ·  {state}"

    def _update_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_text())


@ep_react_interceptor(name="tui_interceptor")
class TuiInterceptor:
    """Interceptor that relays stage events to the active ChatApp."""

    def __init__(self, **kwargs) -> None:
        pass  # Rely on context variable for decoupling

    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
        ],
        context: ReactContext,
    ) -> None:
        app = CURRENT_TUI_APP.get()
        if not app:
            return

        info: dict[str, Any] = {
            "stage": stage,
            "turn_id": context.turn_id,
            "conversation_id": context.conversation_id,
        }

        if stage == "before_llm":
            info["detail"] = "thinking"

        elif stage == "after_llm":
            resp = context.current_llm_response
            if resp and resp.tool_calls:
                info["detail"] = "tool_calls"
                info["tool_calls"] = [{"name": tc.name, "arguments": tc.arguments} for tc in resp.tool_calls]
            else:
                info["detail"] = "response_ready"

        elif stage == "after_tool":
            # The last message in current should be the tool result
            last = context.current[-1].llm_message if context.current else {}
            info["detail"] = "tool_result"
            info["tool_name"] = last.get("name", "unknown")
            result_text = str(last.get("content", ""))
            info["tool_result"] = result_text[:200] + ("…" if len(result_text) > 200 else "")

        elif stage == "final_response":
            info["detail"] = "final"
            info["content"] = context.final_content or ""

        elif stage == "max_iteration":
            info["detail"] = "max_iteration"

        try:
            app.post_message(AgentStepEvent(info))
        except Exception:
            logger.debug("TUI interceptor callback error", exc_info=True)


async def run_chat_tui(agent: ReactAgent, mailbox: Mailbox) -> None:
    """Helper method to bootstrap the components for a TUI-driven interactive chat loop."""
    actor = AgentActor("tui_agent", agent, mailbox)
    app = ChatApp(agent=agent, mailbox=mailbox)
    token = CURRENT_TUI_APP.set(app)

    actor_task = asyncio.create_task(actor.run())

    try:
        await app.run_async()
    finally:
        CURRENT_TUI_APP.reset(token)
        actor_task.cancel()
        try:
            await actor_task
        except asyncio.CancelledError:
            pass
