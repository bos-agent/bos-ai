import asyncio

import pytest

from bos.cli.tui_app import ChatApp, CommandResultEvent, run_chat_tui
from bos.protocol import WS_TAKEOVER_CLOSE_REASON, MessageType


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.client_id = "client-1"
        self.chat_id = "chat-1"
        self.connected = True

    async def send(self, content, **kwargs):
        self.calls.append({"content": content, **kwargs})

    def update_chat_id(self, chat_id: str) -> None:
        self.chat_id = chat_id


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


@pytest.mark.asyncio
async def test_interrupt_turn_hotkey_sends_interrupt_abort(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    app._busy = True
    app._buffer.append("queued")
    outputs: list[str] = []
    updates: list[bool] = []
    focused: list[bool] = []

    class FakeSidebar:
        display = True

        def clear(self):
            self.display = False

    class FakePrompt:
        def focus(self):
            focused.append(True)

    sidebar = FakeSidebar()

    def fake_query_one(selector, *args, **kwargs):
        if selector == "#sidebar":
            return sidebar
        if selector == "#prompt":
            return FakePrompt()
        raise AssertionError(selector)

    monkeypatch.setattr(app, "query_one", fake_query_one)
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
    assert sidebar.display is False
    assert updates == [False]
    assert outputs == ["[yellow]⏹ Turn interrupt requested.[/]"]
    assert focused == [True]


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


def test_chat_status_text_uses_current_client_and_chat():
    client = FakeClient()
    app = ChatApp(client=client)

    assert "Chat: chat-1" in app._chat_status_text()
    assert "Channel: client-1" in app._chat_status_text()
    assert "connected" in app._chat_status_text()
    assert "Gateway | chat-1" in app._header_subtitle()


@pytest.mark.asyncio
async def test_system_event_updates_displayed_chat(monkeypatch):
    client = FakeClient()
    app = ChatApp(client=client)
    outputs: list[str] = []
    updates: list[str] = []

    monkeypatch.setattr(app, "_write_system", outputs.append)
    monkeypatch.setattr(app, "_update_status", lambda: updates.append(app._chat_id))

    event = type("E", (), {"content": "session acknowledged", "chat_id": "handshake-chat"})()

    await app.on_system_event(event)

    assert app._chat_id == "handshake-chat"
    assert client.chat_id == "handshake-chat"
    assert updates == ["handshake-chat"]
    assert outputs == ["[green]session acknowledged[/]"]


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
