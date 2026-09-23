"""Embed the BOS agent as a plain Python object — no web framework (BEP 18 §3.8).

Run it::

    uv run python examples/embed_sdk.py

This is the *mode 1* shape: what someone building a command-line agent, a chat
bot, or a batch job starts from. Compare with ``embed_fastapi.py``, which adds
only HTTP on top of the same calls.

What it demonstrates, and what it deliberately does not need:

* **Everything BOS promises an embedder comes from one place.** ``BosApp`` and
  ``ep_provider`` here are both ``bos.sdk`` exports — ``bos.sdk.__all__`` *is*
  the contract (BEP 18 §3.8). Nothing in this file imports ``bos.gateway`` or
  ``bos.cli``.
* **Configuration is a dict**, with a stub ``@ep_provider`` registered before
  ``BosApp`` opens, so this runs offline and needs no ``[litellm]`` extra — no
  dependency beyond the base ``bos-ai`` install. CI asserts exactly that.
* **Chat continuity is an id, not an API.** Both turns below pass the same
  ``chat_id``; the second reply shows the first turn already in its history.
* **``event_sink`` is ``Agent.run()``'s ordinary surface**, not a gateway-only
  feature — passing one on the second turn is the only difference from the
  first call.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any

# Registering only the adapters this app uses, instead of importing `bos.exts`,
# which loads every built-in.
import bos.extensions.chat_stores.in_memory  # noqa: F401  registers "InMemChatStore"
import bos.extensions.mailboxes.in_memory  # noqa: F401  registers "InMemMailRoute"
from bos.core import LLMResponse, TurnEvent
from bos.sdk import BosApp, TurnEventSink, ep_provider


@ep_provider(name="echo")
async def echo_provider(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    """A stand-in for a real model call, so this example runs offline.

    Replace with your own client, or install bos-ai[litellm] and drop this.
    Reporting how many messages it was handed is what makes the second turn's
    reply below visibly include the first turn's history.
    """
    last = messages[-1]["content"] if messages else ""
    return LLMResponse(content=f"echo: {last} (history: {len(messages)} messages)")


class PrintingEventSink:
    """Prints each intra-turn event as it happens.

    Structurally satisfies ``TurnEventSink`` (a ``Protocol``) with no base
    class — proof that `event_sink` is ordinary `Agent.run()` surface, not
    something the gateway alone can hand out.
    """

    async def emit(self, event: TurnEvent) -> None:
        print(f"  [event] {event.event_type} ({event.phase})")


CONFIG: dict[str, Any] = {
    "platform": {"extensions": []},
    "harness": {
        "chat_store": "InMemChatStore",
        "mail_route": "InMemMailRoute",
    },
    "agents": {
        "assistant": {
            "system_prompt": "You are a concise assistant.",
            "model": "echo/demo",
        }
    },
}


async def main() -> None:
    # bos_dir is where file-backed adapters would write. Both stores here are
    # in-memory, so this directory stays empty — it is a required argument, not
    # a required dependency on the filesystem.
    with tempfile.TemporaryDirectory() as bos_dir:
        async with BosApp(CONFIG, bos_dir=bos_dir) as app:
            agent = app.agent()

            first = await agent.run("cli-session", "hello")
            print("turn 1:", first.output)

            sink: TurnEventSink = PrintingEventSink()
            second = await agent.run("cli-session", "what did I just say?", event_sink=sink)
            print("turn 2:", second.output)


if __name__ == "__main__":
    asyncio.run(main())
