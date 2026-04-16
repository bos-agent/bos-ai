from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bos.core.contract import (
    Message,
    ep_consolidator,
    ep_mailbox,
    ep_memory_store,
    ep_message_store,
    ep_provider,
    ep_skills_loader,
)
from bos.core.llm import LLMResponse
from bos.protocol import Envelope

from ._utils import _litellm_response_to_llm_response, _read_text


@ep_provider(name="_default")
async def litellm_complete(messages: list[dict], model: str, **kwargs: Any) -> LLMResponse:
    os.environ["LITELLM_MODE"] = "extension"

    import litellm

    raw = await litellm.acompletion(model=model, messages=messages, **kwargs)
    return _litellm_response_to_llm_response(raw)


@ep_message_store(name="_default")
class InMemMessageStore:
    """In-process memory store for conversation and long-term notes."""

    def __init__(self) -> None:
        self._messages: dict[str, list[Message]] = {}

    async def save_messages(self, conversation_id: str, messages: list[Message]) -> None:
        self._messages.setdefault(conversation_id, []).extend(messages)

    async def get_messages(self, conversation_id: str, original: bool = False) -> list[Message]:
        if original:
            return [m for m in self._messages.get(conversation_id, []) if not m.is_summary]
        result = []
        for m in reversed(self._messages.get(conversation_id, [])):
            if m.is_summary:
                result.append(m)
                break
            result.append(m)
        result.reverse()
        return result

    async def save_summary(self, conversation_id: str, summary: str) -> None:
        self._messages.setdefault(conversation_id, []).append(
            Message(llm_message={"role": "system", "content": f"Conversation summary:\n{summary}"}, is_summary=True)
        )

    async def list_conversations(self) -> dict[str, Any]:
        contexts = {}
        for conversation_id, messages in self._messages.items():
            if not (m := next((m for m in messages if m.llm_message["role"] == "user"), None)):
                m = messages[0]
            contexts[conversation_id] = {
                "description": m.llm_message["content"],
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
            summary = (summary or "") + (content if role == "system" else f"{role}: {content.strip()}") + "\n"
        return summary.strip()


@ep_skills_loader(name="_default")
class FileSystemSkillsLoader:
    def __init__(self, skill_dirs: Iterable[Path | str] | None = None) -> None:
        self._skill_dirs = [(Path(__file__).parent / "skills").resolve()]
        self._skill_dirs.extend(Path(dir).expanduser().resolve() for dir in skill_dirs or [])
        self._skills: dict[str, dict[str, Any]] | None = None

    async def list_skills(self) -> dict[str, dict[str, Any]]:
        if self._skills is None:
            self._skills = await self._load_skill_summary()
        return self._skills

    async def load_skill(self, name: str) -> tuple[Path, str]:
        skills = await self.list_skills()
        if (s := skills.get(name)) and (text := _read_text(s.get("path"))):
            return s["path"].parent, text

    async def search_skills(self, query: str | None = None) -> dict[str, str]:
        results = {name: data.get("summary", "") for name, data in (await self.list_skills()).items()}
        if query:
            query = query.lower()
            return {name: data for name, data in results.items() if query in name.lower() or query in data.lower()}
        return results

    async def _load_skill_summary(self) -> dict[str, dict[str, Any]]:
        def _iter_skill_files() -> Iterable[tuple[str, Path]]:
            for d in self._skill_dirs:
                if (d / "SKILL.md").exists():
                    yield d.name, d / "SKILL.md"
                if d.is_dir():
                    for c in d.iterdir():
                        if c.is_dir() and (c / "SKILL.md").exists():
                            yield c.name, c / "SKILL.md"

        skills = {}
        for skill_name, path in _iter_skill_files():
            content = path.read_text(encoding="utf-8")
            if frontmatter := re.match(r"^---\n(.*?)\n---", content, re.DOTALL):
                summary = frontmatter.group(1)
            else:
                summary = ""
                for line in (line.strip() for line in content.splitlines() if line.strip()):
                    if len(summary) > 150:
                        break
                    summary += line + "\n"
            skills[skill_name] = {"path": path, "summary": summary}
        return skills


@ep_mailbox(name="_default")
class InMemMailbox:
    _queues: dict[str, asyncio.Queue[Envelope]] = {}

    @classmethod
    def _get_queue(cls, address: str) -> asyncio.Queue[Envelope]:
        if address not in cls._queues:
            cls._queues[address] = asyncio.Queue()
        return cls._queues[address]

    async def receive(self, address: str) -> Envelope:
        return await self._get_queue(address).get()

    async def send(self, env: Envelope) -> None:
        await self._get_queue(env.recipient).put(env)

    async def receive_nowait(self, address: str) -> Envelope | None:
        try:
            return self._get_queue(address).get_nowait()
        except asyncio.QueueEmpty:
            return None
