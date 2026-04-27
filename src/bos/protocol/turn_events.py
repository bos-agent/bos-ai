from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TurnEvent:
    event_type: str
    phase: str
    chat_id: str
    turn_id: str
    agent_name: str | None = None
    stage: str | None = None
    detail: str | None = None
    parent_turn_id: str | None = None
    parent_chat_id: str | None = None
    parent_agent_name: str | None = None
    tool_name: str | None = None
    content: str | None = None
    summary: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_type": self.event_type,
            "phase": self.phase,
            "chat_id": self.chat_id,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "agent_name": self.agent_name,
            "stage": self.stage,
            "detail": self.detail,
            "parent_turn_id": self.parent_turn_id,
            "parent_chat_id": self.parent_chat_id,
            "parent_agent_name": self.parent_agent_name,
            "tool_name": self.tool_name,
            "content": self.content,
            "summary": self.summary,
            "tool_calls": self.tool_calls,
            "metadata": self.metadata,
        }
        return {k: v for k, v in payload.items() if v is not None}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TurnEvent":
        timestamp = payload.get("timestamp")
        return cls(
            event_type=str(payload["event_type"]),
            phase=str(payload["phase"]),
            chat_id=str(payload["chat_id"]),
            turn_id=str(payload["turn_id"]),
            agent_name=payload.get("agent_name"),
            stage=payload.get("stage"),
            detail=payload.get("detail"),
            parent_turn_id=payload.get("parent_turn_id"),
            parent_chat_id=payload.get("parent_chat_id"),
            parent_agent_name=payload.get("parent_agent_name"),
            tool_name=payload.get("tool_name"),
            content=payload.get("content"),
            summary=payload.get("summary"),
            tool_calls=payload.get("tool_calls"),
            metadata=payload.get("metadata") or {},
            timestamp=datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else datetime.now(),
        )
