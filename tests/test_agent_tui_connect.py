import click
import pytest

from bos.cli.commands.agent import _connect_tui_client, _default_tui_client_id


class ConflictError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"status={status}")
        self.status = status


class FakeClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[bool] = []

    async def connect(self, *, takeover: bool = False) -> None:
        self.calls.append(takeover)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome


@pytest.mark.asyncio
async def test_connect_tui_client_prompts_and_retries_with_takeover(monkeypatch):
    client = FakeClient([ConflictError(409), None])
    prompts: list[str] = []

    def fake_confirm(text: str, default: bool = False) -> bool:
        prompts.append(text)
        return True

    monkeypatch.setattr(click, "confirm", fake_confirm)

    await _connect_tui_client(client)

    assert client.calls == [False, True]
    assert prompts == ["Another interactive TUI client is already connected. Disconnect it and take over?"]


@pytest.mark.asyncio
async def test_connect_tui_client_aborts_when_user_declines_takeover(monkeypatch):
    client = FakeClient([ConflictError(409)])

    monkeypatch.setattr(click, "confirm", lambda text, default=False: False)

    with pytest.raises(click.Abort):
        await _connect_tui_client(client)

    assert client.calls == [False]


@pytest.mark.asyncio
async def test_connect_tui_client_propagates_non_conflict_errors():
    client = FakeClient([RuntimeError("boom")])

    with pytest.raises(RuntimeError, match="boom"):
        await _connect_tui_client(client)

    assert client.calls == [False]


def test_default_tui_client_id_uses_normalized_username(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "Jerry Compute")

    assert _default_tui_client_id() == "tui:jerry-compute"


def test_default_tui_client_id_falls_back_to_env_username(monkeypatch):
    def raise_getuser():
        raise OSError("no user")

    monkeypatch.setattr("getpass.getuser", raise_getuser)
    monkeypatch.setenv("USERNAME", "Windows User")

    assert _default_tui_client_id() == "tui:windows-user"
