"""BEP 19 Layer 4a, Task 10: the host's own `ep_tool`s reaching Codex over MCP,
and `boscli inspect` reading the runtime's *resolved* config (§3.8, §8.2).

The two belong together because they share one seam: what an agent asked for
(`mcp_tools`) versus what the host can actually serve. It could not be the MCP
server that answers it. `register_agent` returns only a token, so there is
nothing to read back out of it, and `inspect` never reaches registration
anyway, because the client — and with it the server — is built lazily on the
first turn and `inspect` runs none. So the rule is a predicate,
`mcp_egress.unregistered_tools`, with three readers: `register_agent`'s own
warn-and-skip, `CodexAgent`'s egress setup (which warns, then declines to build
a server with nothing in it — BEP 19 §7.5), and `resolved_config`, which is
what `inspect` reads. The tests below pin them to each other rather than
one at a time.
"""

from __future__ import annotations

import io
import logging

import pytest
from test_external_agent_seam import _write_workspace


@pytest.fixture
def host_tools():
    """One host tool in the global `ep_tool` registry, removed again after."""
    from bos.core.contract import ep_tool

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    @ep_tool(name="WiringAlpha", description="Alpha", parameters=schema)
    async def alpha(x: str) -> str:
        return f"alpha:{x}"

    yield
    ep_tool._extensions.pop("WiringAlpha", None)


def _agent(tmp_path, fake_codex, *, mcp, **cfg):
    """A `CodexAgent` with a caller-supplied `mcp` accessor.

    `tests/test_codex_runtime.py`'s own `_agent` hardcodes `mcp=lambda: None`,
    which is right there — no test in that file reaches the accessor — and
    useless here, where the accessor *is* the subject. `fake_codex` is a
    required parameter (not read) so every caller is guaranteed the
    `_CODEX_FACTORY` patch is live before the agent builds a client.
    """
    from bos.core.defaults.structured_validator import JsonSchemaValidator
    from bos.extensions.runtimes.codex import CodexAgent

    cfg.setdefault("permission", "read-only")
    return CodexAgent(
        kind="george",
        cfg=cfg,
        chat_store=None,
        workspace=tmp_path,
        mcp=mcp,
        structured_validator=JsonSchemaValidator(),
    )


def _never_called():
    """An `mcp` accessor that fails the test if anything asks for a server.

    Not a call counter: `AgentHarness._ensure_tool_mcp_server` *builds* the
    server as a side effect of being called, so one call has already bound a
    loopback port. The call itself is the failure.
    """

    raise AssertionError("the MCP server accessor was called, and calling it is what builds the server")


