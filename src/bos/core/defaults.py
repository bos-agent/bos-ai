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
    MemoryEntry,
    Message,
    SkillMeta,
    ep_consolidator,
    ep_mail_route,
    ep_memory,
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


@ep_memory(name="_default")
class InMemMemoryExtension:
    """In-memory store for maxims and episodic memories."""

    def __init__(self, **maxims: str) -> None:
        self._maxims = {k.lower(): v for k, v in maxims.items()}
        self._memories: dict[str, MemoryEntry] = {}
        self._counter = 0

    # ── Maxims ──

    async def get_maxim(self, key: str) -> str:
        return self._maxims.get(key.lower(), "")

    async def set_maxim(self, key: str, content: str) -> None:
        self._maxims[key.lower()] = content

    # ── Memories ──

    async def search_memories(self, query: str, *, top_k: int = 5) -> list[MemoryEntry]:
        q = query.lower()
        results = [e for e in self._memories.values() if q in e.content.lower() or any(q in t.lower() for t in e.tags)]
        return sorted(results, key=lambda e: e.created_at, reverse=True)[:top_k]

    async def ingest_memory(self, content: str, *, tags: list[str] | None = None) -> str:
        self._counter += 1
        entry_id = f"mem_{self._counter}"
        self._memories[entry_id] = MemoryEntry(
            id=entry_id,
            content=content,
            tags=tags or [],
            created_at=datetime.now().isoformat(),
        )
        return entry_id

    async def get_memory(self, entry_id: str) -> MemoryEntry | None:
        return self._memories.get(entry_id)

    async def forget_memory(self, entry_id: str) -> None:
        self._memories.pop(entry_id, None)

    # ── Optimization ──

    async def optimize(self) -> None:
        pass


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


def _parse_frontmatter_fields(frontmatter: str) -> dict[str, str]:
    """Parse the simple YAML-style front matter fields used by skill files."""

    def strip_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    def normalize_block(block: list[str], style: str) -> str:
        lines = [line[2:] if line.startswith("  ") else line.lstrip() for line in block]
        if style == ">":
            return " ".join(line.strip() for line in lines if line.strip())
        return "\n".join(lines).strip()

    fields: dict[str, str] = {}
    lines = frontmatter.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            i += 1
            continue

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        block_style = value[:1] if value in {">", "|", ">-", "|-", ">+", "|+"} else ""
        if block_style:
            block: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line and not next_line.startswith((" ", "\t")) and ":" in next_line:
                    break
                block.append(next_line)
                i += 1
            fields[key] = normalize_block(block, block_style)
            continue

        fields[key] = strip_quotes(value)
        i += 1

    return fields


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
            else {
                name: sm
                for name, sm in skill_metas.items()
                if query in name.lower() or query in sm.name.lower() or query in sm.description.lower()
            }
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
            description = ""
            if frontmatter := re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL):
                metadata = _parse_frontmatter_fields(frontmatter.group(1))
                description = metadata.get("description") or metadata.get("summary") or ""
            if not description:
                for line in (line.strip() for line in content.splitlines() if line.strip()):
                    if len(description) > 250:
                        break
                    description += line + " "
            skill_metas[skill_name] = SkillMeta(
                location=str(path),
                name=skill_name,
                description=description,
            )
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


_system_prompt = """
## Role
You are a helpful assistant attempting to solve a task by interleaving reasoning (Thought) and actions (Act).

## Process Loop
For every user input, you must strictly follow this cycle:
1. **Thought**: Reason about the current situation. Describe what you need to do and which tool is best suited.
2. **Action**: Choose an action from the list of available tools.
3. **Action Input**: Provide the specific parameters for the chosen tool.
4. **Observation**: You will receive the output of that tool (this is provided by the system, not you).
... (Repeat steps 1-4 until you have sufficient information)
5. **Final Answer**: Provide the definitive response to the user.

## Constraints
- If a tool fails, reflect on the error in your next Thought and try a different approach.
- Only use the tools provided. Do not make up tool names.
- Always wait for an Observation before proceeding to the next Thought.
"""

