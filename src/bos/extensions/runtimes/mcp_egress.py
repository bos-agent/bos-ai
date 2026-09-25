"""One loopback MCP server exposing selected BOS tools to both runtimes (BEP 19 §3.8).

Streamable HTTP, not stdio and not Claude's in-process SDK server: Claude Code
accepts ``{"type": "http", ...}`` and Codex accepts an ``[mcp_servers.<n>]``
with a ``url``, so one server and one authorization check serve both. A stdio
shim would still need IPC back into this process, and HTTP is that IPC.

Built on ``mcp.server.lowlevel.Server`` rather than ``MCPServer``:
``MCPServer.add_tool`` derives a tool's input schema from the Python function's
signature, so the ``**kwargs`` closure needed to forward to ``ep_tool.invoke``
would advertise a tool that takes no arguments. The lowlevel server returns
``types.Tool`` objects whose ``input_schema`` is supplied directly — the host
tool's real JSON schema — and lets ``tools/list`` itself be scoped to the
calling agent, which no HTTP middleware can do: a middleware sees a request and
a response stream, never the MCP-level tool list.

Every third-party import is deferred into the method that needs it, so importing
this module never by itself requires ``mcp``, ``uvicorn`` or ``starlette``. The
harness reaches the class by dotted path, because ``bos.core`` may not name an
outer ring at import time (BEP 13 §3.1) and loads on a base install with no extras.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotations only — `from __future__ import annotations` keeps these unevaluated
    import mcp.types as types
    from mcp.server import ServerRequestContext
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_BEARER = "Bearer "
_NO_GRANT: tuple[str, frozenset[str]] = ("<unauthenticated>", frozenset())


def unregistered_tools(names: Sequence[str]) -> tuple[str, ...]:
    """Which of *names* the global ``ep_tool`` registry has no entry for.

    The skip rule of BEP 19 §3.8, in one place because two callers need the
    same answer and must not drift: :meth:`BosToolMcpServer.register_agent`,
    which warns and skips exactly these, and an external runtime's
    ``resolved_config``, which reports them so ``boscli inspect`` can show an
    operator that an agent asks for a tool the host does not have.

    A free function, not a method, because of that second caller: ``inspect``
    builds an agent and runs no turn, and a runtime builds its client — and
    with it its MCP registration — lazily on the first turn (§3.1), so there
    is no server to ask. Comparing what an agent requested against what a
    server granted would report nothing at all on that path.

    Order- and duplicate-preserving, so a caller's warnings line up one for
    one with the names it was given.
    """
    from bos.core.contract import ep_tool

    return tuple(name for name in names if not ep_tool.has(name))


class BosToolMcpServer:
    """Per-harness MCP endpoint over the global ``ep_tool`` registry.

    Tools are resolved by name at registration time. Which agent may see which
    is decided here, by bearer token, because the registry itself is
    process-global and cannot scope per workspace (BEP 19 §3.12).
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._port: int | None = None
        self._allowed: dict[str, tuple[str, frozenset[str]]] = {}  # token -> (agent, tools)
        # (uvicorn server, its main loop) — set together by start(), cleared
        # together by aclose(), so neither can outlive the other.
        self._running: tuple[Any, asyncio.Future[None]] | None = None

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("BosToolMcpServer is not started.")
        return f"http://{self._host}:{self._port}/mcp"

    def register_agent(self, agent_name: str, tools: Sequence[str]) -> str:
        """Grant *agent_name* the named tools and return its bearer token.

        Returns only the token: what was *skipped* is reported by
        :func:`unregistered_tools`, which the caller can ask without a server.
        """
        missing = unregistered_tools(tools)
        for name in missing:
            logger.warning(
                "Agent %r lists mcp_tools=%r, which is not a registered tool; skipping.",
                agent_name,
                name,
            )
        token = secrets.token_urlsafe(32)
        self._allowed[token] = (agent_name, frozenset(tools) - frozenset(missing))
        return token

    def _grant(self, headers: Mapping[str, str] | None) -> tuple[str, frozenset[str]] | None:
        """``(agent, tools)`` for this caller's bearer token, or None for no grant.

        *headers* are the HTTP headers behind this message; None (no request
        behind it at all) grants nothing, as does any header set without a
        bearer token this server issued.
        """
        value = (headers or {}).get("authorization", "")
        token = value[len(_BEARER) :] if value.startswith(_BEARER) else ""
        return self._allowed.get(token)

    async def start(self) -> None:
        """Bind an ephemeral loopback port and serve. Idempotent."""
        if self._running is not None:
            return

        import uvicorn
        from mcp.server.lowlevel import Server

        mcp_server = Server("bos-tools", on_list_tools=self._on_list_tools, on_call_tool=self._on_call_tool)
        app = self._gate(
            mcp_server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host=self._host)
        )

        # Deliberately not ``uvicorn.Server.serve()``: it installs SIGINT/SIGTERM
        # handlers on the main thread, which belongs to the host embedding BOS
        # (and, under pytest, to pytest). ``lifespan="on"`` is required — the MCP
        # app starts its session manager there.
        config = uvicorn.Config(
            app,
            host=self._host,
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="on",
            # A runtime holding an SSE stream open must not make harness teardown hang.
            timeout_graceful_shutdown=5,
        )
        server = uvicorn.Server(config)
        config.load()
        server.lifespan = config.lifespan_class(config)
        await server.startup()
        self._running = (server, asyncio.ensure_future(server.main_loop()))
        self._port = server.servers[0].sockets[0].getsockname()[1]

    def _gate(self, app: ASGIApp) -> ASGIApp:
        """Refuse any request without a known bearer token, before MCP sees it.

        Fail-closed on the scope type: only ``lifespan`` — which is how the MCP
        session manager starts, and which carries no headers — bypasses the
        check. Everything else must present a grant, including the ``websocket``
        scope uvicorn will happily deliver even though this app registers no
        websocket route.

        Raw ASGI, not ``BaseHTTPMiddleware``: the latter buffers through a
        wrapping response and does not get along with the transport's SSE
        streams.
        """
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse

        async def gated(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "lifespan" and self._grant(Headers(scope=scope)) is None:
                if scope["type"] == "websocket":
                    # A 401 body is not a thing on this protocol; 1008 is "policy violation".
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            await app(scope, receive, send)

        return gated

    async def _on_list_tools(
        self,
        ctx: ServerRequestContext[Any, Request],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        """Exactly the calling agent's tools — the listing *is* the allowlist."""
        import mcp.types as types

        from bos.core.contract import ep_tool

        _agent, allowed = self._grant(_headers_of(ctx)) or _NO_GRANT
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=ext.description or name,
                    input_schema=ep_tool.build_openai_schema(ext)["function"]["parameters"],
                )
                for name, ext in ep_tool._extensions.items()
                if name in allowed
            ]
        )

    async def _on_call_tool(
        self,
        ctx: ServerRequestContext[Any, Request],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        """Run one host tool, in this process, under this agent's allowlist."""
        import mcp.types as types

        from bos.core.contract import ep_tool

        agent, allowed = self._grant(_headers_of(ctx)) or _NO_GRANT
        if params.name not in allowed:
            logger.warning("Agent %r may not call %r; refusing.", agent, params.name)
            return _tool_error(f"Tool {params.name!r} is not available to this agent.")
        try:
            output = await ep_tool.invoke(params.name, dict(params.arguments or {}))
        except Exception as exc:
            # A host tool that raises is this call's error, not the server's: it
            # becomes an isError result and the connection — and every later call
            # on it, for this agent and every other — stays usable.
            logger.warning("Host tool %r raised for agent %r", params.name, agent, exc_info=True)
            return _tool_error(f"{type(exc).__name__}: {exc}")
        return types.CallToolResult(content=[types.TextContent(type="text", text=output)])

    async def aclose(self) -> None:
        """Stop serving and release the port. A server that never started is a no-op."""
        running, self._running, self._port = self._running, None, None
        if running is None:
            return
        server, serving = running
        server.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)
        await server.shutdown()


def _headers_of(ctx: ServerRequestContext[Any, Request]) -> Mapping[str, str] | None:
    """The HTTP headers behind this MCP message, or None when there is no request.

    ``mcp`` frames every streamable-HTTP message with the Starlette request
    (``mcp/server/streamable_http.py``) and ``ServerRequestContext.request``
    carries it into the handler. That attribute is marked transitional upstream
    ("TODO(L54): remove for Context rework"), so this is the one place that
    reads it — annotated, so a type check catches the day it moves, and
    returning None (deny) if a transport ever leaves it unset.
    """
    return ctx.request.headers if ctx.request is not None else None


def _tool_error(message: str) -> types.CallToolResult:
    import mcp.types as types

    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)
