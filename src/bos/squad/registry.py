from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bos.core import MailBox


@dataclass
class ActorRecord:
    name: str
    address: str
    mailbox: MailBox
    is_default: bool = False


@dataclass
class RouteResult:
    target_address: str
    content: str
    target_actor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_MENTION_RE = re.compile(r"@([\w][\w-]*)\s+")


class ActorRegistry:
    def __init__(self) -> None:
        self._actors: dict[str, ActorRecord] = {}
        self._default: str | None = None

    def register(
        self, name: str, mailbox: MailBox, *, is_default: bool = False
    ) -> None:
        self._actors[name] = ActorRecord(
            name=name,
            address=mailbox.address,
            mailbox=mailbox,
            is_default=is_default,
        )
        if is_default:
            self._default = name

    def resolve_address(self, target_actor: str | None) -> str:
        if target_actor is not None and target_actor in self._actors:
            return self._actors[target_actor].address
        if self._default is not None:
            return self._actors[self._default].address
        raise KeyError(
            f"No actor for {target_actor!r} and no default configured"
        )

    def resolve_mailbox(self, target_actor: str | None) -> MailBox:
        if target_actor is not None and target_actor in self._actors:
            return self._actors[target_actor].mailbox
        if self._default is not None:
            return self._actors[self._default].mailbox
        raise KeyError(
            f"No actor for {target_actor!r} and no default configured"
        )

    def list_actors(self) -> dict[str, ActorRecord]:
        return dict(self._actors)

    def route(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> RouteResult:
        target_actor: str | None = None
        cleaned = content
        out_metadata = dict(metadata or {})

        m = _MENTION_RE.match(content)
        if m:
            name = m.group(1)
            if name in self._actors:
                target_actor = name
                cleaned = content[m.end():]
                out_metadata["target_actor"] = name

        if target_actor is None and metadata:
            target_actor = metadata.get("target_actor")
            if isinstance(target_actor, str) and target_actor not in self._actors:
                target_actor = None

        address = self.resolve_address(target_actor)
        return RouteResult(
            target_address=address,
            content=cleaned,
            target_actor=target_actor,
            metadata=out_metadata,
        )
