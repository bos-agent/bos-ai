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
        "LLM", "ChatStore", "Consolidator", "ToolSet", "TurnInterceptor",
        "PromptProvider", "TurnEventSink",
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
            or getattr(bos.core.contract, name, None)  # ToolSet, PromptProvider
        )
        if source is not None:
            assert exported is source, f"{name} is re-exported, not redefined"


def test_nothing_underscored_is_promised():
    import bos.sdk

    assert not [name for name in bos.sdk.__all__ if name.startswith("_")]
