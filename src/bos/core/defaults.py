from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bos.protocol import Envelope, MessageContent, MessageType
from bos.protocol.content import content_preview, image_source_to_model_url

from ._utils import _litellm_response_to_llm_response, _read_text
from .contract import (
    Message,
    SkillMeta,
    ep_consolidator,
    ep_mail_route,
    ep_memory_store,
    ep_message_store,
    ep_provider,
    ep_skills_loader,
)
from .llm import LLMResponse


@ep_provider(name="_default")
async def litellm_complete(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    os.environ["LITELLM_MODE"] = "extension"

    import litellm

    try:
        normalized_messages = [_normalize_litellm_message(message) for message in messages]
    except ValueError as exc:
        return LLMResponse(content=f"Error calling default provider: {exc}", finish_reason="error")

    raw = await litellm.acompletion(model=model, messages=normalized_messages, **kwargs)
    return _litellm_response_to_llm_response(raw)


@ep_message_store(name="_default")
class InMemMessageStore:
    """In-process memory store for chat and long-term notes."""

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}

    async def save_messages(self, chat_id: str, messages: list[Message]) -> None:
        self._messages.setdefault(chat_id, []).extend(messages)

    async def get_messages(self, chat_id: str, original: bool = False) -> list[Message]:
        if original:
            return [m for m in self._messages.get(chat_id, []) if not m.is_summary]
        result = []
        for m in reversed(self._messages.get(chat_id, [])):
            if m.is_summary:
                result.append(m)
                break
            result.append(m)
        result.reverse()
        return result

    async def save_summary(self, chat_id: str, summary: str) -> None:
        self._messages.setdefault(chat_id, []).append(
            Message(llm_message={"role": "system", "content": f"Chat summary:\n{summary}"}, is_summary=True)
        )

    async def list_chats(self) -> dict[str, Any]:
        contexts = {}
        for chat_id, messages in self._messages.items():
            if not (m := next((m for m in messages if m.llm_message["role"] == "user"), None)):
                m = messages[0]
            contexts[chat_id] = {
                "description": content_preview(m.llm_message["content"]),
                "created_at": m.created_at,
                "last_activity": messages[-1].created_at,
                "message_count": len(messages),
            }
        return contexts


@ep_memory_store(name="_default")
class InMemMemoryStore:
    """In-memory store for long-term agent identity and rules."""

    def __init__(self, **memories: str) -> None:
        self._mem = {k.lower(): v for k, v in memories.items()}

    async def load_memory(self, key: str) -> str:
        return self._mem.get(key.lower(), "")

    async def save_memory(self, key: str, content: str) -> None:
        self._mem[key.lower()] = content

    async def list_memories(self) -> dict[str, str]:
        return self._mem.copy()

    async def search_memory(self, query: str) -> dict[str, str]:
        return {key: txt for key, txt in self._mem.items() if query.lower() in txt.lower()}


@ep_consolidator(name="_default")
class NaiveConsolidator:
    """Naive content consolidator that take the last 10 messages and concatenate them."""

    async def consolidate(self, messages: list[dict], instruction: str | None = None) -> str:
        summary = None
        for role, content in ((m.get("role"), m.get("content", "")) for m in messages if not m.get("tool_calls")):
            if summary is None and role not in ["user", "system"]:
                continue
            preview = content_preview(content, limit=200)
            summary = (summary or "") + (preview if role == "system" else f"{role}: {preview.strip()}") + "\n"
        return summary.strip()


