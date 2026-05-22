"""MemoryHarnessPlugin and MemoryAgentPlugin."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bos.core._utils import _allowed
from bos.core.contract import (
    AgentBindContext,
    AgentPlugin,
    PluginServices,
    TurnInterceptor,
    ep_plugin,
)
from bos.core.registry import ExtensionPoint, ToolRegistry

from .scoped_memory import MemoryBackend

ep_memory_backend = ExtensionPoint(
    description="Memory store implementations (MarkdownMemoryBackend, InMemMemoryExtension, etc.)."
)

if TYPE_CHECKING:
    from bos.core.agent import TurnContext

MAXIM_LIMIT = 2048

_MAXIM_DESCRIPTIONS = {
    "user": "your knowledge about the user — preferences, background, projects, style",
    "soul": "your character and operating philosophy — how you work, communicate, and make decisions",
    "identity": "who you are — your role, purpose, and context",
    "rules": "hard constraints — things you must always or never do",
}

_MEMORY_TOOL_USAGE = {
    "Remember": """Store durable context in episodic memory for later recall.

### Memories (Episodic)

Use for stable user preferences, recurring feedback, non-obvious project context, and useful
task outcomes that may matter in future conversations.

### Memory hygiene
- Do not store facts derivable from the current repository state.
- Do not store transient task-planning details; use task tools for those.
- Prefer a small set of high-signal entries over many low-signal entries.
- When a memory is clearly stale or superseded, use Forget to remove it.""",

    "Recall": """Retrieve information from episodic memory.

Use query to search for relevant memories, or entry_id to fetch a specific memory in full after
a search result identifies it. Use memory as context, not as proof of current repository state.

Guidelines:
- Recall when the user references prior conversations, preferences, or remembered context.
- Prefer current files, tests, and git history for facts about the repository.
- Verify any memory-derived file, symbol, or behavior claim before acting on it.""",

    "Forget": """Remove information from episodic memory.

Use entry_id to remove one specific memory, or query to remove all matching memories. Prefer
entry_id when possible so unrelated memories are not removed accidentally.

Guidelines:
- Use when the user asks you to forget remembered information or when a memory is clearly stale.
- Search with Recall first if you need to identify the exact memory.
- Do not use Forget for current task state; update tasks instead.""",

    "ReviseMaxim": """Append a revision note to a maxim. Existing content is preserved.

### Maxims

