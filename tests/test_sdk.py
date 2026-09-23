"""bos.sdk — the embedding contract (BEP 18)."""

from __future__ import annotations

import pytest

from bos.sdk import open_harness


def _config() -> dict:
    # bos.exts must load: InMemChatStore/InMemMailRoute are registered by it, and
    # with extensions=[] `workspace.harness()` raises
    # "Extension 'InMemMailRoute' not found". Task 4 also needs `BOS` registered.
    return {
        "platform": {"extensions": ["bos.exts"]},
        "harness": {"chat_store": "InMemChatStore", "mail_route": "InMemMailRoute"},
        "agents": {"solo": {"system_prompt": "hi"}},
    }


@pytest.mark.asyncio
async def test_open_harness_bootstraps_and_yields_a_usable_harness(tmp_path):
    from bos.config import Workspace
    from bos.core import AgentRegistry

    ws = Workspace(tmp_path, tmp_path / ".bos", _config())
    async with open_harness(ws) as harness:
        # bootstrap_platform ran: the config's agents are registered.
        assert AgentRegistry.has_registered("solo")
        agent = await harness.create_agent(kind="solo")
        assert agent is not None


@pytest.mark.asyncio
async def test_open_harness_resolves_agent_files_before_registering(tmp_path):
    """The order is the point. bootstrap_platform registers what resolve_agents
    loaded, so reversing them drops every agent file silently (BEP 18 §3.3)."""
    from bos.config import Workspace
    from bos.core import AgentRegistry

    agents_dir = tmp_path / ".bos" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "fromfile.md").write_text("---\nname: fromfile\n---\n\nYou are from a file.\n")

    ws = Workspace(tmp_path, tmp_path / ".bos", _config())
    async with open_harness(ws):
        assert AgentRegistry.has_registered("fromfile")


@pytest.mark.asyncio
async def test_bosapp_caches_agents_and_resolves_the_default(tmp_path):
    from bos.sdk import BosApp

    async with BosApp(_config(), bos_dir=tmp_path / ".bos") as app:
        first = app.agent()
        assert first is app.agent("solo"), "agent() must cache, not rebuild per call"
        assert app.workspace.resolve_default_agent() == "solo"


@pytest.mark.asyncio
async def test_a_registry_only_default_is_built_at_entry(tmp_path):
    """The shipped `default` preset's shape: `default_agent` names a kind an
    ``@ep_agent`` factory registers, and `[agents]` is empty. `agent()` is sync
    and cached, so the resolved default is built during `__aenter__` too, not
    only the kinds the config names (BEP 18 §3.4)."""
    from bos.core import Agent
    from bos.sdk import BosApp

    config = _config() | {"default_agent": "BOS", "agents": {}}
    async with BosApp(config, bos_dir=tmp_path / ".bos") as app:
        assert isinstance(app.agent(), Agent)


@pytest.mark.asyncio
async def test_an_ambiguous_default_does_not_prevent_entry(tmp_path):
    """Building the default is tried, not required: two agents and no
    `default_agent` is unresolvable, and a project that always names its agent
    must still start. Only `agent()` with no argument fails."""
    from bos.sdk import BosApp

    config = _config() | {"agents": {"a": {"system_prompt": "a"}, "b": {"system_prompt": "b"}}}
    async with BosApp(config, bos_dir=tmp_path / ".bos") as app:
        assert app.agent("a") is not None
        with pytest.raises(ValueError):
            app.agent()


@pytest.mark.asyncio
async def test_agent_before_entering_says_so(tmp_path):
    """Review Focus 3: no harness yet — a message, not AttributeError on None."""
    from bos.sdk import BosApp

    app = BosApp(_config(), bos_dir=tmp_path / ".bos")
    with pytest.raises(RuntimeError) as excinfo:
        app.agent()
    assert "async with" in str(excinfo.value)


@pytest.mark.asyncio
async def test_agent_after_exit_says_so(tmp_path):
    """Review Focus 3, the other half: a closed app must not hand out an Agent
    whose harness is gone."""
    from bos.sdk import BosApp

    async with BosApp(_config(), bos_dir=tmp_path / ".bos") as app:
        pass
    with pytest.raises(RuntimeError) as excinfo:
        app.agent()
    assert "async with" in str(excinfo.value)
    with pytest.raises(RuntimeError) as excinfo:
        app.harness
    assert "async with" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_unbuilt_kind_names_build_agent(tmp_path):
    """agent() is sync and create_agent is not, so kinds that exist only in
    AgentRegistry are built on request — with an await, through a method that
    says so rather than a second return shape from agent() (BEP 18 §3.4)."""
    from bos.sdk import BosApp

    async with BosApp(_config(), bos_dir=tmp_path / ".bos") as app:
        with pytest.raises(RuntimeError) as excinfo:
            app.agent("BOS")
        assert "build_agent" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_second_bosapp_in_one_process_is_refused(tmp_path):
    """Review Focus 1: bootstrap_platform writes os.environ and clears
    AgentRegistry, so a second live BosApp would wipe the first's agents while
    it is still running. Refusing beats corrupting."""
    from bos.sdk import BosApp

    async with BosApp(_config(), bos_dir=tmp_path / ".bos"):
        with pytest.raises(RuntimeError) as excinfo:
            async with BosApp(_config(), bos_dir=tmp_path / ".bos2"):
                pass
    assert "already" in str(excinfo.value).lower()

    # And the guard releases, so a later app still works.
    async with BosApp(_config(), bos_dir=tmp_path / ".bos3") as app:
        assert app.agent() is not None


