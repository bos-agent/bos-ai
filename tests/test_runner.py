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

        def __init__(self, *, runtime, harness, runtime_label="process"):
            calls.append(("gateway_init", (runtime, harness)))

        async def start(self):
            calls.append(("gateway_start", None))

        def build_app(self):
            from starlette.applications import Starlette

            return Starlette()

        def set_endpoint(self, host, port):
            calls.append(("gateway_endpoint", (host, port)))

        async def wait_for_shutdown(self):
            calls.append(("gateway_wait", None))

        async def stop(self, *, graceful=True):
            calls.append(("gateway_stop", graceful))

    class FakeSocket:
        def getsockname(self):
            return ("127.0.0.1", 12345)

    class FakeSocketServer:
        sockets = [FakeSocket()]

    class FakeUvicornServer:
        """Stands in for uvicorn.Server: binds nothing, records the calls.

        ``serve()`` drives startup/main_loop/shutdown itself rather than
        ``Server.serve()``, which would install signal handlers — so those three
        are what a fake has to provide.
        """

        def __init__(self, config):
            self.config = config
            self.should_exit = False
            self.started = False
            self.lifespan = None
            self.servers = [FakeSocketServer()]

        async def startup(self, sockets=None):
            calls.append(("site_start", None))
            self.started = True

        async def main_loop(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)

        async def shutdown(self, sockets=None):
            calls.append(("runner_cleanup", None))

    import uvicorn

    from bos.gateway.channels.ws_channel import WS_MAX_MESSAGE_BYTES

    # Capture what serve() *passes*, not what the Config ends up holding:
    # uvicorn's ws_max_size default is currently the same 16 MiB, so asserting
    # the resulting value cannot tell a stated limit from a coincidental one —
    # and the coincidence is precisely the thing that must not be relied on.
    real_config = uvicorn.Config
    passed: dict = {}

    def _capturing_config(*args, **kwargs):
        passed.update(kwargs)
        return real_config(*args, **kwargs)

    monkeypatch.setattr("bos.gateway.Gateway", FakeGateway)
    monkeypatch.setattr("uvicorn.Config", _capturing_config)
    monkeypatch.setattr("uvicorn.Server", FakeUvicornServer)

    asyncio.run(start(FakeWorkspace()))

    names = [name for name, _ in calls]
    # Both ends of the websocket state the same ceiling. GatewayClient raises the
    # websockets client off its 1 MiB default to WS_MAX_MESSAGE_BYTES; a session
    # ack carrying a full transcript is what falls over if the two ever drift.
    assert passed["ws_max_size"] == WS_MAX_MESSAGE_BYTES
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

        def __init__(self, *, runtime, harness, runtime_label="process"):
            calls.append(("gateway_init", (runtime, harness)))

        async def start(self):
            calls.append(("gateway_start", None))

        def build_app(self):
            from starlette.applications import Starlette

            return Starlette()

        def set_endpoint(self, host, port):
            calls.append(("gateway_endpoint", (host, port)))

        async def wait_for_shutdown(self):
            # Never returns on its own — the task gets cancelled while awaiting
            # this, the same way a SIGTERM's `main_task.cancel()` lands here.
            await asyncio.Event().wait()

        async def stop(self, *, graceful=True):
            calls.append(("gateway_stop", graceful))

    class FakeSocket:
        def getsockname(self):
            return ("127.0.0.1", 12345)

    class FakeSocketServer:
        sockets = [FakeSocket()]

    class FakeUvicornServer:
        """Stands in for uvicorn.Server: binds nothing, records the calls.

        ``serve()`` drives startup/main_loop/shutdown itself rather than
        ``Server.serve()``, which would install signal handlers — so those three
        are what a fake has to provide.
        """

        def __init__(self, config):
            self.config = config
            self.should_exit = False
            self.started = False
            self.lifespan = None
            self.servers = [FakeSocketServer()]

        async def startup(self, sockets=None):
            calls.append(("site_start", None))
            self.started = True

        async def main_loop(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)

        async def shutdown(self, sockets=None):
            calls.append(("runner_cleanup", None))

    monkeypatch.setattr("bos.gateway.Gateway", FakeGateway)
    monkeypatch.setattr("uvicorn.Server", FakeUvicornServer)

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
    from bos.runner.runner import GatewayAlreadyRunningError, start

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

    class RefusingServer:
        def __init__(self, config):
            raise AssertionError("a standby mount must not bind a socket")

    monkeypatch.setattr("uvicorn.Server", RefusingServer)

    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    try:
        with pytest.raises(GatewayAlreadyRunningError, match="already running"):
            asyncio.run(start(FakeWorkspace()))
    finally:
        holder.close()

    # bootstrap_platform imports extension modules and writes os.environ; the
    # instance that loses the race must do neither.
    assert calls == []


