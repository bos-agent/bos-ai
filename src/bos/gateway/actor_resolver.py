from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bos.protocol import MessageContent


@dataclass(frozen=True)
class ActorDescriptor:
    name: str
    address: str
    display_name: str | None = None
    agent_kind: str | None = None
    is_default: bool = False


@dataclass(frozen=True)
class ActorRouteResult:
    target_actor: str
    target_address: str
    content: MessageContent
    metadata: dict[str, Any] = field(default_factory=dict)


class ActorResolutionError(ValueError):
    def __init__(self, target_actor: str) -> None:
        super().__init__(f"Unknown actor @{target_actor}")
        self.target_actor = target_actor

    def to_event(self) -> dict[str, str]:
        return {
            "event": "actor_resolution_error",
            "target_actor": self.target_actor,
            "message": str(self),
        }


class ActorResolver:
    def __init__(
        self,
        actors: dict[str, ActorDescriptor],
        *,
        default_actor: str,
        mention_prefix: str = "@",
    ) -> None:
        self._actors = dict(actors)
        self._actors_lower = {name.lower(): name for name in actors}
        self._default_actor = default_actor
        self._mention_prefix = mention_prefix
        escaped = re.escape(mention_prefix)
        self._mention_re = re.compile(rf"{escaped}([\w][\w-]*)(?:\s+|$)", re.IGNORECASE)
        self._require_actor(default_actor)

    def list_actors(self) -> dict[str, ActorDescriptor]:
        return dict(self._actors)

    def resolve(
        self,
        content: MessageContent,
        *,
        default_actor: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActorRouteResult:
        out_metadata = dict(metadata or {})
        target_actor = default_actor or self._default_actor
        cleaned = content

        if isinstance(content, str):
            stripped = content.lstrip()
            match = self._mention_re.match(stripped)
            if match is not None:
                mentioned = match.group(1)
                target_actor = self._resolve_name(mentioned)
                cleaned = stripped[match.end():]
        elif isinstance(out_metadata.get("target_actor"), str):
            target_actor = out_metadata["target_actor"]

        resolved = self._require_actor(target_actor)
        descriptor = self._actors[resolved]
        out_metadata["target_actor"] = resolved
        out_metadata["target_agent"] = resolved
        out_metadata["target_address"] = descriptor.address
        if descriptor.display_name:
            out_metadata["target_display"] = descriptor.display_name
        return ActorRouteResult(
            target_actor=resolved,
            target_address=descriptor.address,
            content=cleaned,
            metadata=out_metadata,
        )

    def _resolve_name(self, name: str) -> str:
        resolved = self._actors_lower.get(name.lower())
        if resolved is None:
            raise ActorResolutionError(name)
        return resolved

    def _require_actor(self, name: str) -> str:
        return self._resolve_name(name)
