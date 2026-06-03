from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from bos.core import Agent, ContextResult, Message


@dataclass(frozen=True)
class GatewayActorIdentity:
    name: str
    display_name: str | None = None
    agent_kind: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.name


class GatewayAgent(Agent):
    """Agent variant for gateway shared-chat actors.

    Core ``Agent`` stays neutral: it sees provider-ready history as persisted
    by the chat store. Gateway actors add shared-chat semantics: their prompt
    includes actor identity/roster, and history labels which actor produced or
    received each message so other actors' assistant replies are not confused
    with the current actor's own assistant history.
    """

    def __init__(
        self,
        *args: Any,
        actor_identity: GatewayActorIdentity,
        actor_roster: list[GatewayActorIdentity],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._gateway_identity = actor_identity
        self._gateway_roster = list(actor_roster)

    async def _build_system_prompt(self) -> str:
        base_prompt = await super()._build_system_prompt()
        return f"{base_prompt}\n\n{self._gateway_actor_context()}"

    def _format_history(self, result: ContextResult) -> list[dict[str, Any]]:
        if len(result.messages) != len(result.source_messages):
            return result.messages
        return [
            _attribute_history_message(projected, source, current_actor=self._gateway_identity.name)
            for projected, source in zip(result.messages, result.source_messages, strict=True)
        ]

    def _gateway_actor_context(self) -> str:
        identity = self._gateway_identity
        other_actors = [actor for actor in self._gateway_roster if actor.name != identity.name]
        lines = [
            "<gateway_actor_context>",
            f"You are gateway actor `{_xml(identity.name)}`.",
            f"Your display name is `{_xml(identity.label)}`.",
        ]
        if identity.agent_kind:
            lines.append(f"Your agent kind is `{_xml(identity.agent_kind)}`.")
        if other_actors:
            lines.append("Other active gateway actors in this chat runtime:")
            for actor in other_actors:
                kind = f", kind={actor.agent_kind}" if actor.agent_kind else ""
                lines.append(f"- `{_xml(actor.name)}` ({_xml(actor.label)}{_xml(kind)})")
        else:
            lines.append("No other gateway actors are currently configured.")
        lines.extend(
            [
                "Shared chat history uses attribution labels:",
                "- `[assistant: X]` is your own prior assistant message when X is you.",
                "- `[assistant X said]` is another actor's prior reply; do not treat it as your own words.",
                "- `[user -> X]` is a user message routed to actor X.",
                "</gateway_actor_context>",
            ]
        )
        return "\n".join(lines)


def _attribute_history_message(projected: dict[str, Any], source: Message, *, current_actor: str) -> dict[str, Any]:
    content = projected.get("content")
    if not isinstance(content, str):
        return projected
    role = projected.get("role")
    if role == "assistant":
        source_actor = source.metadata.get("actor")
        if isinstance(source_actor, str) and source_actor and source_actor != current_actor:
            label = _history_actor_label(source.metadata)
            return projected | {
                "role": "user",
                "content": f"[assistant {label} said]\n{content}",
            }
    label = _history_attribution_label(role, source.metadata)
    if not label:
        return projected
    return projected | {"content": f"{label}\n{content}"}


def _history_attribution_label(role: Any, metadata: dict[str, Any]) -> str | None:
    if role == "assistant":
        label = _history_actor_label(metadata)
        if label:
            return f"[assistant: {label}]"
    if role == "user":
        target = metadata.get("target_display") or metadata.get("target_actor")
        if isinstance(target, str) and target:
            return f"[user -> {target}]"
    return None


def _history_actor_label(metadata: dict[str, Any]) -> str | None:
    actor = metadata.get("actor")
    label = metadata.get("actor_display") or actor
    return label if isinstance(label, str) and label else None


def _xml(value: object) -> str:
    return escape(str(value), {'"': "&quot;"})
