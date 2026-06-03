import pytest
from conftest import MessageOnlyConsolidator

from bos.core import Message
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import GatewayActorIdentity, GatewayAgent


def _gateway_agent(*, agent_name: str, store: InMemChatStore) -> GatewayAgent:
    return GatewayAgent(
        kind="poet",
        agent_name=agent_name,
        chat_store=store,
        consolidator=MessageOnlyConsolidator(),
        tools=[],
        plugins=[],
        actor_identity=GatewayActorIdentity(name="libai", display_name="Li Bai", agent_kind="poet"),
        actor_roster=[
            GatewayActorIdentity(name="main", display_name="Main", agent_kind="_default"),
            GatewayActorIdentity(name="libai", display_name="Li Bai", agent_kind="poet"),
        ],
    )


@pytest.mark.asyncio
async def test_gateway_agent_history_formats_actor_attribution_for_shared_chat():
    store = InMemChatStore()
    await store.commit_turn(
        "shared",
        [
            Message(
                llm_message={"role": "user", "content": "hello"},
                metadata={"target_actor": "libai", "target_display": "Li Bai"},
            ),
            Message(
                llm_message={"role": "assistant", "content": "a poem"},
                metadata={"actor": "libai", "actor_display": "Li Bai"},
            ),
            Message(
                llm_message={"role": "assistant", "content": "main says hi"},
                metadata={"actor": "main"},
            ),
        ],
        turn_id="turn-1",
    )
    agent = _gateway_agent(agent_name="libai", store=store)

    history = await agent._load_and_compact_history("shared", budget_model=None)

    assert history[0]["content"] == "[user -> Li Bai]\nhello"
    assert history[1]["content"] == "[assistant: Li Bai]\na poem"
    assert history[1]["role"] == "assistant"
    assert history[2]["role"] == "user"
    assert history[2]["content"] == "[assistant main said]\nmain says hi"


@pytest.mark.asyncio
async def test_gateway_agent_system_prompt_embeds_identity_and_actor_roster():
    agent = _gateway_agent(agent_name="libai", store=InMemChatStore())

    prompt = await agent._build_system_prompt()

    assert "<gateway_actor_context>" in prompt
    assert "You are gateway actor `libai`." in prompt
    assert "Your display name is `Li Bai`." in prompt
    assert "- `main` (Main, kind=_default)" in prompt
    assert "`libai` (Li Bai" not in prompt
    assert "do not treat it as your own words" in prompt
