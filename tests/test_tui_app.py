import asyncio

import pytest

from bos.cli.tui_app import (
    ChatApp,
    CommandResultEvent,
    PlanModal,
    PromptHistory,
    PromptInput,
    SystemEvent,
    TurnEventMessage,
    _render_plan_text,
    run_chat_tui,
)
from bos.core.actor import MessageType
from bos.core.agent import TurnEvent
from bos.gateway import WS_TAKEOVER_CLOSE_REASON


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.client_id = "client-1"
        self.chat_id = "chat-1"
        self.connected = True
        self.workdir: str | None = None

    async def send(self, content, **kwargs):
        self.calls.append({"content": content, **kwargs})

    async def receive(self):
        await asyncio.Event().wait()

    async def list_actors(self):
        return {}

    def update_chat_id(self, chat_id: str) -> None:
        self.chat_id = chat_id

    def update_workdir(self, workdir: str | None) -> None:
        self.workdir = workdir or None


@pytest.mark.asyncio
async def test_run_chat_tui_constructs_chat_app_with_client(monkeypatch):
    client = FakeClient()
    seen = {}

    async def fake_run_async(self):
        seen["client"] = self._client

    monkeypatch.setattr(ChatApp, "run_async", fake_run_async)

    await run_chat_tui(client)

    assert seen["client"] is client


