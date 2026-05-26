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
    display_name: str | None = None
    agent_kind: str | None = None

    @property
    def display_label(self) -> str:
        label = self.display_name or self.name
        return f"{label} ({self.agent_kind})" if self.agent_kind else label


@dataclass
class RouteResult:
    target_address: str
    content: Any
    target_actor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_MENTION_RE = re.compile(r"@([\w][\w-]*)(?:\s+|$)", re.IGNORECASE)


class ActorRegistry:
    def __init__(self) -> None:
        self._actors: dict[str, ActorRecord] = {}
        self._default: str | None = None

    def register(
        self,
        name: str,
        mailbox: MailBox,
        *,
        is_default: bool = False,
        display_name: str | None = None,
        agent_kind: str | None = None,
    ) -> None:
        self._actors[name] = ActorRecord(
            name=name,
            address=mailbox.address,
            mailbox=mailbox,
            is_default=is_default,
            display_name=display_name,
            agent_kind=agent_kind,
        )
        if is_default:
            self._default = name

    def resolve_address(self, target_actor: str | None) -> str:
        resolved = self._find_actor(target_actor) if target_actor is not None else None
        if resolved is not None:
            return self._actors[resolved].address
        if self._default is not None:
            return self._actors[self._default].address
        raise KeyError(f"No actor for {target_actor!r} and no default configured")

    def resolve_mailbox(self, target_actor: str | None) -> MailBox:
        resolved = self._find_actor(target_actor) if target_actor is not None else None
        if resolved is not None:
            return self._actors[resolved].mailbox
        if self._default is not None:
            return self._actors[self._default].mailbox
        raise KeyError(f"No actor for {target_actor!r} and no default configured")

    def list_actors(self) -> dict[str, ActorRecord]:
        return dict(self._actors)

    def _find_actor(self, name: str) -> str | None:
        lower = name.lower()
        for key in self._actors:
            if key.lower() == lower:
                return key
        return None

    def route(self, content: Any, metadata: dict[str, Any] | None = None) -> RouteResult:
        target_actor: str | None = None
        cleaned = content
        out_metadata = dict(metadata or {})

        if isinstance(content, str):
            stripped = content.lstrip()
            m = _MENTION_RE.match(stripped)
            if m is not None:
                name = m.group(1)
                resolved = self._find_actor(name)
                if resolved is not None:
                    target_actor = resolved
                    cleaned = stripped[m.end():]
                    out_metadata["target_actor"] = resolved

        if target_actor is None and metadata:
            metadata_target = metadata.get("target_actor")
            if isinstance(metadata_target, str):
                resolved = self._find_actor(metadata_target)
                if resolved is not None:
                    target_actor = resolved

        address = self.resolve_address(target_actor)
        if target_actor is not None and target_actor in self._actors:
            record = self._actors[target_actor]
            out_metadata.setdefault("target_actor", target_actor)
            out_metadata.setdefault("target_address", record.address)
            out_metadata.setdefault("target_display", record.display_label)
        return RouteResult(
            target_address=address,
            content=cleaned,
            target_actor=target_actor,
            metadata=out_metadata,
        )
