"""`boscli gateway status`/`restart`/`stop` against an embedded gateway (BEP 17 §4.3)."""

import json

import httpx
import pytest
from click.testing import CliRunner

from bos.cli.entry import cli
from bos.gateway.state import GatewayRunDir, acquire_singleton_lock


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

    result = _invoke(tmp_path, "restart")

    # All three: `posted == []` alone is also satisfied by an early SystemExit
    # that never reaches the branch, which would make this test green while the
    # process path was broken.
    assert result.exit_code == 0, result.output
    assert started == [True]
    assert posted == []


def test_restart_of_an_embedded_gateway_posts_to_its_base_url(tmp_path, monkeypatch):
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


def test_status_sees_a_live_embedded_gateway(tmp_path):
    """BEP 17 §4.3: status "works against both a standalone and an embedded
    gateway, because both write gateway.state".

    ``is_running`` needs a gateway.pid and a ``bos.runner`` cmdline; a mount has
    neither, so this read as "stale state" — and the fix it printed,
    ``gateway start``, reaped the live gateway's state file on the way past.
    """
    rd = _state(tmp_path, "embedded", base_url="http://127.0.0.1:8123/bos")
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    try:
        result = _invoke(tmp_path, "status")
    finally:
        holder.close()

    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "embedded" in result.output
    assert "stale" not in result.output


def test_status_reports_a_stale_embedded_state_as_stopped(tmp_path):
    """The other direction: a host that crashed without unlinking leaves the
    flock free, and the state file it left behind describes nothing. Reporting
    that as running would make the file unreachable by every command."""
    _state(tmp_path, "embedded", base_url="http://127.0.0.1:8123/bos")

    result = _invoke(tmp_path, "status")

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_start_refuses_rather_than_reaping_a_live_embedded_gateway(tmp_path):
    """`gateway start` reaps stale pid/state before spawning. Keyed off the pid
    file, that unlinked a live mount's gateway.state; keyed off the flock, it
    refuses outright."""
    rd = _state(tmp_path, "embedded", base_url="http://127.0.0.1:8123/bos")
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    try:
        result = _invoke(tmp_path, "start")
    finally:
        holder.close()

    assert result.exit_code != 0
    assert "already running" in result.output
    assert rd.state_file.exists()


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(httpx.ConnectError("connection refused"), id="stale base_url"),
        pytest.param(httpx.ReadTimeout("timed out"), id="rebuild outran the budget"),
    ],
)
def test_restart_reports_an_unreachable_embedded_gateway(tmp_path, monkeypatch, exc):
    """A host that is down or moved, and a rebuild past the grace + margin
    budget, are ordinary failures of this command — one line and a non-zero
    exit, not an uncaught httpx error and a traceback."""
    _state(tmp_path, "embedded", base_url="https://app.example.com/bos")

    def _post(url, **kwargs):
        raise exc

    monkeypatch.setattr("httpx.post", _post)

    result = _invoke(tmp_path, "restart")

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Could not reach the embedded gateway" in result.output
    assert "Traceback" not in result.output