@pytest.mark.asyncio
async def test_handle_new_slash_command_uses_actor_command_path(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []

    monkeypatch.setattr(app, "_write_system", outputs.append)

    await app._handle_slash_command("/new")

    assert client.calls == [
        {
            "content": "/new",
            "content_type": MessageType.COMMAND,
            "chat_id": "chat-1",
        }
    ]
    assert outputs == ["[dim]  ⏳ /new…[/]"]


@pytest.mark.asyncio
async def test_ctrl_n_uses_same_reset_chat_path(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)

    async def fake_send_command(command_text: str) -> None:
        client.calls.append({"content": command_text})

    monkeypatch.setattr(app, "_send_command", fake_send_command)

    app.action_reset_chat()
    await asyncio.sleep(0)

    assert client.calls == [{"content": "/new"}]


class FakeQueued:
    def __init__(self) -> None:
        self.display = False
        self.content = None

    def update(self, content):
        self.content = content


class FakePrompt:
    def __init__(self, focused: list[bool]) -> None:
        self._focused = focused
        self.text = ""

    def focus(self):
        self._focused.append(True)

    def _replace_text(self, text: str) -> None:
        self.text = text


class FakeLog:
    def __init__(self) -> None:
        self.lines: list = []
        self.cleared = False

    def write(self, content):
        self.lines.append(content)

    def clear(self):
        self.cleared = True
        self.lines.clear()


def _fake_widgets(app, monkeypatch):
    """Patch query_one/refresh_bindings with fakes; return (queued, log, focused)."""
    queued = FakeQueued()
    log = FakeLog()
    focused: list[bool] = []
    prompt = FakePrompt(focused)

    def fake_query_one(selector, *args, **kwargs):
        if selector == "#queued":
            return queued
        if selector == "#prompt":
            return prompt
        if selector == "#chat":
            return log
        raise AssertionError(selector)

    monkeypatch.setattr(app, "query_one", fake_query_one)
    monkeypatch.setattr(app, "refresh_bindings", lambda: None)
    return queued, log, focused


@pytest.mark.asyncio
async def test_interrupt_turn_hotkey_sends_interrupt_abort(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._busy = True
    app._buffer.append("queued")
    outputs: list[str] = []
    updates: list[bool] = []

    queued, _log, focused = _fake_widgets(app, monkeypatch)
    queued.display = True
    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: updates.append(app._busy))

    await app.action_interrupt_turn()

    assert client.calls == [
        {
            "content": "",
            "content_type": MessageType.INTERRUPT_ABORT,
            "chat_id": "chat-1",
            "metadata": {"reason": "user_hotkey"},
        }
    ]
    assert app._busy is False
    assert app._buffer == []
    assert queued.display is False
    assert updates == [False]
    assert outputs == ["[yellow]⏹ Turn interrupt requested.[/]"]
    assert focused == [True]


@pytest.mark.asyncio
async def test_submit_while_busy_queues_locally_without_sending(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._busy = True

    queued, _log, _focused = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    class FakeEvent:
        value = "later message"

        class prompt_input:
            @staticmethod
            def clear():
                pass

    await app.on_prompt_input_submitted(FakeEvent())

    assert client.calls == []
    assert app._buffer == ["later message"]
    assert queued.display is True


@pytest.mark.asyncio
async def test_agent_reply_flushes_queued_messages_as_next_turn(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._busy = True
    app._buffer.extend(["first", "second"])

    queued, log, focused = _fake_widgets(app, monkeypatch)
    queued.display = True
    monkeypatch.setattr(app, "_update_status", lambda: None)

    from bos.cli.tui_app import AgentReplyEvent

    await app.on_agent_reply_event(AgentReplyEvent("done", chat_id="chat-1"))

    assert client.calls == [{"content": "first\n\nsecond", "chat_id": "chat-1"}]
    assert app._buffer == []
    assert queued.display is False
    assert app._busy is True
    assert focused == [True]


@pytest.mark.asyncio
async def test_agent_reply_without_queue_clears_busy(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._busy = True

    _queued, _log, _focused = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    from bos.cli.tui_app import AgentReplyEvent

    await app.on_agent_reply_event(AgentReplyEvent("done", chat_id="chat-1"))

    assert client.calls == []
    assert app._busy is False


@pytest.mark.asyncio
async def test_cancel_queued_drops_buffer_without_sending(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._busy = True
    app._buffer.extend(["first", "second"])
    outputs: list[str] = []

    queued, _log, _focused = _fake_widgets(app, monkeypatch)
    queued.display = True
    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    app.action_cancel_queued()

    assert client.calls == []
    assert app._buffer == []
    assert queued.display is False
    assert app._busy is True
    assert outputs == ["[yellow]✗ Dropped 2 queued messages.[/]"]


@pytest.mark.asyncio
async def test_workdir_command_set_show_unset(monkeypatch, tmp_path):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    monkeypatch.setattr(app, "_write_system", outputs.append)

    await app._handle_slash_command(f"/workdir {tmp_path}")
    assert client.workdir == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in outputs[-1]

    await app._handle_slash_command("/workdir")
    assert str(tmp_path.resolve()) in outputs[-1]

    await app._handle_slash_command("/workdir unset")
    assert client.workdir is None

    await app._handle_slash_command("/workdir")
    assert "unset" in outputs[-1]

    # purely client-side: nothing is sent to the gateway
    assert client.calls == []


@pytest.mark.asyncio
async def test_workdir_command_accepts_missing_path_with_warning(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    monkeypatch.setattr(app, "_write_system", outputs.append)

    await app._handle_slash_command("/workdir /no/such/dir")

    assert client.workdir == "/no/such/dir"
    assert "not found" in outputs[-1]


def test_cancel_queued_is_noop_when_empty(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    monkeypatch.setattr(app, "_write_system", outputs.append)

    app.action_cancel_queued()

    assert outputs == []
    assert app.check_action("cancel_queued", ()) is None
    app._buffer.append("x")
    assert app.check_action("cancel_queued", ()) is True


@pytest.mark.asyncio
async def test_interrupt_turn_without_active_turn_is_noop(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []

    monkeypatch.setattr(app, "_write_system", outputs.append)

    await app.action_interrupt_turn()

    assert client.calls == []
    assert outputs == ["[dim]No active turn to interrupt.[/]"]


@pytest.mark.asyncio
async def test_command_result_event_for_new_updates_displayed_chat(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._chat_id = "old-chat"
    outputs: list[str] = []
    updates: list[str] = []

    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: updates.append(app._chat_id))

    await app.on_command_result_event(
        CommandResultEvent(
            "new",
            {
                "name": "new",
                "ok": True,
                "result": "chat reset",
                "chat_id": "new-chat",
            },
        )
    )

    assert app._chat_id == "new-chat"
    assert client.chat_id == "new-chat"
    assert updates == ["new-chat"]
    assert outputs == ["[dim]chat reset[/]"]


@pytest.mark.asyncio
async def test_command_result_event_for_resume_updates_displayed_chat(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    updates: list[str] = []

    monkeypatch.setattr(app, "_write_system", lambda text: None)
    monkeypatch.setattr(app, "_update_status", lambda: updates.append(app._chat_id))

    await app.on_command_result_event(
        CommandResultEvent(
            "resume",
            {
                "name": "resume",
                "ok": True,
                "result": "resumed chat",
                "chat_id": "resumed-chat",
            },
        )
    )

    assert app._chat_id == "resumed-chat"
    assert client.chat_id == "resumed-chat"
    assert updates == ["resumed-chat"]
    assert log.cleared


@pytest.mark.asyncio
async def test_resume_result_renders_history_into_cleared_log(monkeypatch):
    from rich.markdown import Markdown

    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)
    log.lines.append("previous chat content")

    history = [
        {"llm_message": {"role": "user", "content": "hello"}},
        {"llm_message": {"role": "assistant", "content": "I am about to run tests", "tool_calls": [{"id": "t1"}]}},
        {"llm_message": {"role": "assistant", "content": "", "tool_calls": [{"id": "t2"}]}},
        {"llm_message": {"role": "tool", "content": "tool output"}},
        {"llm_message": {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}},
        {"llm_message": {"role": "user", "content": "old summary"}, "is_summary": True},
    ]
    await app.on_command_result_event(
        CommandResultEvent(
            "resume",
            {"name": "resume", "ok": True, "result": "resumed chat-9", "chat_id": "chat-9"},
            {"missing_messages": history},
        )
    )

    assert log.cleared
    assert "previous chat content" not in log.lines
    assert "\n[bold cyan]❯ You[/]" in log.lines
    assert "  hello" in log.lines
    # A tool-calling assistant message renders as a dim preview, not a reply.
    assert "\n[dim italic]● I am about to run tests[/]" in log.lines
    assert log.lines.count("\n[bold green]▸ Assistant[/]") == 1
    markdowns = [line.markup for line in log.lines if isinstance(line, Markdown)]
    assert markdowns == ["hi there"]
    assert not any("tool output" in str(line) for line in log.lines)
    assert not any("old summary" in str(line) for line in log.lines)
    # The confirmation line lands after the rendered history.
    assert log.lines[-1] == "[dim]resumed chat-9[/]"


@pytest.mark.asyncio
async def test_resume_without_args_requests_chat_list(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    monkeypatch.setattr(app, "_write_system", lambda text: None)

    await app._handle_slash_command("/resume")

    assert app._awaiting_chat_list is True
    assert client.calls == [
        {
            "content": "/chats",
            "content_type": MessageType.COMMAND,
            "chat_id": "chat-1",
        }
    ]


@pytest.mark.asyncio
async def test_resume_with_chat_id_delegates_to_server(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    monkeypatch.setattr(app, "_write_system", lambda text: None)

    await app._handle_slash_command("/resume chat-7")

    assert app._awaiting_chat_list is False
    assert client.calls == [
        {
            "content": "/resume chat-7",
            "content_type": MessageType.COMMAND,
            "chat_id": "chat-1",
        }
    ]


@pytest.mark.asyncio
async def test_chats_result_opens_picker_and_selection_resumes(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._awaiting_chat_list = True
    pushed: dict = {}

    monkeypatch.setattr(app, "_write_system", lambda text: None)
    monkeypatch.setattr(
        app, "push_screen", lambda screen, callback=None: pushed.update(screen=screen, callback=callback)
    )

    chats = [{"chat_id": "chat-2", "message_count": 3, "last_activity": None, "description": "hello"}]
    await app.on_command_result_event(CommandResultEvent("chats", {"name": "chats", "ok": True, "result": chats}))

    assert app._awaiting_chat_list is False
    assert pushed["screen"]._chats == chats

    pushed["callback"]("chat-2")
    await asyncio.sleep(0)
    assert client.calls[-1] == {
        "content": "/resume chat-2",
        "content_type": MessageType.COMMAND,
        "chat_id": "chat-1",
    }


@pytest.mark.asyncio
async def test_chat_picker_selecting_current_chat_sends_nothing(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._awaiting_chat_list = True
    pushed: dict = {}

    monkeypatch.setattr(app, "_write_system", lambda text: None)
    monkeypatch.setattr(
        app, "push_screen", lambda screen, callback=None: pushed.update(screen=screen, callback=callback)
    )

    chats = [{"chat_id": "chat-1", "message_count": 1, "last_activity": None, "description": "current"}]
    await app.on_command_result_event(CommandResultEvent("chats", {"name": "chats", "ok": True, "result": chats}))

    pushed["callback"]("chat-1")
    await asyncio.sleep(0)
    assert client.calls == []


@pytest.mark.asyncio
async def test_chats_result_with_empty_list_shows_message(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._awaiting_chat_list = True
    outputs: list[str] = []
    monkeypatch.setattr(app, "_write_system", outputs.append)

    await app.on_command_result_event(CommandResultEvent("chats", {"name": "chats", "ok": True, "result": []}))

    assert outputs == ["[dim]No chats to resume.[/]"]


@pytest.mark.asyncio
async def test_removed_slash_commands_report_unknown(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    monkeypatch.setattr(app, "_write_system", outputs.append)

    await app._handle_slash_command("/chats")
    await app._handle_slash_command("/restart")

    assert client.calls == []
    assert outputs == [
        "[yellow]Unknown command: /chats[/]",
        "[yellow]Unknown command: /restart[/]",
    ]


@pytest.mark.asyncio
async def test_app_mounts_and_focuses_prompt_without_crash():
    """Mount the real widget tree: catches conflicts with Textual internals
    (e.g. TextArea's own `history` attribute) that widget-level fakes miss."""
    client = FakeClient()
    app = ChatApp(client=client)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", PromptInput)
        assert app.focused is prompt
        await pilot.press("h", "i")
        assert prompt.text == "hi"


def test_prompt_history_cycles_and_restores_draft():
    history = PromptHistory()
    history.record("first")
    history.record("second")

    # Stepping back saves the in-progress draft.
    assert history.previous("draft text") == "second"
    assert history.previous("ignored") == "first"
    # Past the oldest entry it stays put.
    assert history.previous("ignored") == "first"
    # Stepping forward walks back to the saved draft.
    assert history.next() == "second"
    assert history.next() == "draft text"
    # Past the draft there is nothing to recall.
    assert history.next() is None


def test_prompt_history_skips_blank_and_consecutive_duplicates():
    history = PromptHistory()
    history.record("hello")
    history.record("   ")
    history.record("hello")
    history.record("world")

    assert history.previous("") == "world"
    assert history.previous("") == "hello"
    assert history.previous("") == "hello"


def test_prompt_history_record_resets_navigation():
    history = PromptHistory()
    history.record("first")
    assert history.previous("draft") == "first"

    history.record("second")
    # After a submit, navigation starts from the newest entry again.
    assert history.next() is None
    assert history.previous("") == "second"


@pytest.mark.asyncio
async def test_llm_usage_events_update_status_tokens(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    def usage_event(total: int) -> TurnEventMessage:
        return TurnEventMessage(
            TurnEvent(
                event_type="llm",
                phase="finish",
                chat_id="chat-1",
                turn_id="turn-1",
                detail="response_ready",
                metadata={"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": total}},
            )
        )

    await app.on_turn_event_message(usage_event(1200))
    await app.on_turn_event_message(usage_event(1500))

    assert app._context_tokens == 1500
    assert app._session_tokens == 2700
    assert "ctx 1.5k · total 2.7k tok" in app._status_text()

    # Token counters are per chat: switching chats resets them.
    app._set_chat_id("other-chat")
    assert app._context_tokens == 0
    assert "tok" not in app._status_text()


def test_status_text_uses_current_client_and_chat():
    client = FakeClient()
    app = ChatApp(client=client)

    assert "Chat: chat-1" in app._status_text()
    assert "Channel: client-1" in app._status_text()
    assert "connected" in app._status_text()
    assert "○ ready" in app._status_text()

    app._busy = True
    app._buffer.append("x")
    assert "● thinking" in app._status_text()
    assert "1 queued" in app._status_text()


@pytest.mark.asyncio
async def test_system_event_updates_displayed_chat(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    updates: list[str] = []

    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: updates.append(app._chat_id))

    event = SystemEvent("session acknowledged", "handshake-chat")

    await app.on_system_event(event)

    assert app._chat_id == "handshake-chat"
    assert client.chat_id == "handshake-chat"
    assert updates == ["handshake-chat"]
    assert outputs == ["[green]session acknowledged[/]"]


@pytest.mark.asyncio
async def test_session_event_hydrates_transcript(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)
    log.lines.append("stale viewport content")

    history = [{"llm_message": {"role": "user", "content": "hi"}}]
    await app.on_system_event(SystemEvent("connected", "chat-7", {"event": "session", "missing_messages": history}))

    assert app._chat_id == "chat-7"
    assert client.chat_id == "chat-7"
    assert log.cleared
    assert "stale viewport content" not in log.lines
    assert any("Agent CLI ready" in str(line) for line in log.lines)
    assert "  hi" in log.lines
    # The first session ack is the initial connect, not a reconnect.
    assert not any("Reconnected" in str(line) for line in log.lines)

    await app.on_system_event(SystemEvent("connected", "chat-7", {"event": "session", "missing_messages": history}))

    assert any("Reconnected" in str(line) for line in log.lines)


@pytest.mark.asyncio
async def test_stale_rejection_renders_missing_and_restores_prompt(monkeypatch):
    from rich.markdown import Markdown

    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)
    app._busy = True
    app._last_sent_text = "my rejected message"

    payload = {
        "event": "stale_chat",
        "error": "stale_chat",
        "missing_messages": [{"llm_message": {"role": "assistant", "content": "from elsewhere"}}],
    }
    await app.on_system_event(SystemEvent("{}", "chat-1", {"event": "stale_chat", "payload": payload}))

    assert app._busy is False
    assert app.query_one("#prompt").text == "my rejected message"
    assert app._last_sent_text == ""
    assert any("updated from another client" in str(line) for line in log.lines)
    assert any(getattr(line, "markup", "") == "from elsewhere" for line in log.lines if isinstance(line, Markdown))
    assert any("Send rejected" in str(line) and "back in the prompt" in str(line) for line in log.lines)


@pytest.mark.asyncio
async def test_stale_rejection_does_not_clobber_typed_text(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)
    app._busy = True
    app._last_sent_text = "my rejected message"
    app.query_one("#prompt").text = "already typing something new"

    await app.on_system_event(SystemEvent("{}", "chat-1", {"event": "stale_chat", "payload": {"missing_messages": []}}))

    assert app.query_one("#prompt").text == "already typing something new"
    assert not any("back in the prompt" in str(line) for line in log.lines)
    assert any("Send rejected" in str(line) for line in log.lines)


@pytest.mark.asyncio
async def test_active_turn_rejection_restores_prompt(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)
    app._busy = True
    app._last_sent_text = "try later"

    await app.on_system_event(
        SystemEvent("{}", "chat-1", {"event": "active_turn", "payload": {"event": "active_turn"}})
    )

    assert app._busy is False
    assert app.query_one("#prompt").text == "try later"
    assert any("Another turn is in progress" in str(line) for line in log.lines)


@pytest.mark.asyncio
async def test_takeover_system_event_exits_tui(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    exited: list[bool] = []

    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exited.append(True))

    await app.on_system_event(type("E", (), {"content": WS_TAKEOVER_CLOSE_REASON, "chat_id": None})())

    assert outputs == [f"[yellow]{WS_TAKEOVER_CLOSE_REASON} Exiting.[/]"]
    assert exited == [True]


@pytest.mark.asyncio
async def test_session_event_adopts_server_active_turn(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _, log, _ = _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    await app.on_system_event(
        SystemEvent(
            "connected",
            "chat-1",
            {
                "event": "session",
                "missing_messages": [],
                "active_turn": {"chat_id": "chat-1", "turn_id": "t1", "actor": "main"},
            },
        )
    )

    assert app._busy is True
    assert any("turn is in progress" in str(line) for line in log.lines)


@pytest.mark.asyncio
async def test_session_event_without_active_turn_clears_busy(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _fake_widgets(app, monkeypatch)
    monkeypatch.setattr(app, "_update_status", lambda: None)
    # The turn finished while this client was disconnected.
    app._busy = True
    app._pending_tool_calls.append(("Tool", ""))

    await app.on_system_event(
        SystemEvent("connected", "chat-1", {"event": "session", "missing_messages": [], "active_turn": None})
    )

    assert app._busy is False
    assert app._pending_tool_calls == []


@pytest.mark.asyncio
async def test_turn_aborted_system_event_resets_busy_state(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    app._busy = True
    app._pending_tool_calls.append(("Tool", ""))
    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    await app.on_system_event(SystemEvent("Turn aborted.", "chat-1", {"event": "turn_aborted"}))

    assert app._busy is False
    assert app._pending_tool_calls == []
    assert any("Turn aborted" in line for line in outputs)


@pytest.mark.asyncio
async def test_turn_error_system_event_resets_busy_and_shows_error(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    app._busy = True
    app._pending_tool_calls.append(("Tool", ""))
    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: None)

    await app.on_system_event(SystemEvent("Turn failed: provider exploded", "chat-1", {"event": "turn_error"}))

    assert app._busy is False
    assert app._pending_tool_calls == []
    assert any("provider exploded" in line for line in outputs)


def _plan_event(plan, chat_id: str = "chat-1") -> TurnEventMessage:
    return TurnEventMessage(
        TurnEvent(
            event_type="plan",
            phase="update",
            chat_id=chat_id,
            turn_id="turn-1",
            detail="plan_state",
            metadata={"plan": plan},
        )
    )


@pytest.mark.asyncio
async def test_plan_state_event_caches_plan_and_writes_card(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _queued, log, _focused = _fake_widgets(app, monkeypatch)

    plan = {
        "status": "needs_input",
        "objective": "Ship the feature",
        "breakdown": ["Write code", "Write tests"],
        "open_questions": ["Which DB?"],
    }
    await app.on_turn_event_message(_plan_event(plan))

    assert app._plan_by_chat["chat-1"] == plan
    card = str(log.lines[-1])
    assert "◆ Plan" in card
    assert "needs_input" in card
    assert "Ship the feature" in card
    assert "Write code" in card
    assert "Which DB?" in card


@pytest.mark.asyncio
async def test_plan_card_hides_breakdown_once_in_progress(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _queued, log, _focused = _fake_widgets(app, monkeypatch)

    plan = {"status": "in_progress", "objective": "Ship it", "breakdown": ["Step one"]}
    await app.on_turn_event_message(_plan_event(plan))

    card = str(log.lines[-1])
    assert "Ship it" in card
    assert "Step one" not in card


@pytest.mark.asyncio
async def test_plan_state_event_with_none_clears_cached_plan(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _queued, log, _focused = _fake_widgets(app, monkeypatch)

    await app.on_turn_event_message(_plan_event({"status": "approved", "objective": "Ship it"}))
    assert "chat-1" in app._plan_by_chat

    await app.on_turn_event_message(_plan_event(None))
    assert "chat-1" not in app._plan_by_chat
    assert "cleared" in str(log.lines[-1])


@pytest.mark.asyncio
async def test_plan_state_event_for_other_chat_caches_without_writing(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    _queued, log, _focused = _fake_widgets(app, monkeypatch)

    await app.on_turn_event_message(_plan_event({"status": "draft", "objective": "Elsewhere"}, chat_id="other-chat"))

    assert app._plan_by_chat["other-chat"]["objective"] == "Elsewhere"
    assert log.lines == []


@pytest.mark.asyncio
async def test_show_plan_action_opens_modal_or_reports_missing(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    pushed: list = []
    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "push_screen", lambda screen: pushed.append(screen))

    app.action_show_plan()
    assert pushed == []
    assert "No plan" in outputs[0]

    app._plan_by_chat["chat-1"] = {"status": "approved", "objective": "Ship it"}
    app.action_show_plan()
    assert len(pushed) == 1
    assert isinstance(pushed[0], PlanModal)


@pytest.mark.asyncio
async def test_plan_slash_command_routes_to_show_plan(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    shown: list[bool] = []
    monkeypatch.setattr(app, "action_show_plan", lambda: shown.append(True))

    await app._handle_slash_command("/plan")

    assert shown == [True]


def test_render_plan_text_includes_populated_sections_only():
    text = _render_plan_text({
        "status": "approved",
        "objective": "Ship the feature",
        "user_value": "Users see plans",
        "breakdown": ["Step one"],
        "open_questions": [],
    }).plain

    assert "Objective" in text
    assert "Ship the feature" in text
    assert "User value" in text
    assert "Breakdown" in text
    assert "Step one" in text
    assert "Open questions" not in text
    assert "Risks" not in text