@ep_skills_loader(name="_default")
class FileSystemSkillsLoader:
    def __init__(self, skill_dirs: Iterable[Path | str] | None = None) -> None:
        self._skill_dirs = [(Path(__file__).parent / "skills").resolve()]
        self._skill_dirs.extend(Path(dir).expanduser().resolve() for dir in skill_dirs or [])
        self._skill_metas: dict[str, SkillMeta] = {}
        self._skill_metas_refreshed_at = datetime(2000, 1, 1)

    async def load_skill(self, name: str) -> str:
        skill_files = await self._list_skill_files()
        return _read_text(skill_files[name])

    async def search_skills(self, query: str | None = None) -> dict[str, SkillMeta]:
        skill_metas = await self._get_skill_metas()
        query = query and query.lower()
        return (
            skill_metas
            if not query
            else {name: sm for name, sm in skill_metas.items() if query in name.lower() or query in sm.summary.lower()}
        )

    async def _get_skill_metas(self) -> dict[str, SkillMeta]:
        now = datetime.now()
        if now - self._skill_metas_refreshed_at > timedelta(minutes=5):
            self._skill_metas = await self._load_skill_metas()
            self._skill_metas_refreshed_at = now
        return self._skill_metas

    async def _list_skill_files(self) -> dict[str, Path]:
        skill_files = {}
        for d in self._skill_dirs:
            if (d / "SKILL.md").exists():
                skill_files[d.name] = d / "SKILL.md"
            if d.is_dir():
                for c in d.iterdir():
                    if c.is_dir() and (c / "SKILL.md").exists():
                        skill_files[c.name] = c / "SKILL.md"
        return skill_files

    async def _load_skill_metas(self) -> dict[str, SkillMeta]:
        skill_files = await self._list_skill_files()
        skill_metas = {}
        for skill_name, path in skill_files.items():
            content = path.read_text(encoding="utf-8")
            if frontmatter := re.match(r"^---\n(.*?)\n---", content, re.DOTALL):
                summary = frontmatter.group(1)
            else:
                summary = ""
                for line in (line.strip() for line in content.splitlines() if line.strip()):
                    if len(summary) > 150:
                        break
                    summary += line + "\n"
            skill_metas[skill_name] = SkillMeta(location=str(path), summary=summary)
        return skill_metas


class _InMemMailBox:
    def __init__(self, route: "InMemMailRoute", address: str) -> None:
        self._route = route
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    async def receive(self) -> Envelope:
        return await self._route.receive(self._address)

    async def send(
        self,
        recipient: str,
        content: MessageContent,
        *,
        content_type: MessageType | str = MessageType.MESSAGE,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._route.deliver(
            Envelope(
                sender=self._address,
                recipient=recipient,
                content=content,
                content_type=content_type,
                chat_id=chat_id,
                metadata=metadata or {},
            )
        )

    async def receive_nowait(self) -> Envelope | None:
        return await self._route.receive_nowait(self._address)


@ep_mail_route(name="_default")
class InMemMailRoute:
    _queues: dict[str, asyncio.Queue[Envelope]] = {}

    @classmethod
    def _get_queue(cls, address: str) -> asyncio.Queue[Envelope]:
        if address not in cls._queues:
            cls._queues[address] = asyncio.Queue()
        return cls._queues[address]

    def bind(self, address: str) -> _InMemMailBox:
        return _InMemMailBox(self, address)

    async def deliver(self, env: Envelope) -> None:
        await self._get_queue(env.recipient).put(env)

    async def receive(self, address: str) -> Envelope:
        return await self._get_queue(address).get()

    async def receive_nowait(self, address: str) -> Envelope | None:
        try:
            return self._get_queue(address).get_nowait()
        except asyncio.QueueEmpty:
            return None


def _normalize_litellm_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, list):
        return message

    normalized: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("Structured message parts must be objects.")
        part_type = part.get("type")
        if part_type == "text":
            normalized.append({"type": "text", "text": part.get("text", "")})
            continue
        if part_type == "image":
            source = part.get("source") or {}
            normalized.append({"type": "image_url", "image_url": {"url": image_source_to_model_url(source)}})
            continue
        if part_type == "file":
            raise ValueError("File/PDF inputs are reserved in phase 1 and are not yet supported.")
        if part_type == "image_url":
            normalized.append(part)
            continue
        raise ValueError(f"Unsupported BOS content part for default provider: {part_type!r}")

    return {**message, "content": normalized}
