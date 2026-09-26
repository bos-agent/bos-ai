"""BEP 19 Layer 4b, Task 10: the host's own `ep_tool`s reaching Claude Code over MCP (§3.8).

The Codex half is tests/test_codex_mcp_wiring.py, and the rules it pins hold here too: the egress
is built lazily, on the agent's first turn, and only when `mcp_tools` names a tool the host has —
decided by `mcp_egress.unregistered_tools`, which also drives the one warning per unknown name
(§7.5). What differs is how the CLI is pointed at the server: one client per turn, each with an
`mcp_servers` entry named `bos-tools` whose bearer is a `${VAR}` placeholder, the value only in
that client's own environment, under a name of its own (fact 7). And the confinement (§3.5.3) must
let through exactly the tools BOS's server granted — by their exact names as the CLI spells them,
not the `mcp__bos-tools__` prefix, which other servers' names can produce: the hook passes them with
no decision and denies any other MCP tool, `can_use_tool` allows them where the mode asks it, and
`strict_mcp_config` keeps every other MCP server out of the session. A turn whose CLI reports BOS's
server as not connected logs a WARNING.

The real-CLI tests drive the bundled CLI against the fake Messages API and a real
`BosToolMcpServer`, as tests/test_claude_code_confinement.py does, through `ClaudeCodeAgent.run()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny, SystemMessage
from conftest import FakeClaudeClient
from test_claude_code_confinement import _LEVELS, _tu
from test_claude_code_runtime import _command, _point_the_cli_at, _turn
from test_claude_code_vendor_facts import _tool_results
from test_codex_mcp_wiring import _list_tools, _never_called

from bos.core.defaults.structured_validator import JsonSchemaValidator
from bos.extensions.runtimes import claude_code
from bos.extensions.runtimes.claude_code import ClaudeCodeAgent
from bos.extensions.runtimes.mcp_egress import BosToolMcpServer

_TOOLS = ("GrantedTool", "OtherTool", "OperatorTool")
# `ep_tool` names the CLI rewrites, each beside the name it gives the tool (measured against CLI
# 2.1.281): a dot becomes `_`; a character beyond U+FFFF becomes `__`, one `_` per UTF-16 code unit;
# a name starting `claude.ai ` also has its runs of `_` collapsed; a double underscore is kept.
_REWRITTEN = {
    "desk.create": "mcp__bos-tools__desk_create",
    "x\U0001f600y": "mcp__bos-tools__x__y",
    "claude.ai  tool..x": "mcp__bos-tools__claude_ai_tool_x",
    "a__b": "mcp__bos-tools__a__b",
}
_BEARER = re.compile(r"Bearer \$\{(BOS_MCP_BEARER_[0-9a-f]{32})\}")


@pytest.fixture
def host_tools():
    """Host tools in the global `ep_tool` registry, each recording its own calls in the list this
    yields, removed again after. The agents below expose `GrantedTool`; `OtherTool` is registered
    and not exposed; `OperatorTool` is what a second MCP server — the operator's own, or a
    repository's — serves; `_REWRITTEN`'s are names the CLI spells differently."""
    from bos.core.contract import ep_tool

    calls: list[str] = []
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    def make(name: str) -> Any:
        async def tool(x: str) -> str:
            calls.append(name)
            return f"{name} result for {x}"

        return tool

    for name in (*_TOOLS, *_REWRITTEN):
        ep_tool(name=name, description=f"The {name} tool", parameters=schema)(make(name))
    yield calls
    for name in (*_TOOLS, *_REWRITTEN):
        ep_tool._extensions.pop(name, None)


def _agent(tmp_path: Path, *, mcp: Any, **cfg: Any) -> ClaudeCodeAgent:
    """A `ClaudeCodeAgent` with a caller-supplied `mcp` accessor, in `<tmp_path>/ws`.
    `auth="api_key"`, so the subscription preflight does not refuse the fake's key."""
    cfg.setdefault("permission", "read-only")
    cfg.setdefault("auth", "api_key")
    cfg.setdefault("cwd", "ws")
    (tmp_path / "ws").mkdir(exist_ok=True)
    return ClaudeCodeAgent(
        kind="george",
        cfg=cfg,
        chat_store=None,
        workspace=tmp_path,
        mcp=mcp,
        structured_validator=JsonSchemaValidator(),
    )


