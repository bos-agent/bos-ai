"""Tests for boscli ask wiring (gateway-backed oneshot)."""

import json

import pytest

from bos.config.workspace import WorkspaceResolutionError, resolve_config_source
from bos.protocol import Envelope, MessageType


def test_resolve_config_source_builtin_preset():
    """resolve_config_source with a preset name resolves to the built-in preset."""
    config_path, bos_dir, config = resolve_config_source("default")
    assert config_path.name == "default.toml"
    assert config_path.exists()


def test_resolve_config_source_unknown_name_raises():
    """resolve_config_source with an unknown name raises WorkspaceResolutionError."""
    with pytest.raises(WorkspaceResolutionError, match="Unknown config source"):
        resolve_config_source("nonexistent-config-zzz")


def test_build_workspace_for_ask_workspace_override(tmp_path, monkeypatch):
    """-w/--workspace overrides the workspace directory."""
    from bos.cli.commands.agent import _build_workspace_for_ask

    override_dir = tmp_path / "override"
    override_dir.mkdir()
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    ctx = type("Ctx", (), {"obj": {}})
    ws = _build_workspace_for_ask(ctx, workspace_override=str(override_dir))

    assert ws.workspace == override_dir.resolve()


class _StubGatewayClient:
    """Duck-typed stand-in for GatewayClient in the oneshot exchange."""

    def __init__(self, envelopes: list[Envelope]) -> None:
        self._envelopes = list(envelopes)
        self.sent: list[str] = []

    async def send(self, content, **kwargs) -> None:
        self.sent.append(content)

    async def receive(self) -> Envelope:
        return self._envelopes.pop(0)


def _env(content, content_type) -> Envelope:
    return Envelope(sender="agent@main", recipient="ask", content=content, content_type=content_type)


@pytest.mark.asyncio
async def test_run_oneshot_exchange_returns_final_reply():
    """The exchange sends the message and returns the first MESSAGE envelope content."""
    from bos.cli.commands.agent import _run_oneshot_exchange

    turn_event = json.dumps({"event_type": "turn", "phase": "start"})
    client = _StubGatewayClient(
        [
            _env(turn_event, MessageType.TURN_EVENT),
            _env("session noise", MessageType.SYSTEM),
            _env("final answer", MessageType.MESSAGE),
        ]
    )

    result = await _run_oneshot_exchange(client, "do the task", progress=None)

    assert result == "final answer"
    assert client.sent == ["do the task"]
