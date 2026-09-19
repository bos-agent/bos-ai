"""Embed the BOS agent in a FastAPI application (BEP 16 §3.6).

Run it::

    pip install bos-ai fastapi uvicorn
    uvicorn examples.embed_fastapi:app
    curl -X POST localhost:8000/ask -H 'content-type: application/json' \
         -d '{"chat_id": "demo", "message": "hello"}'

What it demonstrates, and what it deliberately does not need:

* **Configuration comes from a dict**, not a TOML file. ``Workspace`` has
  accepted ``dict | RootConfig`` since BEP 6; ``Workspace.from_discovery()`` is
  the file-discovery convenience the CLI uses, not the only entry point. Load
  this dict from a database, environment, or a control plane.
* **Adapters are chosen, not inherited.** ``[platform] extensions = []`` means
  nothing is auto-loaded; this file imports the two in-memory adapters it wants
  and registers its own LLM provider. Swap ``echo_provider`` for a real client,
  or install ``bos-ai[litellm]`` and point ``model`` at a real model.
* **No CLI, no gateway, no filesystem.** Nothing here imports ``bos.cli`` or
  ``bos.gateway``, and ``bos_dir`` stays empty because both stores are
  in-memory. The host application owns HTTP, concurrency, and persistence.

Not shown: authentication, streaming (pass an ``event_sink`` to ``run()``), and
per-request concurrency limits. Those belong to the host application.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

# Registering only the adapters this app uses, instead of importing `bos.exts`,
# which loads every built-in.
import bos.extensions.chat_stores.in_memory  # noqa: F401  registers "InMemChatStore"
import bos.extensions.mailboxes.in_memory  # noqa: F401  registers "InMemMailRoute"
from bos.config import Workspace
from bos.core import AgentHarness, LLMResponse, ep_provider


@ep_provider(name="echo")
async def echo_provider(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    """A stand-in for a real model call, so this example runs offline.

    Replace with your own client, or install bos-ai[litellm] and drop this.
    """
    last = messages[-1]["content"] if messages else ""
    return LLMResponse(content=f"echo: {last}")


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


class AskRequest(BaseModel):
    chat_id: str
    message: str


def build_workspace(bos_dir: str) -> Workspace:
    workspace = Workspace(workspace=".", bos_dir=bos_dir, config=CONFIG)
    workspace.bootstrap_platform()
    return workspace


@asynccontextmanager
async def lifespan(app: FastAPI):
    # bos_dir is where file-backed adapters would write. Both stores here are
    # in-memory, so this directory stays empty — it is a required argument, not
    # a required dependency on the filesystem.
    with tempfile.TemporaryDirectory() as bos_dir:
        harness: AgentHarness = build_workspace(bos_dir).harness()
        async with harness:
            app.state.agent = await harness.create_agent(kind="assistant")
            yield


app = FastAPI(lifespan=lifespan)


@app.post("/ask")
async def ask(request: AskRequest) -> dict[str, str]:
    result = await app.state.agent.run(request.chat_id, request.message)
    return {"reply": str(result.output or "")}
