"""BEP 19 §3.8: one streamable-HTTP MCP server exposing selected ep_tool tools."""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture
def host_tools():
    """Two host tools and one that raises, registered in the global ep_tool.

    Yields the list every tool appends to, so a test can assert a denied call
    never reached the host implementation.
    """
    from bos.core.contract import ep_tool

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    calls: list[str] = []

    @ep_tool(name="EgressAlpha", description="Alpha", parameters=schema)
    async def alpha(x: str) -> str:
        calls.append("EgressAlpha")
        return f"alpha:{x}"

    @ep_tool(name="EgressBeta", description="Beta", parameters=schema)
    async def beta(x: str) -> str:
        calls.append("EgressBeta")
        return f"beta:{x}"

    @ep_tool(name="EgressBoom", description="Boom", parameters=schema)
    async def boom(x: str) -> str:
        calls.append("EgressBoom")
        raise RuntimeError("host tool exploded")

    yield calls
    for name in ("EgressAlpha", "EgressBeta", "EgressBoom"):
        ep_tool._extensions.pop(name, None)


@contextlib.asynccontextmanager
async def _session(url: str, token: str):
    """An initialized MCP session over streamable HTTP, carrying *token* as a bearer."""
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _call(url: str, token: str, tool: str, arguments: dict):
    async with _session(url, token) as session:
        return await session.call_tool(tool, arguments)


async def _list(url: str, token: str):
    async with _session(url, token) as session:
        return sorted(t.name for t in (await session.list_tools()).tools)


@pytest.mark.asyncio
async def test_an_agent_sees_only_its_own_tools(host_tools):
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        token_a = server.register_agent("george", ["EgressAlpha"])
        token_b = server.register_agent("martha", ["EgressBeta"])

        assert await _list(server.url, token_a) == ["EgressAlpha"]
        assert await _list(server.url, token_b) == ["EgressBeta"]
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_overlapping_allowlists_do_not_collide(host_tools):
    """Review Focus 4: a shared tool is legitimate; the other agent's stays hidden."""
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        token_a = server.register_agent("george", ["EgressAlpha", "EgressBeta"])
        token_b = server.register_agent("martha", ["EgressBeta"])

        assert await _list(server.url, token_a) == ["EgressAlpha", "EgressBeta"]
        assert await _list(server.url, token_b) == ["EgressBeta"]
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_a_tool_call_reaches_the_host_implementation(host_tools):
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        token = server.register_agent("george", ["EgressAlpha"])
        result = await _call(server.url, token, "EgressAlpha", {"x": "hi"})
        assert "alpha:hi" in str(result.content[0].text)
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_the_advertised_schema_is_the_host_tools_own(host_tools):
    """The ep_tool JSON schema reaches the runtime, not one inferred from a **kwargs closure."""
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        token = server.register_agent("george", ["EgressAlpha"])
        async with _session(server.url, token) as session:
            tool = (await session.list_tools()).tools[0]
        assert tool.description == "Alpha"
        assert tool.input_schema == {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_a_raising_host_tool_is_an_mcp_error_not_a_dead_server(host_tools):
    """Review Focus 3: the server and later calls must survive a bad tool."""
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        token = server.register_agent("george", ["EgressBoom", "EgressAlpha"])
        boom = await _call(server.url, token, "EgressBoom", {"x": "hi"})
        assert boom.is_error is True
        ok = await _call(server.url, token, "EgressAlpha", {"x": "hi"})
        assert ok.is_error is not True
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_a_tool_outside_the_allowlist_is_refused_without_executing(host_tools):
    """The authorization check, not merely the listing: a denied call must not run."""
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        token_b = server.register_agent("martha", ["EgressBeta"])
        denied = await _call(server.url, token_b, "EgressAlpha", {"x": "hi"})
        assert denied.is_error is True
        assert "EgressAlpha" in str(denied.content[0].text)
        assert host_tools == []  # the host implementation never ran
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_a_bad_token_is_rejected(host_tools):
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        server.register_agent("george", ["EgressAlpha"])
        with pytest.raises(Exception):
            await _list(server.url, "not-a-real-token")
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_a_request_with_no_authorization_header_is_refused_over_http(host_tools):
    """The absent-header case on the wire, not through a double: Starlette's own Headers."""
    import httpx

    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    try:
        await server.start()
        server.register_agent("george", ["EgressAlpha"])
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                server.url,
                json=body,
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            )
        assert response.status_code == 401
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_a_request_with_no_usable_credential_is_granted_nothing(host_tools):
    """The deny-by-default arms: no request at all, and a header that is not a bearer."""
    from starlette.datastructures import Headers

    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    token = server.register_agent("george", ["EgressAlpha"])

    assert server._grant(None) is None
    assert server._grant(Headers({})) is None
    assert server._grant(Headers({"authorization": token})) is None  # no "Bearer " prefix
    assert server._grant(Headers({"authorization": f"Bearer {token}"})) == ("george", frozenset({"EgressAlpha"}))
    # Starlette's Headers are case-insensitive; the wire spelling must work too.
    assert server._grant(Headers({"Authorization": f"Bearer {token}"})) == ("george", frozenset({"EgressAlpha"}))