async def _list_tools(url: str, authorization: str) -> list[str]:
    """`tools/list` against *url*, sending *authorization* verbatim.

    Verbatim, not re-wrapped from a token, because the header value is exactly
    what this task builds: a test that rebuilt `f"Bearer {token}"` itself would
    pass on a config that told Codex to send something else.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with create_mcp_http_client(headers={"Authorization": authorization}) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return sorted(t.name for t in (await session.list_tools()).tools)


@pytest.mark.asyncio
async def test_the_thread_config_opens_the_door_to_exactly_the_agents_tools(
    tmp_path, fake_codex, host_tools, caplog
):
    """The whole chain in one, against a real server: the parsed `mcp_tools`
    tuple -> `register_agent` -> a bearer token -> the `http_headers` Codex is
    told to send -> a `tools/list` that answers with exactly the granted tools.

    `mcp_tools` deliberately names one tool the host has and one it does not, so
    the granted set is a strict subset and the assertion below is an invariant
    rather than a restatement: what the server grants is the parsed list minus
    what `resolved_config` reports as unavailable. That is the tie between the
    two halves of this task — one predicate, two readers.

    It also pins BEP 19 §7.5's "exactly one" against a double warning. Both
    `CodexAgent` and `register_agent` can warn about an unknown name, and
    `CodexAgent` filters before registering precisely so only the first does.
    """
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    agent = _agent(tmp_path, fake_codex, mcp=lambda: server, mcp_tools=["WiringAlpha", "NoSuchTool"])
    try:
        with caplog.at_level(logging.WARNING):
            await agent._thread_for("chat-1", turn_id="t1")

        (kwargs,) = fake_codex.instances[0].thread_start_calls
        config = kwargs["config"]
        # The exact nesting, pinned by shape and not only by lookup: a stray key
        # here is a config Codex refuses to load at all (`bearer_token` is the
        # live example — "bearer_token is not supported for streamable_http").
        assert set(config) == {"mcp_servers"}
        assert set(config["mcp_servers"]) == {"bos-tools"}
        entry = config["mcp_servers"]["bos-tools"]
        assert set(entry) == {"url", "http_headers"}
        assert entry["url"] == server.url
        assert set(entry["http_headers"]) == {"Authorization"}

        granted = await _list_tools(entry["url"], entry["http_headers"]["Authorization"])
        resolved = agent.resolved_config
        assert granted == sorted(set(resolved["mcp_tools"]) - set(resolved["mcp_tools_unavailable"]))
        assert granted == ["WiringAlpha"]

        warnings = [r for r in caplog.records if "NoSuchTool" in r.getMessage()]
        assert len(warnings) == 1, "one warning per typo, from CodexAgent — register_agent must not warn again"
        assert "george" in warnings[0].getMessage(), "§7.5: the warning names the agent"
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_an_agent_with_no_mcp_tools_never_asks_for_a_server(tmp_path, fake_codex):
    """BEP 19 §3.1 and the lazy half of §7.7, on a real runtime for the first
    time: the difference between BOS binding a loopback port for every agent and
    only for the ones that asked. Layer 1-3 could only prove the accessor's own
    laziness, because `_FakeRuntime` never called it either way.
    """
    agent = _agent(tmp_path, fake_codex, mcp=_never_called)

    await agent._thread_for("chat-1", turn_id="t1")

    (kwargs,) = fake_codex.instances[0].thread_start_calls
    assert kwargs["config"] is None, "no override at all — not an empty mcp_servers table"


@pytest.mark.asyncio
async def test_a_config_naming_only_unknown_tools_warns_and_starts_no_server(tmp_path, fake_codex, caplog):
    """BEP 19 §7.5, both halves at once — and they are only both reachable
    because the skip rule is a predicate rather than something the server
    decides. `CodexAgent` asks `unregistered_tools` itself, so it can warn and
    *then* decline to build a server that would serve an empty tool list.
    """
    agent = _agent(tmp_path, fake_codex, mcp=_never_called, mcp_tools=["NoSuchTool"])

    with caplog.at_level(logging.WARNING):
        await agent._thread_for("chat-1", turn_id="t1")

    warnings = [r for r in caplog.records if "NoSuchTool" in r.getMessage()]
    assert len(warnings) == 1, "exactly one"
    assert "george" in warnings[0].getMessage(), "naming the agent"
    (kwargs,) = fake_codex.instances[0].thread_start_calls
    assert kwargs["config"] is None, "and no server: Codex is not told to connect to anything"


@pytest.mark.asyncio
async def test_a_tool_the_host_does_not_have_is_reported_without_a_server_or_a_turn(tmp_path, fake_codex, host_tools):
    """`resolved_config` is what `inspect` reads, and it must answer on a cold
    agent: no MCP server, no client, no `codex app-server` child."""
    agent = _agent(tmp_path, fake_codex, mcp=_never_called, mcp_tools=["WiringAlpha", "NoSuchTool"])

    assert agent.resolved_config["mcp_tools_unavailable"] == ["NoSuchTool"]
    assert fake_codex.instances == [], "reading the resolved config must not build a client"


async def _inspect_george(tmp_path) -> dict:
    """`boscli inspect agent george` against a real `CodexAgent`, built by the
    harness and never run. No `fake_codex`: nothing here reaches
    `_CODEX_FACTORY`, and that is half of what these two tests assert.
    """
    from bos.cli.commands.inspect import _agent_capabilities

    (tmp_path / "services").mkdir()
    ws = _write_workspace(
        tmp_path,
        '[agents.george]\n_parent = "codex"\ncwd = "services"\npermission = "workspace-write"\n'
        'mcp_tools = ["WiringAlpha", "NoSuchTool"]\n',
    )
    ws.resolve_agents()
    ws.bootstrap_platform()
    return await _agent_capabilities(ws, "george")


@pytest.mark.asyncio
async def test_inspect_reports_the_resolved_config_of_a_real_runtime(tmp_path, host_tools):
    """BEP 19 §8.2, three carry-forwards at once. Against `_FakeRuntime` this
    branch looked fine, because that double's `resolved_config` *is* its raw
    cfg; against a real `CodexAgent` the old `getattr(agent, "cfg", {})` found
    nothing at all, so `cwd` reported "." and `permission` reported None.
    """
    info = await _inspect_george(tmp_path)

    assert info["runtime"] == "codex"
    assert info["cwd"] == str((tmp_path / "services").resolve()), "the resolved absolute cwd, not the configured one"
    assert info["permission"] == "workspace-write"
    assert info["mcp_tools"] == ["NoSuchTool", "WiringAlpha"]
    assert info["mcp_tools_unavailable"] == ["NoSuchTool"]


@pytest.mark.asyncio
async def test_inspect_shows_the_unavailable_tools_in_text_mode(tmp_path, host_tools):
    """Text mode is the default operator view, and is where a previous version
    of this renderer hid every field it had just learned to report (§4.3)."""
    from rich.console import Console

    from bos.cli.commands.inspect import _render_agent

    info = await _inspect_george(tmp_path)

    buffer = io.StringIO()
    _render_agent(Console(file=buffer, width=200), info)
    output = buffer.getvalue()

    assert str((tmp_path / "services").resolve()) in output
    assert "workspace-write" in output
    # On the warning line specifically, not merely somewhere in the output: the
    # `mcp_tools` line already lists every requested name, unavailable ones
    # included, so a bare `"NoSuchTool" in output` passes with the new line
    # deleted. That is the exact shape of the bug this assertion guards.
    assert [line for line in output.splitlines() if "NoSuchTool" in line and "no such tool is registered" in line]
