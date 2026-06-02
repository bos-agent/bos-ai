from bos.config.workspace import Workspace


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


def test_resolved_http_channel_binds_all_interfaces_in_docker(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()
    (bos_dir / "config.toml").write_text(
        """
[main]
agent = "main"

[[main.channels]]
name = "HttpChannel"
bind_address = "channel@http"
target_address = "agent@main"
host = "127.0.0.1"
port = 8080
""".strip()
        + "\n",
        encoding="utf-8",
    )

    channel = Workspace.from_discovery(tmp_path).resolve_channels(runtime_kind="docker")[0]

    assert channel.options["host"] == "0.0.0.0"

