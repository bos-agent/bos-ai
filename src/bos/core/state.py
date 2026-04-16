from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bos.core.registry import ExtensionPoint

if TYPE_CHECKING:
    from bos.core import Message


ep_message_store = ExtensionPoint(
    description="""
        Message store. A factory that creates message stores implementing the MessageStore protocol.
    """
)


@dataclass
class Message:
    llm_message: dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    turn_id: str | None = None
    is_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MessageStore(Protocol):
    async def save_messages(self, conversation_id: str, messages: Iterable[Message]) -> None: ...
    async def get_messages(self, conversation_id: str, original: bool = False) -> Iterable[Message]: ...
    async def save_summary(self, conversation_id: str, summary: str) -> None: ...
    async def list_conversations(self) -> dict[str, dict[str, Any]]: ...


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


ep_memory_store = ExtensionPoint(
    description="""
        Memory store. A factory that creates memory stores implementing the MemoryStore protocol.
    """
)


@runtime_checkable
class MemoryStore(Protocol):
    async def load_memory(self, key: str) -> str: ...
    async def save_memory(self, key: str, content: str) -> None: ...
    async def list_memories(self) -> dict[str, str]: ...
    async def search_memory(self, query: str) -> dict[str, str]: ...


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


ep_consolidator = ExtensionPoint(
    description="""
        Content consolidator. A factory that creates consolidators implementing the Consolidator protocol.
    """
)


@runtime_checkable
class Consolidator(Protocol):
    async def consolidate(self, messages: list[dict], instruction: str | None = None) -> str: ...


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


ep_skills_loader = ExtensionPoint(
    description="""
        Skills Loader. A factory that creates skills loaders implementing the SkillsLoader protocol.
    """
)


@runtime_checkable
class SkillsLoader(Protocol):
    async def load_skill(self, name: str) -> str: ...
    async def search_skills(self, query: str) -> list[str]: ...


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
        from bos.core import _read_text

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
