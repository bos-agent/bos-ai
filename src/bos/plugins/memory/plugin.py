"""MemoryHarnessPlugin and MemoryAgentPlugin."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from bos.core._utils import _allowed, _xml_attr
from bos.core.contract import (
    AgentPlugin,
    PluginServices,
    TurnInterceptor,
    ep_plugin,
)
from bos.core.registry import ExtensionPoint, ToolRegistry

from .scoped_memory import MemoryBackend

# Plugin-defined extension points use the `pep_` prefix (plugin extension
# point) to distinguish them from the core `ep_` points in bos.core.contract.
pep_memory_backend = ExtensionPoint(
    name="pep_memory_backend",
    description="Memory store implementations (MarkdownMemoryBackend, InMemMemoryExtension, etc.).",
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
- To retract a fact, Remember a negation; off-turn curation invalidates the stale entry.""",
    "Recall": """Retrieve information from episodic memory.

Use query to search for relevant memories, or entry_id to fetch a specific memory in full after
a search result identifies it. Use memory as context, not as proof of current repository state.

Guidelines:
- Recall when the user references prior conversations, preferences, or remembered context.
- Prefer current files, tests, and git history for facts about the repository.
- Verify any memory-derived file, symbol, or behavior claim before acting on it.""",
    "ReviseMaxim": """Append a revision note to a maxim. Existing content is preserved.

### Maxims

Deeply held convictions (e.g., user preferences, rules). Always visible in your context.
- Scope: Respect the keys ("user", "soul", "identity", "rules").
- Limits: 2048 chars total. Keep notes concise.
- Do NOT use for: Facts, snippets, meeting notes, one-off details.""",
}

_MEMORY_PROMPT_SECTION = """<memory_workflow>
Use memory tools for durable context that should help in future conversations, not for temporary task state.

- Use Recall when the user references prior conversations, preferences, remembered context, or memory actions.
- Treat recalled memories as context, not proof; verify repository facts against current files or git state.
- Use Remember for stable user preferences, recurring feedback, non-obvious project context, and useful outcomes.
- Do not remember facts that are derivable from the current repository, transient plans, or ordinary task progress.
- To stop using something, Remember it as a negation (e.g. "X is no longer true"); curation removes it
  off-turn — there is no destructive delete.
- Use ReviseMaxim only for compact, high-priority maxims that should remain visible every turn.
</memory_workflow>"""