Deeply held convictions (e.g., user preferences, rules). Always visible in your context.
- Scope: Respect the keys ("user", "soul", "identity", "rules").
- Limits: 2048 chars total. Keep notes concise.
- Do NOT use for: Facts, snippets, meeting notes, one-off details.""",
}


@ep_plugin(name="MemoryPlugin")
class MemoryHarnessPlugin:
    @property
    def name(self) -> str:
        return "MemoryPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {"maxims": ["user", "soul", "identity", "rules"], "scope": "workspace", "backend": "_default"}

    async def setup(self, services: PluginServices) -> None:
        self._services = services
        self._backend: MemoryBackend | None = None

    def validate_config(self, config: Mapping[str, Any], context: AgentBindContext) -> None:
        maxims = config.get("maxims", [])
        if not isinstance(maxims, list):
            raise TypeError("MemoryPlugin: 'maxims' must be a list")
        scope = config.get("scope", "workspace")
        if scope not in ("workspace", "actor"):
            raise ValueError(f"MemoryPlugin: 'scope' must be 'workspace' or 'actor', got {scope!r}")

    def bind(self, config: Mapping[str, Any], context: AgentBindContext) -> AgentPlugin:
        if self._backend is None:
            backend_name = config.get("backend", "_default")
            backend_ext = ep_memory_backend.get(backend_name)
            if backend_ext is None:
                raise ValueError(f"MemoryPlugin: unknown backend {backend_name!r}")
            self._backend = backend_ext.fn(bos_dir=self._services.bos_dir)
        backend = self._backend
        scope = config.get("scope", "workspace")
        if scope == "actor" and context.actor_scope:
            from .scoped_memory import ScopedMemory

            backend = ScopedMemory(backend, context.actor_scope)
        maxim_keys = set(config.get("maxims", []))
        return MemoryAgentPlugin(backend, maxim_keys)

    async def teardown(self) -> None:
        from bos.core._utils import _aclose

        await _aclose(getattr(self, "_backend", None))


class MemoryAgentPlugin:
    def __init__(self, backend: MemoryBackend, maxim_keys: set[str]) -> None:
        self._backend = backend
        self._maxim_keys = maxim_keys

    @property
    def name(self) -> str:
        return "MemoryPlugin"

    def register_tools(self, registry: ToolRegistry) -> None:
        backend = self._backend
        maxim_keys = self._maxim_keys

        @registry(
            name="Remember",
            description="Store a fact or detail in your episodic memory for later Recall.",
            usage=_MEMORY_TOOL_USAGE["Remember"],
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The information to store."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorisation.",
                    },
                },
                "required": ["content"],
            },
        )
        async def remember(content: str, tags: list[str] | None = None) -> str:
            entry_id = await backend.ingest_memory(content, tags=tags)
            tag_note = f" Tags: {tags}." if tags else ""
            return f"(Memory stored with entry_id: {entry_id}.{tag_note})"

        @registry(
            name="ReviseMaxim",
            description=(
                "Append a revision note to a maxim. Existing content is preserved; "
                "your text is added as a timestamped entry. You can only update the "
                "active maxims in your context."
            ),
            usage=_MEMORY_TOOL_USAGE["ReviseMaxim"],
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Maxim key. One of: user, soul, identity, rules."},
                    "content": {"type": "string", "description": "The revision note to append."},
                },
                "required": ["key", "content"],
            },
        )
        async def revise_maxim(key: str, content: str) -> str:
            if not _allowed(key.lower(), maxim_keys):
                return f"Error: Maxim '{key}' is not allowed."
            current = await backend.get_maxim(key.lower())
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            revised = f"{current}\n[{ts}] {content}" if current else f"[{ts}] {content}"
            if len(revised) > MAXIM_LIMIT:
                return (
                    f"Error: Revision would bring maxim '{key}' to "
                    f"{len(revised)} characters (limit {MAXIM_LIMIT}). "
                    f"Wait for a merge cycle or keep it shorter."
                )
            await backend.set_maxim(key.lower(), revised)
            return f"(Revision appended to maxim '{key}'. Total size: {len(revised)}/{MAXIM_LIMIT} characters.)"

        @registry(
            name="Recall",
            description=(
                "Retrieve information from your memories. Use with a 'query' to search "
                "(returns snippets of matching entries). Use with an 'entry_id' to fetch "
                "the full content of a specific entry after searching."
            ),
            usage=_MEMORY_TOOL_USAGE["Recall"],
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query to find relevant memories."},
                    "entry_id": {
                        "type": "string",
                        "description": (
                            "ID of a specific memory entry to retrieve in full"
                            " (from previous Recall results)."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return when searching (default: 5).",
                    },
                },
                "required": [],
            },
        )
        async def recall(query: str | None = None, entry_id: str | None = None, top_k: int = 5) -> str:
            if entry_id:
                entry = await backend.get_memory(entry_id)
                if entry is None:
                    return f"(No memory found with entry_id: {entry_id}.)"
                return (
                    f"Memory entry {entry_id}:\n---\n{entry.content}\n---\n"
                    f"Tags: {entry.tags}\nCreated: {entry.created_at}"
                )
            if query:
                entries = await backend.search_memories(query, top_k=top_k)
                if not entries:
                    return f"(No memories found for '{query}'.)"
                results = []
                for e in entries:
                    snippet = e.content[:200] + "..." if len(e.content) > 200 else e.content
                    results.append(f"[{e.id}] {snippet}\n    Tags: {e.tags}")
                header = f"Found {len(entries)} memories for '{query}':\n\n"
                footer = '\n\nUse Recall(entry_id="...") to fetch the full content of any entry.'
                return header + "\n\n".join(results) + footer
            return "Error: Provide either 'query' to search or 'entry_id' to fetch a specific entry."

        @registry(
            name="Forget",
            description=(
                "Remove information from your memory. Use with an 'entry_id' to remove a specific "
                "memory. Use with a 'query' to search and remove all matching memories."
            ),
            usage=_MEMORY_TOOL_USAGE["Forget"],
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "ID of a specific memory entry to remove."},
                    "query": {"type": "string", "description": "Search query — all matching memories will be removed."},
                },
                "required": [],
            },
        )
        async def forget(entry_id: str | None = None, query: str | None = None) -> str:
            if entry_id:
                await backend.forget_memory(entry_id)
                return f"(Memory entry {entry_id} forgotten.)"
            if query:
                entries = await backend.search_memories(query, top_k=20)
                if not entries:
                    return f"(No memories found for '{query}' — nothing to forget.)"
                count = len(entries)
                for e in entries:
                    await backend.forget_memory(e.id)
                return (
                    f"(Forgot {count} memory entries matching '{query}'. "
                    f"If the user asked you to stop referencing something, consider using "
                    f'Remember(key="user", content="...") to record why you forgot it.)'
                )
            return "Error: Provide either 'entry_id' or 'query' to forget."

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:
        if not self._maxim_keys:
            return None
        items: dict[str, str] = {}
        for key in sorted(self._maxim_keys):
            content = await self._backend.get_maxim(key) or "(empty)"
            label = f"{key}: {_MAXIM_DESCRIPTIONS.get(key, '')}" if key in _MAXIM_DESCRIPTIONS else key
            items[label] = content
        if not items:
            return None
        section = "<active_maxims>\n"
        section += "\n\n".join([f"## {k}\n{v}" for k, v in items.items()])
        section += "\n</active_maxims>"
        return section

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        return []
