import pytest

from bos.cli.tui_app import ChatApp, run_chat_tui


@pytest.mark.asyncio
async def test_run_chat_tui_constructs_chat_app_with_client(monkeypatch):
    client = object()
    seen = {}

    async def fake_run_async(self):
        seen["client"] = self._client

    monkeypatch.setattr(ChatApp, "run_async", fake_run_async)

    await run_chat_tui(client)

    assert seen["client"] is client