def _bearer(options: Any) -> tuple[str, str]:
    """(variable name, token) of the bearer *options* point the CLI at: the name out of the
    placeholder in the `bos-tools` entry, the value out of `options.env`."""
    match = _BEARER.fullmatch(options.mcp_servers["bos-tools"]["headers"]["Authorization"])
    assert match, options.mcp_servers
    return match.group(1), options.env[match.group(1)]


def _connection_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The WARNINGs BOS logs when the CLI reports its MCP server as not connected."""
    return [r for r in caplog.records if r.levelno >= logging.WARNING and "BOS's MCP server" in r.getMessage()]


def _armed(built: list[FakeClaudeClient]) -> Any:
    """A client factory whose every client streams one ordinary turn — `fake_claude.arm` arms
    only the next client, and concurrent turns build two."""

    def factory(options: Any) -> FakeClaudeClient:
        client = FakeClaudeClient(options)
        client.messages = _turn()
        built.append(client)
        return client

    return factory


# ── What each client is given (fake client) ────────────────────────────────────


@pytest.mark.asyncio
async def test_each_client_names_bos_tools_with_a_bearer_only_its_own_environment_carries(
    tmp_path, fake_claude, host_tools, caplog
):
    """The chain across two turns of one agent, so two clients: the parsed `mcp_tools` ->
    `register_agent` -> the token -> each client's `mcp_servers` entry and environment -> a
    `tools/list` that answers with exactly the granted tools.

    Fact 7 is why the token rides the environment: the SDK puts `mcp_servers` on the CLI's command
    line, which other local users can read, and the CLI expands the placeholder from its own
    environment. The variable's name is fresh per client, for Codex's reason — a name nothing can
    guess is one no other MCP server's entry can name to be handed the token — and the variable is
    merged into BOS's `env`, whose overrides must survive it (§3.12). One agent, one grant: the
    token is the same in both.

    `mcp_tools` also names a tool the host does not have, so the grant is a strict subset, and it
    is warned about once — by the runtime, not a second time by `register_agent` (§7.5)."""
    server = BosToolMcpServer()
    agent = _agent(tmp_path, mcp=lambda: server, mcp_tools=["GrantedTool", "NoSuchTool"])
    try:
        with caplog.at_level(logging.WARNING):
            for chat_id in ("chat-1", "chat-2"):
                fake_claude.arm(messages=_turn())
                await agent.run(chat_id, "go")

        bearers = []
        for client in fake_claude.instances:
            options = client.options
            name, token = _bearer(options)
            placeholder = f"Bearer ${{{name}}}"
            assert options.mcp_servers == {
                "bos-tools": {"type": "http", "url": server.url, "headers": {"Authorization": placeholder}}
            }
            assert options.env == {**claude_code._INHERITED_ENV_OVERRIDES, name: token}, "merged, one variable"
            command = " ".join(_command(options))
            assert f"${{{name}}}" in command, "the placeholder rides the command line"
            assert token not in command, "the token does not"
            bearers.append((name, token))
        (first_name, first_token), (second_name, second_token) = bearers
        assert first_name != second_name, "a variable of its own per client"
        assert first_token == second_token, "one grant per agent"

        resolved = agent.resolved_config
        granted = await _list_tools(server.url, first_token)
        assert granted == sorted(set(resolved["mcp_tools"]) - set(resolved["mcp_tools_unavailable"])) == ["GrantedTool"]
        warnings = [record for record in caplog.records if "NoSuchTool" in record.getMessage()]
        assert len(warnings) == 1, "one warning per unknown name, across turns"
        assert "george" in warnings[0].getMessage(), "§7.5: naming the agent"
        assert _connection_warnings(caplog) == [], "an init message with no server list tells BOS nothing"
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_an_agent_with_no_mcp_tools_never_asks_for_a_server(tmp_path, fake_claude):
    """BEP 19 §3.1: the accessor builds the server when called, so an agent that exposes nothing
    must never call it — and its clients name no MCP server and carry no bearer."""
    agent = _agent(tmp_path, mcp=_never_called)
    fake_claude.arm(messages=_turn())

    await agent.run("chat-1", "go")

    (client,) = fake_claude.instances
    assert client.options.mcp_servers == {}
    assert client.options.env == dict(claude_code._INHERITED_ENV_OVERRIDES)


@pytest.mark.asyncio
async def test_a_config_naming_only_unknown_tools_warns_once_and_starts_no_server(tmp_path, fake_claude, caplog):
    """BEP 19 §7.5, both halves, across two turns: exactly one warning naming the agent, and no
    server — the runtime asks `unregistered_tools` itself, so it can warn and then decline to build
    a server that would serve nothing."""
    agent = _agent(tmp_path, mcp=_never_called, mcp_tools=["NoSuchTool"])

    with caplog.at_level(logging.WARNING):
        for chat_id in ("chat-1", "chat-2"):
            fake_claude.arm(messages=_turn())
            await agent.run(chat_id, "go")

    warnings = [record for record in caplog.records if "NoSuchTool" in record.getMessage()]
    assert len(warnings) == 1 and "george" in warnings[0].getMessage()
    assert [client.options.mcp_servers for client in fake_claude.instances] == [{}, {}]


@pytest.mark.asyncio
async def test_the_connection_warning_is_logged_once_per_turn(tmp_path, fake_claude, host_tools, caplog):
    """Every native turn's init message repeats the MCP servers' status — a schema retry's included
    (measured) — so BOS reads a turn's first, and warns once however many native turns it runs. The
    turn still completes: the warning does not fail it."""
    server = BosToolMcpServer()
    agent = _agent(tmp_path, mcp=lambda: server, mcp_tools=["GrantedTool"])
    failed = {"mcp_servers": [{"name": "bos-tools", "status": "failed", "source": "dynamic"}]}
    rounds = [_turn("not json"), _turn('{"ok": true}')]
    for messages in rounds:
        messages[0] = SystemMessage(subtype="init", data={**messages[0].data, **failed})
    fake_claude.arm(messages=[message for messages in rounds for message in messages])
    try:
        with caplog.at_level(logging.WARNING):
            result = await agent.run("chat-1", "go", schema={"type": "object"})
    finally:
        await server.aclose()

    assert result.structured and result.output == {"ok": True}
    assert len(_connection_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_concurrent_first_turns_register_one_grant(tmp_path, monkeypatch, host_tools):
    """Two chats' first turns at once still register the agent once: the build holds a lock, so
    the second waits for the first's grant rather than starting the server again beside it and
    registering a second token."""
    registrations: list[str] = []

    class Counting(BosToolMcpServer):
        def register_agent(self, agent_name: str, tools: Any) -> str:
            registrations.append(agent_name)
            return super().register_agent(agent_name, tools)

    built: list[FakeClaudeClient] = []
    monkeypatch.setattr(claude_code, "_CLIENT_FACTORY", _armed(built))
    server = Counting()
    agent = _agent(tmp_path, mcp=lambda: server, mcp_tools=["GrantedTool"])
    try:
        await asyncio.gather(agent.run("chat-1", "go"), agent.run("chat-2", "go"))
    finally:
        await server.aclose()

    assert registrations == ["george"]
    assert len({_bearer(client.options)[1] for client in built}) == 1


# ── Exactly the granted tools pass the confinement (§3.5.3) ───────────────────

# The names the CLI would give tools that are not BOS's granted ones, each beside what produces it.
# The CLI names a tool `mcp__<server>__<tool>` after rewriting each part (among other rules, every
# ASCII character outside [a-zA-Z0-9_-] becomes `_`), so the first three entries — four server names —
# all start `mcp__bos-tools__`, the prefix the narrowing used to trust.
_NEAR_MISSES = {
    "mcp__bos-tools___GrantedTool": "a server named `bos-tools_`, or `bos-tools.`, which the CLI rewrites to it",
    "mcp__bos-tools__evil__GrantedTool": "a server named `bos-tools__evil`",
    "mcp__bos-tools__x__GrantedTool": "a server named `bos-tools..x`, which the CLI rewrites to `bos-tools__x`",
    "mcp__evil__x__bos-tools__GrantedTool": "a tool whose name contains `__bos-tools__`, on a server `evil`",
    "mcp__BOS-TOOLS__GrantedTool": "a case variant of BOS's server",
    "mcp__bos_tools__GrantedTool": "Codex's spelling, the hyphen folded",
    "mcp__bos-tools__OtherTool": "a tool of BOS's own server name that BOS did not grant",
    "mcp__operator__OperatorTool": "another server altogether",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["read-only", "workspace-write", "full-access"])
async def test_the_hook_and_can_use_tool_let_through_exactly_the_granted_tools(
    tmp_path, monkeypatch, host_tools, permission
):
    """The hook and `can_use_tool` of a client built from the agent's grant allow the exact names of
    the tools BOS granted, spelled as the CLI spells them (`_mcp_egress`), and nothing that merely
    looks like one (`_NEAR_MISSES`). The hook passes BOS's tool with no decision at every level, so
    `can_use_tool` is asked where the mode prompts, and allows it; it denies every near miss at every
    level — at `full-access` the only layer, since `bypassPermissions` never asks `can_use_tool` and
    BOS installs none there — and `can_use_tool` denies them too, as a backstop. A client built with
    no grant allows no MCP tool at all."""
    monkeypatch.setattr(claude_code, "_bash_sandbox_unavailable", lambda platform: None)
    server = BosToolMcpServer()
    agent = _agent(tmp_path, mcp=lambda: server, permission=permission, mcp_tools=["GrantedTool"])
    try:
        granted, ungranted = agent._options(await agent._mcp_egress()), agent._options()
    finally:
        await server.aclose()

    async def hook_decision(options: Any, tool: str) -> str | None:
        (hook,) = options.hooks["PreToolUse"][0].hooks
        output = await hook({"tool_name": tool, "tool_input": {"x": "hi"}}, None, None)
        return output.get("hookSpecificOutput", {}).get("permissionDecision")

    assert await hook_decision(granted, "mcp__bos-tools__GrantedTool") is None, "no decision: can_use_tool answers"
    assert await hook_decision(ungranted, "mcp__bos-tools__GrantedTool") == "deny", "no grant, no MCP tool"
    for near_miss, source in _NEAR_MISSES.items():
        assert await hook_decision(granted, near_miss) == "deny", source
    if permission == "full-access":
        assert granted.can_use_tool is None
        return
    assert isinstance(await granted.can_use_tool("mcp__bos-tools__GrantedTool", {}, None), PermissionResultAllow)
    assert isinstance(await ungranted.can_use_tool("mcp__bos-tools__GrantedTool", {}, None), PermissionResultDeny)
    for near_miss, source in _NEAR_MISSES.items():
        assert isinstance(await granted.can_use_tool(near_miss, {}, None), PermissionResultDeny), source


# ── Against the real CLI ──────────────────────────────────────────────────────


def _recording(built: list[Any], asked: list[str]) -> Any:
    """A client factory that builds the real client, keeps the options BOS built it from, and records
    each tool BOS's `can_use_tool` is asked about before it answers."""

    def factory(options: Any) -> ClaudeSDKClient:
        built.append(options)
        answer = options.can_use_tool
        if answer is not None:

            async def recording(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
                asked.append(tool_name)
                return await answer(tool_name, tool_input, context)

            options = replace(options, can_use_tool=recording)
        return ClaudeSDKClient(options)

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", _LEVELS)
async def test_the_model_calls_the_exposed_tool_and_gets_the_hosts_result_against_the_real_cli(
    tmp_path, fake_anthropic, monkeypatch, host_tools, caplog, permission
):
    """BEP 19 §7.22 against the real CLI with the fake model, at every level: the CLI offers the
    exposed tool as `mcp__bos-tools__GrantedTool`, the model calls it, the host's own tool runs in
    this process and its result reaches the model. `OtherTool` is a registered `ep_tool` the agent
    does not expose: BOS's server does not list it for this agent's token, so the CLI has no such
    tool and the host tool never runs.

    Under `read-only` the call completes too — §3.5's "`permission` bounds the filesystem, not the
    tools": `tools=` does not filter MCP tools (the CLI offers the server's tools beside `Read`),
    and the call reaches `can_use_tool`, which allows it, where the mode asks — `default` and
    `acceptEdits` — while `bypassPermissions` asks nobody. The unknown tool is refused before any
    permission check. The token appears in no log line."""
    _point_the_cli_at(fake_anthropic, tmp_path, monkeypatch)
    built: list[Any] = []
    asked: list[str] = []
    monkeypatch.setattr(claude_code, "_CLIENT_FACTORY", _recording(built, asked))
    server = BosToolMcpServer()
    agent = _agent(tmp_path, mcp=lambda: server, permission=permission, mcp_tools=["GrantedTool"])
    fake_anthropic.script([
        [_tu("tu_granted", "mcp__bos-tools__GrantedTool", x="hi"), _tu("tu_other", "mcp__bos-tools__OtherTool", x="hi")]
    ])
    try:
        with caplog.at_level(logging.DEBUG):
            async with asyncio.timeout(90):
                await agent.run("chat-1", "use the tools", turn_id="t1")
    finally:
        await server.aclose()

    results = _tool_results(fake_anthropic)
    offered = {tool["name"] for tool in fake_anthropic.requests[0]["tools"]}
    assert host_tools == ["GrantedTool"], results
    assert "GrantedTool result for hi" in results["tu_granted"], results
    assert "mcp__bos-tools__GrantedTool" in offered and "mcp__bos-tools__OtherTool" not in offered, offered
    assert "No such tool available" in results["tu_other"], results
    assert asked == ([] if permission == "full-access" else ["mcp__bos-tools__GrantedTool"]), asked
    assert _connection_warnings(caplog) == [], "the server connected, and BOS says nothing"
    (options,) = built
    _, token = _bearer(options)
    assert not [record for record in caplog.records if token in record.getMessage()], "the token was logged"


# Server names whose tools the CLI names with BOS's prefix, keyed by the name it gives their
# `OperatorTool` (`bos-tools..x` is rewritten to `bos-tools__x`).
_COLLIDING = {
    "mcp__bos-tools__evil__OperatorTool": "bos-tools__evil",
    "mcp__bos-tools___OperatorTool": "bos-tools_",
    "mcp__bos-tools__x__OperatorTool": "bos-tools..x",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["repo .mcp.json", "operator user config"])
@pytest.mark.parametrize("strict", [True, False], ids=["bos-options", "control-without-strict"])
async def test_another_mcp_servers_tools_are_not_reachable_against_the_real_cli(
    tmp_path, fake_anthropic, monkeypatch, host_tools, where, strict
):
    """Other MCP servers — in a repository's `.mcp.json`, loaded under `setting_sources =
    ["project"]`, or the operator's own user-scope servers in the CLI's global config, loaded under
    `["user"]`: one called `operator`, a same-named `bos-tools` entry pointing elsewhere, and three
    whose tools the CLI names with BOS's `mcp__bos-tools__` prefix (`_COLLIDING`).

    With BOS's options, `strict_mcp_config` keeps all of them out: none is contacted, none of their
    tools is offered, and the model's calls find no such tool. The control flips only
    `strict_mcp_config`, to show that is not vacuous: then the CLI loads them and offers their tools
    — and the hook denies every call, the colliding names included, at `full-access`, where
    `bypassPermissions` asks `can_use_tool` nothing and the hook is the only layer. In both, BOS's
    own `bos-tools` entry keeps the name: the exposed tool is the one offered, and it runs."""
    _point_the_cli_at(fake_anthropic, tmp_path, monkeypatch)
    listed: list[list[str]] = []

    class Operator(BosToolMcpServer):
        async def _on_list_tools(self, ctx: Any, params: Any) -> Any:
            result = await super()._on_list_tools(ctx, params)
            listed.append(sorted(tool.name for tool in result.tools))
            return result

    bos, operator = BosToolMcpServer(), Operator()
    await operator.start()
    servers = {
        name: {
            "type": "http",
            "url": operator.url,
            "headers": {"Authorization": f"Bearer {operator.register_agent(name, ['OperatorTool'])}"},
        }
        for name in ("operator", "bos-tools", *_COLLIDING.values())
    }
    (tmp_path / "ws").mkdir()
    if where == "repo .mcp.json":
        (tmp_path / "ws" / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
        sources = ["project"]
    else:
        (tmp_path / "claude-config" / ".claude.json").write_text(json.dumps({"mcpServers": servers}))
        sources = ["user"]
    if not strict:
        monkeypatch.setattr(
            claude_code, "_CLIENT_FACTORY", lambda options: ClaudeSDKClient(replace(options, strict_mcp_config=False))
        )
    agent = _agent(
        tmp_path, mcp=lambda: bos, permission="full-access", setting_sources=sources, mcp_tools=["GrantedTool"]
    )
    others = ["mcp__operator__OperatorTool", *_COLLIDING]
    fake_anthropic.script([
        [
            _tu("tu_granted", "mcp__bos-tools__GrantedTool", x="hi"),
            *(_tu(f"tu_other_{i}", name, x="hi") for i, name in enumerate(others)),
        ]
    ])
    try:
        async with asyncio.timeout(90):
            await agent.run("chat-1", "use the tools", turn_id="t1")
    finally:
        await bos.aclose()
        await operator.aclose()

    results = _tool_results(fake_anthropic)
    offered = {tool["name"] for tool in fake_anthropic.requests[0]["tools"]}
    refusals = {name: results[f"tu_other_{i}"] for i, name in enumerate(others)}
    assert host_tools == ["GrantedTool"], results
    assert "GrantedTool result for hi" in results["tu_granted"], results
    assert "mcp__bos-tools__OperatorTool" not in offered, "the same-named entry took BOS's place"
    if strict:
        assert listed == [], "strict_mcp_config: no other server is contacted"
        assert offered.isdisjoint(others), offered
        assert all("No such tool available" in text for text in refusals.values()), refusals
    else:
        assert listed and set(others) <= offered, f"the control: without strict they load: {sorted(offered)}"
        assert all("is not available to this Claude Code agent" in text for text in refusals.values()), refusals


@pytest.mark.asyncio
async def test_bos_computes_the_names_the_cli_gives_its_tools_against_the_real_cli(
    tmp_path, fake_anthropic, monkeypatch, host_tools
):
    """The hook and `can_use_tool` allow BOS's tools by the exact names the CLI gives them, so BOS
    must spell each as the CLI does (`_cli_tool_name`, after the CLI's `wn`) or deny its own tool.
    Pinned for the names the CLI rewrites (`_REWRITTEN`): each is offered under exactly the name BOS
    computed for its grant, and the model's call to it runs the host's tool at `read-only`, where
    the hook would deny any other spelling and `can_use_tool` is asked."""
    _point_the_cli_at(fake_anthropic, tmp_path, monkeypatch)
    server = BosToolMcpServer()
    agent = _agent(tmp_path, mcp=lambda: server, mcp_tools=list(_REWRITTEN))
    fake_anthropic.script([[_tu(f"tu_{i}", name, x="hi") for i, name in enumerate(_REWRITTEN.values())]])
    try:
        async with asyncio.timeout(90):
            await agent.run("chat-1", "use the tools", turn_id="t1")
    finally:
        await server.aclose()

    offered = {tool["name"] for tool in fake_anthropic.requests[0]["tools"] if tool["name"].startswith("mcp__")}
    assert offered == set(_REWRITTEN.values()), offered
    assert agent._mcp_grant is not None and agent._mcp_grant[2] == offered, "BOS's names are the CLI's"
    assert sorted(host_tools) == sorted(_REWRITTEN), _tool_results(fake_anthropic)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["refused bearer", "denied by a repo's settings"])
async def test_a_turn_warns_when_bos_mcp_server_is_not_connected_against_the_real_cli(
    tmp_path, fake_anthropic, monkeypatch, host_tools, caplog, case
):
    """When BOS's server does not connect, the CLI carries on without it: the turn completes with
    none of the agent's tools, and only the CLI's init message says so — `failed` when the server
    refuses the CLI's bearer (every request answered 401 here), and the server left out of the list
    when an MCP deny list drops it (here a repository's, loaded under `["project"]`; BOS's own flag
    settings and the user's settings drop it the same way, measured, not pinned). BOS reads that and
    logs one WARNING naming the agent, the chat and the status, and does not fail the turn."""
    _point_the_cli_at(fake_anthropic, tmp_path, monkeypatch)

    class Refusing(BosToolMcpServer):
        def _grant(self, headers: Any) -> None:
            return None  # `_gate` refuses every request, as it refuses a token it never issued

    (tmp_path / "ws" / ".claude").mkdir(parents=True)
    if case == "refused bearer":
        server, sources = Refusing(), []
    else:
        server, sources = BosToolMcpServer(), ["project"]
        deny = {"deniedMcpServers": [{"serverName": "bos-tools"}]}
        (tmp_path / "ws" / ".claude" / "settings.json").write_text(json.dumps(deny))
    agent = _agent(tmp_path, mcp=lambda: server, setting_sources=sources, mcp_tools=["GrantedTool"])
    fake_anthropic.script([[_tu("tu_granted", "mcp__bos-tools__GrantedTool", x="hi")]])
    try:
        with caplog.at_level(logging.WARNING):
            async with asyncio.timeout(90):
                result = await agent.run("chat-1", "use the tool", turn_id="t1")
    finally:
        await server.aclose()

    assert result.output == "done", "the turn completes, without BOS's tools"
    assert host_tools == [], _tool_results(fake_anthropic)
    (warning,) = _connection_warnings(caplog)
    message = warning.getMessage()
    assert "'george'" in message and "'chat-1'" in message, message
    assert ("'failed'" if case == "refused bearer" else "not loaded") in message, message


@pytest.mark.asyncio
async def test_bos_tools_are_offered_up_front_when_the_operator_turns_tool_search_on_against_the_real_cli(
    tmp_path, fake_anthropic, monkeypatch, host_tools
):
    """Tool search — on by default against a first-party host, and forced on by an inherited
    `ENABLE_TOOL_SEARCH=true`, which the CLI honours even against the fake — adds a
    `DeferredToolPlaceholder` the confinement does not classify. BOS's environment pins it off
    (§3.12), and the SDK layers BOS's `env` over the inherited one, so with `true` inherited no
    placeholder appears, BOS's tool is offered up front, and the call completes. Without BOS's pin
    the placeholder appears, which fails this test."""
    _point_the_cli_at(fake_anthropic, tmp_path, monkeypatch)
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    server = BosToolMcpServer()
    agent = _agent(tmp_path, mcp=lambda: server, mcp_tools=["GrantedTool"])
    fake_anthropic.script([[_tu("tu_granted", "mcp__bos-tools__GrantedTool", x="hi")]])
    try:
        async with asyncio.timeout(90):
            await agent.run("chat-1", "use the tool", turn_id="t1")
    finally:
        await server.aclose()

    offered = {tool["name"]: tool for tool in fake_anthropic.requests[0]["tools"]}
    assert "DeferredToolPlaceholder" not in offered and "ToolSearch" not in offered, sorted(offered)
    assert "defer_loading" not in offered["mcp__bos-tools__GrantedTool"], "BOS's tool is offered up front"
    assert host_tools == ["GrantedTool"], _tool_results(fake_anthropic)
