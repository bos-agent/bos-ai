

def test_runner_start_bootstraps_gateway(monkeypatch):
    import asyncio

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

    class FakeGateway:
        def __init__(self, *, workspace, harness):
            calls.append(("gateway_init", (workspace, harness)))

        async def run(self):
            calls.append(("gateway_run", None))

    monkeypatch.setattr("bos.gateway.Gateway", FakeGateway)

    asyncio.run(start(FakeWorkspace()))

    assert calls[0][0] == "harness_enter"
    assert calls[1][0] == "gateway_init"
    assert calls[1][1][1] == "harness"
    assert calls[2] == ("gateway_run", None)
    assert calls[3][0] == "harness_exit"


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