@ep_plugin(name="MemoryPlugin")
class MemoryHarnessPlugin:
    @property
    def name(self) -> str:
        return "MemoryPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {
            "maxims": ["user", "soul", "identity", "rules"],
            "scope": "workspace",
            "backend": "_default",
            "retrieval": {"auto_recall": True, "index_in_prompt": True, "index_max": 50, "top_k": 5},
            "consolidation": {"enabled": False, "retention_days": 30, "auto_apply": False},
        }

    async def setup(self, services: PluginServices) -> None:
        from pathlib import Path

        from ._watermark import WatermarkStore
        from .consolidator import ConsolidationPolicy, DefaultMemoryConsolidator
        from .operation_service import DefaultMemoryOperationService

        self._services = services
        cfg = getattr(self, "_cfg", None) or dict(self.default_config())
        self._cfg = cfg

        # Eager backend (was lazy at bind() time; consolidation needs it at setup)
        backend_name = cfg.get("backend", "_default")
        backend_ext = pep_memory_backend.get(backend_name)
        if backend_ext is None:
            raise ValueError(f"MemoryPlugin: unknown backend {backend_name!r}")
        self._backend: MemoryBackend = backend_ext.fn(bos_dir=services.bos_dir)

        # L1 operation service + audit log under the memory store dir
        memory_dir = Path(services.bos_dir) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        self._maxim_keys = set(cfg.get("maxims", []))
        self._operation_service = DefaultMemoryOperationService(
            self._backend,
            audit_path=memory_dir / "audit.jsonl",
            maxim_keys=self._maxim_keys,
        )
        self._watermarks = WatermarkStore(memory_dir / "watermarks.json")
        self._consolidator = (
            DefaultMemoryConsolidator(services.background_llm, maxim_keys=self._maxim_keys)
            if services.background_llm is not None
            else None
        )

        cons_cfg = dict(cfg.get("consolidation", {}))
        self._policy = ConsolidationPolicy(
            enabled=bool(cons_cfg.get("enabled", False)),
            retention_days=int(cons_cfg.get("retention_days", 30)),
            auto_apply=bool(cons_cfg.get("auto_apply", False)),
        )
        self._scope = cfg.get("scope") or "workspace"

        if (
            self._policy.enabled
            and services.events is not None
            and services.jobs is not None
            and services.background_llm is not None
            and services.chat_store is not None
        ):
            services.jobs.bind_trigger("session_close", self._make_consolidation_job_factory())

        # Recall-log flush (BEP 10 §6): subscribe on turn_complete when auto_recall is on
        # OR when consolidation is enabled (both want fresh last_used signal).
        retrieval_cfg = dict(cfg.get("retrieval", {}))
        if services.events is not None and (
            retrieval_cfg.get("auto_recall", True) or self._policy.enabled
        ):
            from .recall_flush import RecallFlushSubscriber

            services.events.subscribe(
                "turn_complete", RecallFlushSubscriber(self._operation_service).handle,
            )

    def _make_consolidation_job_factory(self):
        from .job import MemoryConsolidationJob

        def factory(event):
            if event is None:
                return None
            return MemoryConsolidationJob(
                scope=self._scope, chat_id=event.chat_id, actor_name=event.actor_name,
                base_revision=int(event.base_revision or 0), trigger="session_close",
                policy=self._policy, chat_store=self._services.chat_store,
                backend=self._backend, consolidator=self._consolidator,
                operation_service=self._operation_service, watermarks=self._watermarks,
                maxim_keys=self._maxim_keys,
            )

        return factory

    async def run_consolidation_now(self, chat_id: str, *, dry_run: bool | None = None):
        """Build and run a consolidation job synchronously (admin "run now")."""
        from .consolidator import ConsolidationPolicy
        from .job import MemoryConsolidationJob

        policy = self._policy
        if dry_run is not None:
            policy = ConsolidationPolicy(
                enabled=policy.enabled, retention_days=policy.retention_days,
                auto_apply=not dry_run,
            )
        rev = await self._services.chat_store.get_revision(chat_id)
        if rev == 0:
            return []
        before = len(await self._operation_service.audit())
        job = MemoryConsolidationJob(
            scope=self._scope, chat_id=chat_id, actor_name=None,
            base_revision=rev, trigger="manual", policy=policy,
            chat_store=self._services.chat_store, backend=self._backend,
            consolidator=self._consolidator,
            operation_service=self._operation_service,
            watermarks=self._watermarks, maxim_keys=self._maxim_keys,
        )
        await job.run()
        return (await self._operation_service.audit())[before:]

    def validate_config(self, config: Mapping[str, Any]) -> None:
        maxims = config.get("maxims", [])
        if not isinstance(maxims, list):
            raise TypeError("MemoryPlugin: 'maxims' must be a list")
        scope = config.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise TypeError("MemoryPlugin: 'scope' must be a string")
        if isinstance(scope, str) and not scope.strip():
            raise ValueError("MemoryPlugin: 'scope' must not be empty")

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        backend = self._backend
        if backend is None:
            # Defensive — setup() should have constructed this; fall back to lazy.
            backend_name = config.get("backend", "_default")
            backend_ext = pep_memory_backend.get(backend_name)
            if backend_ext is None:
                raise ValueError(f"MemoryPlugin: unknown backend {backend_name!r}")
            backend = self._backend = backend_ext.fn(bos_dir=self._services.bos_dir)
        scope = config.get("scope")
        if scope and scope != "workspace":
            from .scoped_memory import ScopedMemory

            backend = ScopedMemory(backend, scope)
        maxim_keys = set(config.get("maxims", []))
        retrieval = dict(config.get("retrieval", {}))
        return MemoryAgentPlugin(
            backend, maxim_keys,
            index_in_prompt=retrieval.get("index_in_prompt", True),
            index_max=retrieval.get("index_max", 50),
            auto_recall=retrieval.get("auto_recall", True),
            top_k=retrieval.get("top_k", 5),
        )

    async def teardown(self) -> None:
        from bos.core._utils import _aclose

        await _aclose(getattr(self, "_backend", None))


class MemoryAgentPlugin:
    def __init__(
        self, backend: MemoryBackend, maxim_keys: set[str], *,
        index_in_prompt: bool = True, index_max: int = 50,
        auto_recall: bool = True, top_k: int = 5,
    ) -> None:
        self._backend = backend
        self._maxim_keys = maxim_keys
        self._index_in_prompt = index_in_prompt
        self._index_max = index_max
        self._auto_recall = auto_recall
        self._top_k = top_k
        self._cached_turn_id: str | None = None
        self._cached_section: str | None = None

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
                            "ID of a specific memory entry to retrieve in full (from previous Recall results)."
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

    async def _render_section(self) -> str:
        sections = [_MEMORY_PROMPT_SECTION]
        if self._index_in_prompt:
            index = await self._backend.list_index()
            if index:
                items = "\n".join(
                    f'<index_entry id="{_xml_attr(ie.id)}" tags="{_xml_attr(",".join(ie.tags))}">'
                    f"{escape(ie.summary).strip()}</index_entry>"
                    for ie in index[: self._index_max]
                )
                sections.append(f"<memory_index>\n{items}\n</memory_index>")
        if self._maxim_keys:
            items = []
            for key in sorted(self._maxim_keys):
                content = await self._backend.get_maxim(key)
                scope = _MAXIM_DESCRIPTIONS.get(key, "")
                items.append(
                    f'<maxim name="{_xml_attr(key)}" scope="{_xml_attr(scope)}">\n'
                    f"{escape(content).strip()}\n</maxim>"
                )
            sections.append("<active_maxims>\n" + "\n".join(items) + "\n</active_maxims>")
        return "\n\n".join(sections)

    async def get_system_prompt_section(self, context: TurnContext) -> str | None:
        turn_id = getattr(context, "turn_id", None)
        if turn_id is not None and turn_id == self._cached_turn_id:
            return self._cached_section
        section = await self._render_section()
        if turn_id is not None:
            self._cached_turn_id = turn_id
            self._cached_section = section
        return section

    def get_interceptors(self) -> Sequence[TurnInterceptor]:
        if not self._auto_recall:
            return []
        from .auto_recall import AutoRecallInterceptor

        return [AutoRecallInterceptor(self._backend, top_k=self._top_k)]