@pytest.mark.asyncio
async def test_a_websocket_scope_is_closed_rather_than_waved_through(host_tools):
    """Fail-closed on scope type: only `lifespan` may skip the check (Review item 2)."""
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    reached: list[str] = []
    sent: list[dict] = []

    async def _inner(scope, receive, send):
        reached.append(scope["type"])

    async def _send(message):
        sent.append(message)

    async def _receive():
        raise AssertionError("a refused scope must not be read from")

    server = BosToolMcpServer()
    await server._gate(_inner)({"type": "websocket", "headers": []}, _receive, _send)

    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert reached == []  # the app never saw it


@pytest.mark.asyncio
async def test_an_unmatched_tool_name_warns_and_is_skipped(host_tools, caplog):
    import logging

    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    await server.start()
    try:
        with caplog.at_level(logging.WARNING):
            token = server.register_agent("george", ["EgressAlpha", "NoSuchTool"])
        assert await _list(server.url, token) == ["EgressAlpha"]
        warnings = [r for r in caplog.records if "NoSuchTool" in r.message]
        assert len(warnings) == 1
        assert "george" in warnings[0].message
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_the_port_is_ephemeral_and_released_on_close(host_tools):
    import socket
    from urllib.parse import urlparse

    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    try:
        await server.start()
        port = urlparse(server.url).port
        assert port and port > 0
    finally:
        await server.aclose()

    with socket.socket() as probe:  # rebindable once released
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_two_servers_get_different_ports(host_tools):
    from urllib.parse import urlparse

    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    first, second = BosToolMcpServer(), BosToolMcpServer()
    try:  # aclose before start is a no-op, so start inside
        await first.start()
        await second.start()
        assert urlparse(first.url).port != urlparse(second.url).port
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_start_is_idempotent(host_tools):
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    try:
        await server.start()
        url = server.url
        await server.start()
        assert server.url == url
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_the_url_of_an_unstarted_server_is_an_error_not_a_guess():
    from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

    server = BosToolMcpServer()
    with pytest.raises(RuntimeError):
        _ = server.url
    await server.aclose()  # closing one that never started is a no-op


@pytest.mark.asyncio
async def test_the_harness_owns_one_lazily_created_server(tmp_path):
    """BEP 19 §3.8: one per harness, never started on its own, reaped by _aclose."""
    from bos.core.harness import AgentHarness

    async with AgentHarness(workspace=tmp_path) as harness:
        assert harness._tool_mcp_server is None  # nothing built until an agent asks
        server = harness._ensure_tool_mcp_server()
        assert harness._ensure_tool_mcp_server() is server  # one per harness
        # Index 0, because __aexit__ closes reversed(_owned): the tool server must
        # outlive every external runtime that may still be calling through it.
        assert harness._owned[0] is server
        await server.start()
        port = server.url

    assert server._running is None  # harness teardown closed it
    assert port
