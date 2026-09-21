def test_runner_start_bootstraps_gateway(tmp_path, monkeypatch):
    import asyncio

    from bos.gateway.config import ResolvedGatewayConfig
    from bos.runner.runner import start

    calls: list[tuple[str, object]] = []

    class HarnessContext:
        async def __aenter__(self):
            calls.append(("harness_enter", self))
            return "harness"

        async def __aexit__(self, exc_type, exc, tb):
            calls.append(("harness_exit", exc_type))

    class FakeWorkspace:
        # The mount takes the singleton lock for this run dir before it builds
        # anything, and runs the bootstrap the losing instance must skip.
        bos_dir = tmp_path / ".bos"

        def resolve_agents(self):
            calls.append(("resolve_agents", None))

        def bootstrap_platform(self):
            calls.append(("bootstrap_platform", None))

        def harness(self):
            return HarnessContext()

        def resolve_gateway_runtime(self):
            return "runtime"

    class FakeGateway:
        config = ResolvedGatewayConfig(host="127.0.0.1", port=0)

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

    names = [name for name, _ in calls]
    # The mount bootstraps only once it holds the lock, then builds the gateway.
    assert names[:4] == ["resolve_agents", "bootstrap_platform", "harness_enter", "gateway_init"]
    assert calls[3][1][1] == "harness"
    # Actors and channels (gateway_start) come up before the socket serves, and
    # the socket is the last thing torn down so the drain keeps its consumers.
    assert names.index("gateway_start") < names.index("site_start")
    assert ("gateway_stop", True) in calls
    assert names.index("gateway_stop") < names.index("harness_exit") < names.index("runner_cleanup")


def test_runner_start_stops_ungracefully_on_cancellation(tmp_path, monkeypatch):
    """A signal that cancels the ``start()`` task (the forceful path — see
    ``__main__._on_sigterm``, which escalates to ``main_task.cancel()``) must
    skip the drain, not just plumb a caller-supplied ``graceful`` flag through.
    Deleting the ``except asyncio.CancelledError`` clause in ``runner.start()``
    must fail this test."""
    import asyncio

    from bos.gateway.config import ResolvedGatewayConfig
    from bos.runner.runner import start

    calls: list[tuple[str, object]] = []

    class HarnessContext:
        async def __aenter__(self):
            calls.append(("harness_enter", self))
            return "harness"

        async def __aexit__(self, exc_type, exc, tb):
            calls.append(("harness_exit", exc_type))

    class FakeWorkspace:
        # The mount takes the singleton lock for this run dir before it builds
        # anything, and runs the bootstrap the losing instance must skip.
        bos_dir = tmp_path / ".bos"

        def resolve_agents(self):
            calls.append(("resolve_agents", None))

        def bootstrap_platform(self):
            calls.append(("bootstrap_platform", None))

        def harness(self):
            return HarnessContext()

        def resolve_gateway_runtime(self):
            return "runtime"

    class FakeGateway:
        config = ResolvedGatewayConfig(host="127.0.0.1", port=0)

        def __init__(self, *, runtime, harness):
            calls.append(("gateway_init", (runtime, harness)))

        async def start(self):
            calls.append(("gateway_start", None))

        def build_app(self):
            return object()

        def set_endpoint(self, host, port):
            calls.append(("gateway_endpoint", (host, port)))

        async def wait_for_shutdown(self):
            # Never returns on its own — the task gets cancelled while awaiting
            # this, the same way a SIGTERM's `main_task.cancel()` lands here.
            await asyncio.Event().wait()

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

    async def _run():
        task = asyncio.ensure_future(start(FakeWorkspace()))
        await asyncio.sleep(0.05)  # let it run up to the wait_for_shutdown suspend
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())

    assert ("gateway_stop", False) in calls  # the forceful path skips the drain
    assert ("runner_cleanup", None) in calls  # but the socket is still cleaned up


def test_runner_start_refuses_to_serve_when_another_gateway_holds_the_lock(tmp_path, monkeypatch):
    """The foreground path goes through GatewayMount, so it is now covered by the
    singleton flock — not just by the weaker pid-file check in ``boscli gateway
    start``. With the lock held elsewhere, nothing comes up and nothing binds
    (BEP 17 §3.4.1)."""
    import asyncio

    import pytest

    from bos.gateway.state import GatewayRunDir, acquire_singleton_lock
    from bos.runner.runner import start

    calls: list[str] = []

    class FakeWorkspace:
        bos_dir = tmp_path / ".bos"

        def resolve_agents(self):
            calls.append("resolve_agents")

        def bootstrap_platform(self):
            calls.append("bootstrap_platform")

        def harness(self):
            raise AssertionError("a standby mount must not open a harness")

        def resolve_gateway_runtime(self):
            raise AssertionError("a standby mount must not build a gateway")

    class RefusingSite:
        def __init__(self, runner, host, port):
            raise AssertionError("a standby mount must not bind a socket")

    monkeypatch.setattr("aiohttp.web.TCPSite", RefusingSite)

    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    try:
        with pytest.raises(RuntimeError, match="singleton lock"):
            asyncio.run(start(FakeWorkspace()))
    finally:
        holder.close()

    # bootstrap_platform imports extension modules and writes os.environ; the
    # instance that loses the race must do neither.
    assert calls == []


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