def test_gateway_start_foreground_refuses_cleanly_when_the_lock_is_held(tmp_path, monkeypatch):
    """``--foreground`` writes no pid file, so ``is_running`` never sees one and
    the mount's flock is the only guard. Its refusal must read like the
    background path's — one line and exit 1 — not a stack trace."""
    from click.testing import CliRunner

    from bos.cli.entry import cli
    from bos.gateway.state import GatewayRunDir, acquire_singleton_lock

    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOS_GATEWAY_API_KEY", "secret")

    rd = GatewayRunDir(tmp_path / "home" / "presets" / "default")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    try:
        result = CliRunner().invoke(cli, ["-c", "default", "gateway", "start", "--foreground"])
    finally:
        holder.close()

    assert result.exit_code == 1
    assert "already running" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_losing_the_lock_leaves_the_live_gateways_pid_file_alone(tmp_path, monkeypatch):
    """A refused start must not clobber gateway.pid — it belongs to the winner.

    This is the race ``proc.start_background`` refuses to risk (see its
    docstring): a second ``gateway start`` spawned inside the first child's
    bring-up window, before it has written its pid, so ``is_running`` is still
    False. If the loser removes that file, ``status`` reports stopped,
    ``stop``/``restart`` cannot find the live process and ``reap_stale`` clears
    its state.
    """
    import asyncio
    import signal
    import sys

    from bos.gateway.state import GatewayRunDir, acquire_singleton_lock
    from bos.runner.__main__ import main

    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "argv", ["bos.runner", "--config", "default"])

    rd = GatewayRunDir(tmp_path / "home" / "presets" / "default")
    rd.ensure()
    rd.pid_file.write_text("4242", encoding="utf-8")  # the winner's pid
    holder = acquire_singleton_lock(rd)  # ... and the winner's flock
    assert holder is not None

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        main()  # loses the lock, logs "already running", returns
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        asyncio.set_event_loop(asyncio.new_event_loop())
        holder.close()

    assert rd.pid_file.exists()
    assert rd.pid_file.read_text(encoding="utf-8") == "4242"


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


def test_gateway_start_leaves_the_bootstrap_to_the_mount_in_foreground(tmp_path, monkeypatch):
    """``--foreground`` must not bootstrap ahead of ``runner.start()``.

    The mount bootstraps the same Workspace once it holds the singleton lock. A
    second pass re-executes every ``./extensions/*.py`` — ``_load_ext_paths``
    runs the file through ``spec.loader.exec_module``, not the module cache — so
    a file that constructs its own ``ExtensionPoint`` raises on the replay, is
    swallowed into a warning, and takes every tool, plugin and channel it
    registered out of the gateway with it (BEP 17 §3.5.2). The background path
    keeps it: the bootstrap happens in a child process there, so this is its
    only pre-flight.
    """
    from click.testing import CliRunner

    from bos.cli.entry import cli
    from bos.config import Workspace

    monkeypatch.setenv("BOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOS_GATEWAY_API_KEY", "secret")

    calls: list[str] = []
    monkeypatch.setattr(Workspace, "resolve_agents", lambda self: calls.append("resolve_agents"))
    monkeypatch.setattr(Workspace, "bootstrap_platform", lambda self: calls.append("bootstrap_platform"))

    async def fake_start(workspace, *, on_ready=None):
        calls.append("runner_start")

    monkeypatch.setattr("bos.runner.runner.start", fake_start)

    result = CliRunner().invoke(cli, ["-c", "default", "gateway", "start", "--foreground"])
    assert result.exit_code == 0, result.output
    assert calls == ["runner_start"]  # the mount, and only the mount, bootstraps

    monkeypatch.setattr("bos.runner.proc.is_running", lambda rd: False)
    monkeypatch.setattr(
        "bos.runner.proc.read_state",
        lambda rd: {"pid": 1234, "gateway": {"host": "127.0.0.1", "port": 5920}},
    )
    monkeypatch.setattr("bos.runner.proc.start_background", lambda argv, rd, env=None, cwd=None: 1234)

    calls.clear()
    result = CliRunner().invoke(cli, ["-c", "default", "gateway", "start"])
    assert result.exit_code == 0, result.output
    assert calls == ["resolve_agents", "bootstrap_platform"]
