"""Gateway-owned configuration shapes (BEP 13 §3.3).

The gateway is *policy*; configuration loading is a *detail*. So the gateway
defines the shape of the settings it consumes — these frozen value objects —
and ``bos.config`` (the loader, an outer ring) reads storage and *produces*
them. The gateway never imports ``bos.config``; it is handed a
``GatewayRuntimeConfig`` at construction time (assembled by the loader and
injected by the composition root, ``runner``/``cli``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResolvedGatewayConfig:
    host: str = "127.0.0.1"
    port: int = 5920
    upload_dir: str = ".bos/uploads/http"
    max_upload_bytes: int = 20 * 1024 * 1024
    api_key_env: str = "BOS_GATEWAY_API_KEY"


@dataclass(frozen=True)
class ResolvedActorConfig:
    name: str
    agent: str
    address: str
    display_name: str | None = None
    restart_on_error: bool = True
    max_restarts: int = 5
    agent_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedGatewayChannelConfig:
    type: str
    channel_id: str
    address: str
    target_actor: str
    display_name: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    def extension_config(self) -> dict[str, Any]:
        return {
            "name": self.type,
            "channel_id": self.channel_id,
            "target_actor": self.target_actor,
            "display_name": self.display_name,
            "settings": self.settings,
        }


@dataclass(frozen=True)
class GatewayRuntimeConfig:
    """Everything the ``Gateway`` needs, assembled by the loader and injected in.

    This is the single gateway-owned aggregate the composition root fills from a
    ``Workspace`` and passes to ``Gateway(runtime=...)``, so the gateway depends
    on no configuration machinery — only on these shapes it owns.
    """

    gateway: ResolvedGatewayConfig
    actors: dict[str, ResolvedActorConfig]
    default_actor: str
    channels: list[ResolvedGatewayChannelConfig]
    mention_prefix: str
    workdir: str
    bos_dir: Path