default_maxims = {
    "user": "your knowledge about the user — preferences, background, projects, style",
    "soul": "your character and operating philosophy — how you work, communicate, and make decisions",
    "identity": "who you are — your role, purpose, and context",
    "rules": "hard constraints — things you must always or never do",
}

default_memory_usage = """--- USING YOUR MEMORY ---

You have two kinds of memory, accessed through three tools: Remember, Recall, and Forget.

## Maxims (your principles)

Maxims are deeply held convictions that shape how you behave and make decisions.
They are always visible to you — they appear above in the MAXIMS section.
Think of them as your conscience, not your notepad.

Each maxim has a defined scope, described in its header. Respect the scope —
put user preferences in "user", not in "rules".

Use Remember(key, content) to update a maxim when:

- The user explicitly asks you to change how you operate.
  Example: "From now on, always use TypeScript instead of JavaScript."
  → Remember(key="user", content="User prefers TypeScript over JavaScript for all projects.")

- You discover a fundamental truth about the user that should change your default behavior.
  Example: The user always rejects async solutions in favor of sync alternatives.
  → Remember(key="user", content="User prefers sync patterns. Default to sync unless async is required.")

- You are given a new rule or constraint you must follow.
  Example: "Never deploy on Fridays."
  → Remember(key="rules", content="Never deploy on Fridays. Deployments only on Mon-Thu before 14:00 UTC.")

Do NOT use maxims for:
- Facts about projects, tools, or APIs — those are memories.
- Meeting notes, code snippets, URLs — those are memories.
- Anything you might need once or twice but doesn't change who you should be.
- New categories of information — maxim keys are fixed. Use memory tags instead.

When updating a maxim, you are overwriting the ENTIRE content. Include all existing
information alongside your changes. The system cannot merge for you — you must read
the current maxim content (visible above), incorporate the new information, and write
the complete updated text.

Each maxim has a hard limit of 2048 characters. If your update would exceed this,
you must summarize and prioritize rather than expanding. Focus on what matters most
for shaping your future behavior. The system will reject writes that exceed the limit.

## Memories (your knowledge)

Memories are facts, experiences, and details you accumulate over time.
They are NOT visible to you by default — you must Recall them when needed.
Think of them as a searchable notebook, not your working memory.

Use Remember(content, tags?) to record a memory when:

- You learn a factual detail that might matter later.
  Example: "The user's database is on AWS RDS, us-east-1, PostgreSQL 16."
  → Remember(content="User's prod DB: AWS RDS us-east-1, PostgreSQL 16, "
      "pgbouncer.", tags=["infra", "database"])

- The user shares context you should carry forward across sessions.
  Example: "We're building a CLI tool for managing Kubernetes secrets."

- You complete a task and want to record the outcome for future reference.
  Example: "Deployed v2.3.1 to staging. All tests passed. Rollback window: 24h."

Use Recall(query, top_k?) to search your memories:

- Before answering a question that might depend on past context.
  Example: The user asks "what's the status of the migration?" → Recall(query="migration status")

- When the user references something you don't fully remember.
  Example: "Remember that bug we fixed last month?"

- After Recall returns snippets: if a snippet looks relevant and you need full detail,
  fetch it with Recall(entry_id=...).

Use Forget(entry_id) or Forget(query) to remove memories:

- The user explicitly asks you to forget something.
  Example: "Stop bringing up project X — we're done with it."
  → Recall(query="project X") → identify entries → Forget(entry_id=...) on each
  → Then: Remember(key="user", content="...user asked to stop referencing project X...")

- Information is clearly stale or contradicted by newer information.
  Example: Two memories contradict each other about the same topic. Keep the newer one, Forget the stale one.

## Memory hygiene

- Write memories AFTER the conversation, not during it. If you're mid-task, focus on the task. Record learnings when the user pauses or the topic concludes.
- Be concise. A memory entry is a note to your future self, not a transcript.
- Use tags. They help you find things later with Recall.
- When in doubt, write it. A slightly noisy memory is better than a lost insight."""

default_agent_spec: dict[str, Any] = {
    "name": "_default",
    "tools": "*",
    "skills": "*",
    "maxims": None,
    "subagents": "*",
    "system_prompt": _system_prompt,
}
