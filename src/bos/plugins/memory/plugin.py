"""MemoryHarnessPlugin and MemoryAgentPlugin."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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
    from collections.abc import Callable

    from bos.core.agent import TurnContext
    from bos.core.contract import Job, SessionEvent

    from ._watermark import WatermarkStore
    from .consolidator import DefaultMemoryConsolidator
    from .job import TriggerName
    from .operation_service import DefaultMemoryOperationService

MAXIM_LIMIT = 2048

_MAXIM_DESCRIPTIONS = {
    "user": "your knowledge about the user — preferences, background, projects, style",
    "self": "who you are and how you work — your role, purpose, character, and operating philosophy",
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
- Scope: Respect the keys ("user", "self", "rules").
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


# Cap on in-flight per-turn recall buffers. A turn's entry is popped when its
# turn_complete fires; only turns that never complete (errored/aborted) leak,
# so a small FIFO bound keeps the map from growing without flushing.
_RECALL_BUFFER_CAP = 512


@dataclass
class _PerAgentMemory:
    """All scoped-to-one-agent memory state (Ω: storage-level isolation)."""

    backend: MemoryBackend
    op_service: DefaultMemoryOperationService
    watermarks: WatermarkStore
    consolidator: DefaultMemoryConsolidator | None
    # Recalled entry-ids surfaced during a turn, keyed by turn_id. The
    # auto-recall interceptor fills it; the turn_complete flush drains it.
    recalled_by_turn: dict[str, list[str]] = field(default_factory=dict)

    def record_recalled(self, turn_id: str, ids: list[str]) -> None:
        self.recalled_by_turn[turn_id] = list(ids)
        while len(self.recalled_by_turn) > _RECALL_BUFFER_CAP:
            # Drop the oldest still-unflushed turn (insertion-ordered dict).
            del self.recalled_by_turn[next(iter(self.recalled_by_turn))]


@ep_plugin(name="MemoryPlugin")
class MemoryHarnessPlugin:
    @property
    def name(self) -> str:
        return "MemoryPlugin"

    def default_config(self) -> Mapping[str, Any]:
        return {
            "maxims": ["user", "self", "rules"],
            "backend": "_default",
            "retrieval": {"auto_recall": True, "index_in_prompt": True, "index_max": 50, "top_k": 5},
            "consolidation": {"enabled": False, "retention_days": 30},
        }

    async def setup(self, services: PluginServices) -> None:
        from .consolidator import ConsolidationPolicy

        self._services = services
        cfg = getattr(self, "_cfg", None) or dict(self.default_config())
        self._cfg = cfg

        self._backend_name = cfg.get("backend", "_default")
        self._backend_ext = pep_memory_backend.get(self._backend_name)
        if self._backend_ext is None:
            raise ValueError(f"MemoryPlugin: unknown backend {self._backend_name!r}")
        self._maxim_keys = set(cfg.get("maxims", []))

        # Per-agent memory subtrees: built lazily on first bind() per agent_name.
        self._per_agent: dict[str, _PerAgentMemory] = {}

        cons_cfg = dict(cfg.get("consolidation", {}))
        self._policy = ConsolidationPolicy(
            enabled=bool(cons_cfg.get("enabled", False)),
            retention_days=int(cons_cfg.get("retention_days", 30)),
        )

        if (
            self._policy.enabled
            and services.events is not None
            and services.jobs is not None
            and services.background_llm is not None
            and services.chat_store is not None
        ):
            services.jobs.bind_trigger("session_close", self._make_consolidation_job_factory("session_close"))
            # Off-turn consolidation after a chat goes quiet. The runner arms a
            # per-chat idle timer on each turn_complete (default 5 min); when it
            # lapses with no new turn it fires this factory for that chat.
            services.jobs.bind_trigger("idle", self._make_consolidation_job_factory("idle"))

        # Recall-log flush (BEP 10 §6): on turn_complete, dispatch to the
        # event.actor_name's bundle. The bundle holds the ids its own
        # interceptor recorded this turn — the platform's turn_complete event
        # is the generic trigger; it carries no memory-specific payload.
        retrieval_cfg = dict(cfg.get("retrieval", {}))
        if services.events is not None and (retrieval_cfg.get("auto_recall", True) or self._policy.enabled):
            services.events.subscribe("turn_complete", self._handle_turn_complete_flush)

    def _build_for(self, agent_name: str) -> _PerAgentMemory:
        """Construct an isolated memory subsystem for one agent."""
        from ._watermark import WatermarkStore
        from .consolidator import DefaultMemoryConsolidator
        from .operation_service import DefaultMemoryOperationService

        assert self._backend_ext is not None  # validated in setup(): unknown backend raises there
        backend = self._backend_ext.fn(
            bos_dir=self._services.bos_dir,
            store_dir=f"memory/agents/{agent_name}",
        )
        agent_dir = Path(self._services.bos_dir) / "memory" / "agents" / agent_name
        op_service = DefaultMemoryOperationService(
            backend,
            audit_path=agent_dir / "audit.jsonl",
            maxim_keys=self._maxim_keys,
        )
        watermarks = WatermarkStore(agent_dir / "watermarks.json")
        consolidator = (
            DefaultMemoryConsolidator(self._services.background_llm, maxim_keys=self._maxim_keys)
            if self._services.background_llm is not None
            else None
        )
        return _PerAgentMemory(backend, op_service, watermarks, consolidator)

    def _for(self, agent_name: str) -> _PerAgentMemory:
        if agent_name not in self._per_agent:
            self._per_agent[agent_name] = self._build_for(agent_name)
        return self._per_agent[agent_name]

    async def _handle_turn_complete_flush(self, event) -> None:
        from .recall_flush import RecallFlushSubscriber

        if not event.actor_name:
            return
        bundle = self._per_agent.get(event.actor_name)
        if bundle is None:
            # The actor was never bound (e.g., emit from a chat that never
            # ran through this harness). Nothing to flush.
            return
        turn_id = getattr(event, "turn_id", None)
        recalled = bundle.recalled_by_turn.pop(turn_id, []) if turn_id else []
        await RecallFlushSubscriber(bundle.op_service).flush(recalled, chat_id=event.chat_id)

    def _make_consolidation_job_factory(self, trigger: TriggerName = "session_close"):
        from .job import MemoryConsolidationJob

        def factory(event: SessionEvent | None) -> Job | None:
            if event is None or event.base_revision is None or not event.actor_name:
                return None
            bundle = self._per_agent.get(event.actor_name)
            if bundle is None or bundle.consolidator is None:
                return None
            return MemoryConsolidationJob(
                actor_name=event.actor_name,
                chat_id=event.chat_id,
                base_revision=int(event.base_revision),
                trigger=trigger,
                policy=self._policy,
                chat_store=self._services.chat_store,
                backend=bundle.backend,
                consolidator=bundle.consolidator,
                operation_service=bundle.op_service,
                watermarks=bundle.watermarks,
                maxim_keys=self._maxim_keys,
            )

        return factory

    async def run_consolidation_now(
        self,
        chat_id: str,
        *,
        agent_name: str,
    ):
        """Build and run a consolidation job synchronously (admin "run now")."""
        from .job import MemoryConsolidationJob

        bundle = self._for(agent_name)
        if bundle.consolidator is None:
            return []
        policy = self._policy
        rev = await self._services.chat_store.get_revision(chat_id)
        if rev == 0:
            return []
        before = len(await bundle.op_service.audit())
        job = MemoryConsolidationJob(
            actor_name=agent_name,
            chat_id=chat_id,
            base_revision=rev,
            trigger="manual",
            policy=policy,
            chat_store=self._services.chat_store,
            backend=bundle.backend,
            consolidator=bundle.consolidator,
            operation_service=bundle.op_service,
            watermarks=bundle.watermarks,
            maxim_keys=self._maxim_keys,
        )
        await job.run()
        return (await bundle.op_service.audit())[before:]

    def validate_config(self, config: Mapping[str, Any]) -> None:
        maxims = config.get("maxims", [])
        if not isinstance(maxims, list):
            raise TypeError("MemoryPlugin: 'maxims' must be a list")
        if "scope" in config:
            raise ValueError(
                "MemoryPlugin: 'scope' is no longer a config option. Each agent's "
                "memory is now isolated by its agent_name automatically (Ω: per-agent "
                "backends). Remove the 'scope' setting from your config."
            )

    def bind(self, config: Mapping[str, Any]) -> AgentPlugin:
        agent_name = config.get("agent_name")
        if not agent_name or not isinstance(agent_name, str):
            raise ValueError(
                "MemoryPlugin.bind: missing 'agent_name' in config. The harness "
                "is expected to inject it; ensure _bind_plugins_for_agent is up to date."
            )
        bundle = self._for(agent_name)
        retrieval = dict(config.get("retrieval", {}))
        return MemoryAgentPlugin(
            bundle.backend,
            self._maxim_keys,
            index_in_prompt=retrieval.get("index_in_prompt", True),
            index_max=retrieval.get("index_max", 50),
            auto_recall=retrieval.get("auto_recall", True),
            top_k=retrieval.get("top_k", 5),
            on_recalled=bundle.record_recalled,
        )

    async def teardown(self) -> None:
        from bos.core._utils import _aclose

        await _aclose(getattr(self, "_backend", None))


class MemoryAgentPlugin:
    def __init__(
        self,
        backend: MemoryBackend,
        maxim_keys: set[str],
        *,
        index_in_prompt: bool = True,
        index_max: int = 50,
        auto_recall: bool = True,
        top_k: int = 5,
        on_recalled: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        self._backend = backend
        self._maxim_keys = maxim_keys
        self._index_in_prompt = index_in_prompt
        self._index_max = index_max
        self._auto_recall = auto_recall
        self._top_k = top_k
        self._on_recalled = on_recalled
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
            # The agent just changed its own memory; bust the per-turn section
            # cache so a later iteration of this same turn sees the new entry.
            self._invalidate_section_cache()
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
                    "key": {"type": "string", "description": "Maxim key. One of: user, self, rules."},
                    "content": {"type": "string", "description": "The revision note to append."},
                },
                "required": ["key", "content"],
            },
        )
        async def revise_maxim(key: str, content: str) -> str:
            if not _allowed(key.lower(), maxim_keys):
                return f"Error: Maxim '{key}' is not allowed."
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            # Atomic read-append-write (enforcing the size cap inside the lock) so a
            # concurrent consolidation PROMOTE on the same maxim cannot be lost.
            written, length = await backend.append_to_maxim(key.lower(), f"[{ts}] {content}", max_len=MAXIM_LIMIT)
            if not written:
                return (
                    f"Error: Revision would bring maxim '{key}' to "
                    f"{length} characters (limit {MAXIM_LIMIT}). "
                    f"Wait for a merge cycle or keep it shorter."
                )
            # Same-turn visibility: bust the cached section so the revised maxim
            # is reflected on the next iteration of this turn.
            self._invalidate_section_cache()
            return f"(Revision appended to maxim '{key}'. Total size: {length}/{MAXIM_LIMIT} characters.)"

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
                    f'<maxim name="{_xml_attr(key)}" scope="{_xml_attr(scope)}">\n{escape(content).strip()}\n</maxim>'
                )
            sections.append("<active_maxims>\n" + "\n".join(items) + "\n</active_maxims>")
        return "\n\n".join(sections)

    def _invalidate_section_cache(self) -> None:
        """Drop the per-turn section cache so the next get_system_prompt_section
        re-renders. Called by the agent's own write tools (Remember/ReviseMaxim)
        so a mid-turn memory edit is visible on the turn's next iteration. Note:
        external/off-turn backend writes deliberately do NOT invalidate — the
        section stays byte-stable within a turn for prompt-cache reuse."""
        self._cached_turn_id = None
        self._cached_section = None

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

        return [AutoRecallInterceptor(self._backend, top_k=self._top_k, on_recalled=self._on_recalled)]
