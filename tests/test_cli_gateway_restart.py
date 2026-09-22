"""`boscli gateway restart`/`stop` branching on the runtime label (BEP 17 §4.3)."""

import json

import pytest
from click.testing import CliRunner

from bos.cli.entry import cli
from bos.gateway.state import GatewayRunDir


def _state(tmp_path, runtime, *, base_url=None):
    """Write a gateway.state describing a gateway in the given mode.

    The CLI has only this file to go on for a gateway it did not start, which is
    why Task 2 had to make the runtime label truthful before this branch existed.
    """
    (tmp_path / ".bos").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".bos" / "config.toml").write_text(
        '[runtime]\nmain_actor = "main"\n\n[runtime.actors.main]\nagent = "bos"\n',
        encoding="utf-8",
    )
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    gateway = {"host": "127.0.0.1", "port": 5920, "shutdown_grace_seconds": 30.0}
    if base_url is not None:
        gateway["base_url"] = base_url
    rd.state_file.write_text(json.dumps({"runtime": runtime, "pid": 4242, "gateway": gateway}), encoding="utf-8")
    return rd


def _invoke(tmp_path, *args):
    config = str(tmp_path / ".bos" / "config.toml")
    return CliRunner().invoke(cli, ["-c", config, "gateway", *args])


def test_restart_of_a_process_gateway_does_not_go_over_http(tmp_path, monkeypatch):
    _state(tmp_path, "process")
    posted: list[str] = []
    monkeypatch.setattr("httpx.post", lambda url, **kw: posted.append(url))
    monkeypatch.setattr("bos.runner.proc.is_running", lambda rd: False)
    started: list[bool] = []
    monkeypatch.setattr("bos.cli.commands.agent.start", lambda *a, **k: started.append(True))

    _invoke(tmp_path, "restart")

    assert posted == []


def test_restart_of_an_embedded_gateway_posts_to_its_base_url(tmp_path, monkeypatch):
    import httpx

    _state(tmp_path, "embedded", base_url="https://app.example.com/bos")
    seen: list[tuple[str, float | None]] = []

    def _post(url, **kwargs):
        seen.append((url, kwargs.get("timeout")))
        return httpx.Response(200, json={"ok": True, "state": "live"})

    monkeypatch.setattr("httpx.post", _post)

    result = _invoke(tmp_path, "restart")

    assert result.exit_code == 0, result.output
    assert seen[0][0] == "https://app.example.com/bos/api/restart"
    # Sized from the grace the running gateway published, as `stop` does.
    assert seen[0][1] == pytest.approx(40.0)


def test_restart_of_an_embedded_gateway_without_a_base_url_does_not_guess(tmp_path, monkeypatch):
    """BEP 17 §4.3: "It does not guess a URL." """
    _state(tmp_path, "embedded")
    monkeypatch.setattr("httpx.post", lambda url, **kw: pytest.fail(f"posted to {url}"))

    result = _invoke(tmp_path, "restart")

    assert result.exit_code != 0
    assert "embedded" in result.output
    assert "http://" not in result.output


def test_stop_refuses_an_embedded_gateway(tmp_path):
    _state(tmp_path, "embedded", base_url="https://app.example.com/bos")

    result = _invoke(tmp_path, "stop")

    assert result.exit_code != 0
    assert "host" in result.output.lower()
