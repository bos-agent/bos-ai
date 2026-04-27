import asyncio

import pytest

from bos.cli.tui_app import ChatApp, CommandResultEvent, run_chat_tui
from bos.extensions.channels.http import WS_TAKEOVER_CLOSE_REASON
from bos.protocol import MessageType


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.client_id = "client-1"
        self.chat_id = "chat-1"

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

    assert app._chat_status_text() == "  Chat: chat-1  |  Client: client-1"
    assert app._header_subtitle() == "HttpChannel | chat-1"


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
