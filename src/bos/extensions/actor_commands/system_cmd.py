"""Built-in actor commands (slash commands handled by AgentActor).

Each command is a plain async function decorated with ``@ep_actor_command``.
The actor injects whichever arguments the function declares from:
``input``, ``env``, ``actor``, ``harness``.

The return value is sent back to the caller:
- ``str`` → wrapped in an Envelope with ``content_type='command_result'``
- ``dict/list`` → JSON-serialized, then wrapped
- ``Envelope`` → sent as-is
- ``None`` → replaced with ``"(done)"``
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bos.core import ep_actor_command
from bos.protocol import Envelope

if TYPE_CHECKING:
    from bos.core import AgentActor


@ep_actor_command(name="history")
async def history(input: str, env: Envelope, actor: AgentActor) -> str:
    """Show conversation history."""
    conversation_id = input.strip() or env.conversation_id
    agent = actor._agent
    if not conversation_id:
        return "(no conversation found)"
    messages = await agent._message_store.get_messages(conversation_id)
    result = [m.llm_message for m in messages]
    return json.dumps({"name": "history", "result": result}, default=str, indent=2)


@ep_actor_command(name="compact")
async def compact(input: str, env: Envelope, actor: AgentActor) -> str:
    """Compact a conversation by summarising it."""
    conversation_id = input.strip() or env.conversation_id
    agent = actor._agent
    if not conversation_id:
        return "(no conversation found)"
    messages = await agent._message_store.get_messages(conversation_id)
    summary = await agent._consolidator.consolidate([m.llm_message for m in messages])
    await agent._message_store.save_summary(conversation_id, summary)
    return f"Conversation {conversation_id} compacted."


@ep_actor_command(name="tokens")
async def tokens(input: str, env: Envelope, actor: AgentActor) -> str:
    """Estimate token usage for a conversation."""
    conversation_id = input.strip() or env.conversation_id
    agent = actor._agent
    if not conversation_id:
        return "(no conversation found)"
    messages = await agent._message_store.get_messages(conversation_id)
    char_count = sum(len(str(m.llm_message.get("content", ""))) for m in messages)
    return json.dumps({"name": "tokens", "result": f"Approx chars: {char_count}  ·  ~{char_count // 4} tokens"})


@ep_actor_command(name="conversations")
async def conversations(actor: AgentActor) -> str:
    """List all conversations."""
    agent = actor._agent
    result = await agent._message_store.list_conversations()
    return json.dumps({"name": "conversations", "result": result}, default=str)


@ep_actor_command(name="memory")
async def memory(actor: AgentActor) -> str:
    """List agent memories."""
    agent = actor._agent
    result = await agent._memory_store.list_memories()
    return json.dumps({"name": "memory", "result": result}, default=str)
