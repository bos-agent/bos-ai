"""Built-in actor commands (slash commands handled by AgentActor)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bos.core import ep_actor_command
from bos.core.chat_state import ChatStateError
from bos.protocol import Envelope

if TYPE_CHECKING:
    from bos.core import AgentActor


def _client_id(env: Envelope) -> str | None:
    routing = env.metadata.get("routing")
    if isinstance(routing, dict):
        client_id = routing.get("client_id")
        if isinstance(client_id, str) and client_id.strip():
            return client_id.strip()
    channel = env.metadata.get("channel")
    if isinstance(channel, dict):
        channel_id = channel.get("channel_id")
        conversation_id = channel.get("channel_conversation_id")
        if isinstance(channel_id, str) and channel_id.strip() and isinstance(conversation_id, str) and conversation_id:
            return f"{channel_id}:{conversation_id}"
    return None


def _command_payload(name: str, *, ok: bool, result=None, error: str | None = None, **extra) -> dict:
    payload = {"name": name, "ok": ok}
    if error:
        payload["error"] = error
    if result is not None:
        payload["result"] = result
    payload.update(extra)
    return payload


@ep_actor_command(name="history")
async def history(input: str, env: Envelope, actor: AgentActor) -> dict:
    """Show chat history."""
    chat_id = actor.current_chat_id(env, input)
    agent = actor._agent
    if not chat_id:
        return {"name": "history", "ok": False, "error": "(no chat found)", "result": []}
    messages = await agent._chat_store.get_messages(chat_id, active_only=True)
    result = [m.llm_message for m in messages]
    return {"name": "history", "ok": True, "result": result}


@ep_actor_command(name="compact")
async def compact(input: str, env: Envelope, actor: AgentActor) -> dict:
    """Compact a chat by summarising it."""
    chat_id = actor.current_chat_id(env, input)
    agent = actor._agent
    if not chat_id:
        return {"name": "compact", "ok": False, "error": "(no chat found)", "result": "(no chat found)"}
    messages = await agent._chat_store.get_compaction_messages(chat_id)
    summary = await agent._consolidator.consolidate(messages)
    await agent._chat_store.save_summary(chat_id, summary)
    return {"name": "compact", "ok": True, "result": f"Chat {chat_id} compacted."}


@ep_actor_command(name="tokens")
async def tokens(input: str, env: Envelope, actor: AgentActor) -> dict:
    """Estimate token usage for a chat."""
    chat_id = actor.current_chat_id(env, input)
    agent = actor._agent
    if not chat_id:
        return {"name": "tokens", "ok": False, "error": "(no chat found)", "result": "(no chat found)"}
    budget_model = getattr(agent, "_model", None)
    estimate = await agent._chat_store.estimate_tokens(chat_id, tokenizer_model=budget_model)
    result = f"Estimated tokens: {estimate.count} ({estimate.source}, model={estimate.tokenizer_model or 'unknown'})"
    return {
        "name": "tokens",
        "ok": True,
        "result": result,
        "estimated_tokens": estimate.count,
        "model": estimate.tokenizer_model,
        "source": estimate.source,
    }


@ep_actor_command(name="chats")
async def chats(actor: AgentActor) -> dict:
    """List all chats."""
    agent = actor._agent
    result = await agent._chat_store.list_chats()
    return {"name": "chats", "ok": True, "result": result}


@ep_actor_command(name="prompt")
async def prompt(actor: AgentActor) -> dict:
    """Show the current agent system prompt."""
    agent = actor._agent
    return {"name": "prompt", "ok": True, "result": await agent._build_system_prompt()}


@ep_actor_command(name="new")
async def new_chat(env: Envelope, actor: AgentActor) -> dict:
    """Start a new chat for the current client."""
    client_id = _client_id(env)
    if client_id:
        chat_id = actor._chat_state.new_chat_for_client(client_id)
    else:
        chat_id = await actor.reset_chat(env)
    await actor.retire_session(env.chat_id)
    return {
        "name": "new",
        "ok": True,
        "result": "chat reset",
        "chat_id": chat_id,
    }


@ep_actor_command(name="resume")
async def resume_chat(input: str, env: Envelope, actor: AgentActor) -> dict:
    """Resume a chat by alias or id for the current client."""
    client_id = _client_id(env)
    if not client_id:
        return _command_payload("resume", ok=False, error="Cannot resume without channel metadata.")
    if not input.strip():
        return _command_payload("resume", ok=False, error="Usage: /resume <alias-or-chat-id>")
    try:
        chat_id = actor._chat_state.resolve_alias_or_id(input.strip())
        actor._chat_state.set_cursor(client_id, chat_id)
    except ChatStateError as exc:
        return _command_payload("resume", ok=False, error=str(exc))
    if env.chat_id != chat_id:
        await actor.retire_session(env.chat_id)
    return _command_payload(
        "resume",
        ok=True,
        result=f"resumed {chat_id}",
        chat_id=chat_id,
    )


@ep_actor_command(name="alias")
async def alias_chat(input: str, env: Envelope, actor: AgentActor) -> dict:
    """Assign an alias to the current chat."""
    if not env.chat_id:
        return _command_payload("alias", ok=False, error="No current chat.")
    try:
        alias = actor._chat_state.set_alias(input.strip(), env.chat_id)
    except ChatStateError as exc:
        return _command_payload("alias", ok=False, error=str(exc))
    return _command_payload("alias", ok=True, result=f"{alias} -> {env.chat_id}", alias=alias)


@ep_actor_command(name="aliases")
async def aliases(actor: AgentActor) -> dict:
    """List chat aliases."""
    return _command_payload("aliases", ok=True, result=actor._chat_state.list_aliases())


@ep_actor_command(name="unalias")
async def unalias_chat(input: str, actor: AgentActor) -> dict:
    """Remove a chat alias."""
    try:
        removed = actor._chat_state.delete_alias(input.strip())
    except ChatStateError as exc:
        return _command_payload("unalias", ok=False, error=str(exc))
    return _command_payload("unalias", ok=True, result="alias removed" if removed else "alias not found")
