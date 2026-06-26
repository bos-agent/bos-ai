"""_HarnessAgentRunner — the unified disposable-agent port (BEP 12).

One adapter subsumes both the subagent (text) and structured one-shot lanes.
``parent`` is optional: present → child chat nests under the parent (on-turn);
omitted → standalone internal chat-id, no parent sink (off-turn). Either way the
disposable agent's chat is internal, so it stays out of the user's chat list.
"""

import json
import uuid

import pytest

from bos.core import AgentHarness, AgentRegistry, LLMResponse, ParentTurn, ep_provider
from bos.core._chat_store_utils import is_internal_chat
from bos.core.contract import ToolContext


def test_tool_context_composes_parent_turn():
    parent = ParentTurn(chat_id="conv-1", turn_id="turn-1", agent_name="parent")
    ctx = ToolContext(parent=parent)
    # ParentTurn is the single source of truth; turn fields are read via .parent.
    assert ctx.parent is parent
    assert ctx.parent.chat_id == "conv-1"


@pytest.mark.asyncio
async def test_on_turn_text_run_nests_internal_child_chat(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"runner_text_provider_{suffix}"
    kind = f"runner_text_kind_{suffix}"

    @ep_provider(name=provider_name)
    async def text_provider(messages, model=None, **kwargs):
        return LLMResponse(content="hello from runner")

    AgentRegistry.register(name=kind, description="t", tools=[], model=f"{provider_name}/x")
    try:
        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            runner = h._plugin_services.agent_runner
            assert runner is not None
            parent = ParentTurn(chat_id="conv-1", turn_id="turn-1", agent_name="parent")
            result = await runner.run("say hi", kind=kind, parent=parent)

            assert result.output == "hello from runner"
            assert result.structured is False
            # The child ran under an internal chat-id nested beneath the parent.
            chats = await h.chat_store.list_chats()
            child_ids = [c for c in chats if c != "conv-1"]
            assert child_ids and all(is_internal_chat(c) for c in child_ids)
            assert any(c.startswith("conv-1") for c in child_ids)
    finally:
        AgentRegistry._registry.pop(kind, None)
        ep_provider._extensions.pop(provider_name, None)


@pytest.mark.asyncio
async def test_off_turn_structured_run_uses_standalone_internal_chat(tmp_path):
    suffix = uuid.uuid4().hex
    provider_name = f"runner_struct_provider_{suffix}"

    @ep_provider(name=provider_name)
    async def struct_provider(messages, model=None, **kwargs):
        return LLMResponse(content=json.dumps({"answer": "42"}))

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    try:
        async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as h:
            runner = h._plugin_services.agent_runner
            assert runner is not None
            # No context (off-turn), ad-hoc agent, structured output.
            result = await runner.run(
                "what is the answer?",
                agent_cfg={"system_prompt": "Reply with JSON.", "tools": []},
                schema=schema,
                model=f"{provider_name}/x",
            )

            assert result.structured is True
            assert result.output == {"answer": "42"}
            # The off-turn chat is a standalone internal chat-id.
            chats = await h.chat_store.list_chats()
            assert chats and all(is_internal_chat(c) for c in chats)
    finally:
        ep_provider._extensions.pop(provider_name, None)
