"""Mount the BOS gateway inside a FastAPI host (BEP 17 §4.1).

The other embedding shape is examples/embed_fastapi.py, which calls agent.run()
directly. This one takes the whole BEP 7 runtime — actors, channels, chat
coordination, the WebSocket protocol — into the host's own process.

Run it::

    pip install bos-ai[gateway] fastapi uvicorn
    uvicorn --factory examples.embed_gateway_fastapi:make_app --port 8123
    curl -s http://127.0.0.1:8123/bos/api/status
    curl -s -X POST http://127.0.0.1:8123/bos/api/restart

What it demonstrates, and what it deliberately does not need:

* **Configuration comes from a dict**, same as embed_fastapi.py — no TOML file,
  in-memory stores chosen by name, ``[platform] extensions = []`` so nothing is
  auto-loaded, and a stub ``@ep_provider`` so this runs with no ``[litellm]``
  and no network access.
* **The mount, not the harness, is the composition root here.** Where
  embed_fastapi.py builds a harness and calls ``agent.run()`` per request, this
  builds a ``GatewayMount`` and lets the gateway's own actors, channels and
  WebSocket protocol handle turns — the host never sees a chat_id or a message.
* **The host owns the socket.** ``mount.build_app()`` is an ordinary ASGI app,
  mounted under this app's own path and served by this app's own uvicorn.
  Nothing here binds a second port.

Not shown: authentication, TLS termination, and a persistent (non-temp)
``bos_dir`` for a long-lived deployment. Those belong to the host application.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

# Registering only the adapters this app uses, instead of importing `bos.exts`,
# which loads every built-in.
import bos.extensions.chat_stores.in_memory  # noqa: F401  registers "InMemChatStore"
import bos.extensions.mailboxes.in_memory  # noqa: F401  registers "InMemMailRoute"
from bos.config import Workspace
from bos.core import LLMResponse, ep_provider
from bos.runner import GatewayMount

# A real, writable path: the singleton flock and the channel cursors live under
# <bos_dir>/run, so a mounted gateway cannot be entirely in memory (BEP 17
# §3.4.5). A host would use its own data directory; an example uses a temp one.
_BOS_DIR = Path(tempfile.mkdtemp(prefix="bos-embed-gateway-")) / ".bos"


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
    "runtime": {
        "gateway": {"port": 0},
        "actors": {"main": {"agent": "assistant"}},
    },
    "agents": {
        "assistant": {
            "system_prompt": "You are a concise assistant.",
            "model": "echo/demo",
        }
    },
}


def _workspace() -> Workspace:
    """Built fresh on every call: a hot restart re-reads configuration, so the
    mount is handed a factory rather than an instance (BEP 17 §3.3.2)."""
    return Workspace(_BOS_DIR.parent, _BOS_DIR, CONFIG)


def make_app() -> FastAPI:
    mount = GatewayMount(
        _workspace,
        # The host owns the socket and chose the mount path, so the gateway
        # cannot discover its own URL — supplying it is what lets gateway.state
        # carry a base_url for `boscli gateway restart` (BEP 17 §3.3.2).
        public_base_url="http://127.0.0.1:8123/bos",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await mount.start()
        try:
            yield
        finally:
            await mount.stop()

    app = FastAPI(lifespan=lifespan)
    # Mounted before start(): the host mounts once, at import time, and the
    # Gateway behind this app is replaced wholesale by a restart (§3.3.3).
    app.mount("/bos", mount.build_app())

    @app.get("/gateway-health")
    async def gateway_health() -> dict:
        """The embedded observability story, demonstrated rather than described:
        the same snapshot `boscli gateway status` reads from gateway.state."""
        return mount.status()

    return app
