def test_runner_start_bootstraps_gateway(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from bos.runner.runner import start

    calls: list[tuple[str, object]] = []

    class HarnessContext:
        async def __aenter__(self):
            calls.append(("harness_enter", self))
            return "harness"

        async def __aexit__(self, exc_type, exc, tb):
            calls.append(("harness_exit", exc_type))

    class FakeWorkspace:
        def harness(self):
            return HarnessContext()

        def resolve_gateway_runtime(self):
            return "runtime"

    class FakeGateway:
        config = SimpleNamespace(host="127.0.0.1", port=0)

        def __init__(self, *, runtime, harness):
            calls.append(("gateway_init", (runtime, harness)))

        async def start(self):
            calls.append(("gateway_start", None))

        def build_app(self):
            return object()

        def set_endpoint(self, host, port):
            calls.append(("gateway_endpoint", (host, port)))

        async def wait_for_shutdown(self):
            calls.append(("gateway_wait", None))

        async def stop(self, *, graceful=True):
            calls.append(("gateway_stop", graceful))

    class FakeAppRunner:
        def __init__(self, app, access_log=None):
            pass

        async def setup(self):
            pass

        async def cleanup(self):
            calls.append(("runner_cleanup", None))

    class FakeSite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            calls.append(("site_start", None))

    monkeypatch.setattr("bos.gateway.Gateway", FakeGateway)
    monkeypatch.setattr("aiohttp.web.AppRunner", FakeAppRunner)
    monkeypatch.setattr("aiohttp.web.TCPSite", FakeSite)

    asyncio.run(start(FakeWorkspace()))

    assert calls[0][0] == "harness_enter"
    assert calls[1][0] == "gateway_init"
    assert calls[1][1][1] == "harness"
    assert calls[-1][0] == "harness_exit"
    # Actors and channels (gateway_start) come up before the socket serves.
    assert calls.index(("gateway_start", None)) < calls.index(("site_start", None))
    assert ("gateway_stop", True) in calls


def test_gateway_start_preserves_preset_name_for_background_runner(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from bos.cli.entry import cli

    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOS_GATEWAY_API_KEY", "secret")
    monkeypatch.setattr("bos.runner.proc.is_running", lambda rd: False)
    monkeypatch.setattr(
        "bos.runner.proc.read_state",
        lambda rd: {"runtime": "process", "pid": 1234, "gateway": {"host": "127.0.0.1", "port": 5920}},
    )
    captured: dict[str, object] = {}

    def fake_start_background(argv, rd, env=None, cwd=None):
        captured["argv"] = argv
        captured["run_root"] = rd.root
        captured["cwd"] = cwd
        return 1234

    monkeypatch.setattr("bos.runner.proc.start_background", fake_start_background)

    result = CliRunner().invoke(cli, ["-c", "default", "gateway", "start"])

    assert result.exit_code == 0
    assert captured["argv"][-2:] == ["--config", "default"]
    assert captured["run_root"] == (tmp_path / "home" / "presets" / "default" / "run").resolve()
    assert captured["cwd"] == (tmp_path / "home" / "presets" / "default").resolve()


def test_gateway_start_without_workspace_falls_back_to_default_preset(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from bos.cli.entry import cli

    workspace = tmp_path / "empty"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOS_GATEWAY_API_KEY", "secret")
    monkeypatch.setattr("bos.runner.proc.is_running", lambda rd: False)
    monkeypatch.setattr(
        "bos.runner.proc.read_state",
        lambda rd: {"runtime": "process", "pid": 1234, "gateway": {"host": "127.0.0.1", "port": 5920}},
    )
    captured: dict[str, object] = {}

    def fake_start_background(argv, rd, env=None, cwd=None):
        captured["argv"] = argv
        captured["run_root"] = rd.root
        captured["cwd"] = cwd
        return 1234

    monkeypatch.setattr("bos.runner.proc.start_background", fake_start_background)

    result = CliRunner().invoke(cli, ["gateway", "start"])

    assert result.exit_code == 0
    assert captured["argv"][-2:] == ["--config", "default"]
    assert captured["run_root"] == (tmp_path / "home" / "presets" / "default" / "run").resolve()
    assert captured["cwd"] == (tmp_path / "home" / "presets" / "default").resolve()


def test_gateway_status_marks_stale_state_and_hides_dead_details(tmp_path, monkeypatch):
    """A leftover state file must not be reported as a live endpoint/actors."""
    from click.testing import CliRunner

    from bos.cli.entry import cli

    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("bos.runner.proc.is_running", lambda rd: False)
    monkeypatch.setattr(
        "bos.runner.proc.read_state",
        lambda rd: {
            "runtime": "process",
            "pid": 1234,
            "started_at": "2026-09-01T00:00:00+00:00",
            "gateway": {"host": "127.0.0.1", "port": 37701},
            "actors": {"main": {"display_name": "Main", "status": "running"}},
        },
    )

    result = CliRunner().invoke(cli, ["-c", "default", "gateway", "status"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert "stale" in result.output
    assert "2026-09-01T00:00:00+00:00" in result.output  # the stale timestamp is the evidence
    assert "37701" not in result.output  # nothing is listening there
    assert "running" not in result.output  # actors belong to the dead process
    assert "Uptime" not in result.output