def test_the_contract_surface_is_importable_and_identical():
    """__all__ is the promise. Re-exports must be the same objects, so there is
    one class and one isinstance answer per name (BEP 18 §3.8)."""
    import bos.config
    import bos.core
    import bos.core.contract
    import bos.sdk

    expected = {
        "BosApp", "open_harness",
        "Agent", "AgentHarness", "AgentResult", "Message", "TurnContext",
        "LLM", "LLMResponse", "ChatStore", "ChatCommit", "ChatMeta",
        "ContextResult", "TokenEstimate", "Consolidator", "ToolSet",
        "ToolAttributes", "ToolCallRequest", "TurnInterceptor",
        "PromptProvider", "TurnEventSink", "TurnEvent",
        "ep_tool", "ep_provider", "ep_agent", "ep_chat_store", "ep_mail_route",
        "ep_consolidator", "ep_turn_interceptor", "ep_channel", "ep_plugin",
        "Workspace", "RootConfig", "validate_config",
    }
    assert set(bos.sdk.__all__) == expected

    for name in bos.sdk.__all__:
        exported = getattr(bos.sdk, name)
        source = (
            getattr(bos.core, name, None)
            or getattr(bos.config, name, None)
            or getattr(bos.core.contract, name, None)  # ToolSet, PromptProvider, ToolAttributes
        )
        if source is not None:
            assert exported is source, f"{name} is re-exported, not redefined"


def test_nothing_underscored_is_promised():
    import bos.sdk

    assert not [name for name in bos.sdk.__all__ if name.startswith("_")]


def _bos_leaf_types(hint: object) -> set[type]:
    """Recursively unwrap a type hint (unions, generics, containers) down to
    the leaf classes it references that are defined somewhere under
    ``bos.core`` — the types BOS itself owns, as opposed to stdlib/typing
    machinery (``str``, ``Any``, ``Literal``, ``None``, ``Sequence``, ...),
    which this silently drops."""
    import typing

    origin = typing.get_origin(hint)
    if origin is not None:
        leaves: set[type] = set()
        for arg in typing.get_args(hint):
            leaves |= _bos_leaf_types(arg)
        return leaves
    if not isinstance(hint, type):
        return set()
    if not (hint.__module__ or "").startswith("bos.core"):
        return set()
    return {hint}


def test_promised_ports_are_implementable_from_the_contract_alone():
    """Every Protocol bos.sdk promises (a port an embedder *implements* —
    ChatStore, LLM, etc.) must be implementable using only names bos.sdk also
    promises. If a port's own method needs a type that isn't in __all__, an
    embedder can satisfy the Protocol's shape but cannot type or construct the
    values its methods pass and return without reaching past the contract
    (BEP 18 §3.8).

    Scoped to Protocols, not every promised name: Agent and AgentHarness are
    concrete classes an embedder *uses*, not a shape they implement, so their
    constructor/method types (e.g. AgentPlugin, HarnessPlugin) are exempt —
    reachable-through is not the same claim as promised-port-is-implementable.
    """
    import inspect
    import typing

    import bos.sdk

    promised = set(bos.sdk.__all__)
    missing: list[str] = []

    for name in sorted(promised):
        cls = getattr(bos.sdk, name)
        if not (isinstance(cls, type) and getattr(cls, "_is_protocol", False)):
            continue  # not a port an embedder implements (e.g. Agent, AgentHarness)
        # inspect.getmembers walks the full MRO (via dir()), not just cls.__dict__ —
        # a method a Protocol inherits from a base is still part of the shape an
        # embedder must implement. A `@property` is not `callable`, so it is
        # unwrapped to its getter explicitly rather than skipped.
        for attr_name, member in inspect.getmembers(cls):
            if attr_name.startswith("_"):
                continue
            if isinstance(member, property):
                if member.fget is None:
                    continue
                member = member.fget
            elif not callable(member):
                continue
            hints = typing.get_type_hints(member)
            for hint_name, hint in hints.items():
                for leaf in _bos_leaf_types(hint):
                    if leaf.__name__ not in promised:
                        missing.append(
                            f"{name}.{attr_name}({hint_name}) needs {leaf.__module__}.{leaf.__name__}, "
                            "which bos.sdk does not promise"
                        )

    assert not missing, "\n".join(missing)
