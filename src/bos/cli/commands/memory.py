"""``boscli memory`` — read + operate on the memory backend.

Each invocation targets a single agent's isolated memory subtree (Ω). Use
``--agent NAME`` to pick the agent; defaults to the workspace's
``runtime.default_actor`` if configured."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import click

import bos.exts  # noqa: F401 — registers default ep impls (chat_store, mail_route, ...)
from bos.cli.commands.scaffolding import _discover_project
from bos.core import _deep_merge
from bos.plugins.memory.plugin import MemoryHarnessPlugin, _PerAgentMemory


def _memory_config(ws) -> dict[str, Any]:
    """Resolve the MemoryPlugin config: defaults overlaid with user [exts.ep_plugin.MemoryPlugin].

    Deep-merges so setting one key under a nested section (e.g. [retrieval]) keeps
    that section's other defaults instead of replacing the whole sub-dict."""
    plugin = MemoryHarnessPlugin()
    cfg = dict(plugin.default_config())
    if getattr(ws.config, "exts", None) is not None:
        user = ws.config.exts.model_dump().get("ep_plugin", {}).get("MemoryPlugin", {}) or {}
        cfg = _deep_merge(cfg, user)
    return cfg


def _resolve_agent_name(ws, explicit: str | None) -> str:
    """Pick the agent name to operate on. Explicit wins; else workspace default; else 'default'."""
    if explicit:
        return explicit
    try:
        return ws.resolve_default_actor()
    except Exception:
        return "default"


def _run(
    coro_factory: Callable[[_PerAgentMemory, MemoryHarnessPlugin, Any], Awaitable[Any]],
    *,
    agent: str | None,
) -> Any:
    """Open harness + memory plugin, resolve the agent's bundle, run coro_factory, close."""

    async def _do():
        ws = _discover_project()
        agent_name = _resolve_agent_name(ws, agent)
        h = ws.harness()
        await h.__aenter__()
        try:
            plugin = MemoryHarnessPlugin()
            plugin._cfg = _memory_config(ws)
            await plugin.setup(h._plugin_services)
            bundle = plugin._for(agent_name)
            return await coro_factory(bundle, plugin, h)
        finally:
            await h.__aexit__(None, None, None)

    return asyncio.run(_do())


_AGENT_OPT = click.option(
    "--agent",
    "agent",
    default=None,
    help="Agent whose memory to operate on (default: workspace's runtime.default_actor).",
)


@click.group(name="memory")
def memory():
    """Memory backend admin commands."""


@memory.command("list")
@_AGENT_OPT
@click.option("--limit", default=20, show_default=True, help="Max entries to show.")
def list_cmd(agent: str | None, limit: int):
    """List active memory entries (importance-ordered)."""

    async def _list(bundle, _plugin, _h):
        entries = await bundle.backend.search_memories("", top_k=limit)
        if not entries:
            click.echo("(no memories)")
            return
        for e in entries:
            click.echo(f"{e.id}  imp={e.metadata.get('importance', 5)}  {e.content[:80]}")

    _run(_list, agent=agent)


@memory.command("show")
@_AGENT_OPT
@click.argument("entry_id")
def show_cmd(agent: str | None, entry_id: str):
    """Show full content of a memory entry by id."""

    async def _show(bundle, _plugin, _h):
        e = await bundle.backend.get_memory(entry_id, include_invalid=True)
        if e is None:
            click.echo(f"(entry {entry_id} not found)")
            return
        click.echo(f"id: {e.id}")
        click.echo(f"tags: {e.tags}")
        click.echo(f"created_at: {e.created_at}")
        click.echo(f"metadata: {json.dumps(e.metadata, indent=2, sort_keys=True)}")
        click.echo("---")
        click.echo(e.content)

    _run(_show, agent=agent)


@memory.command("index")
@_AGENT_OPT
def index_cmd(agent: str | None):
    """Print the in-context index (id, tags, summary), importance-ordered."""

    async def _idx(bundle, _plugin, _h):
        idx = await bundle.backend.list_index()
        if not idx:
            click.echo("(empty index)")
            return
        for ie in idx:
            tags = ",".join(ie.tags) if ie.tags else ""
            click.echo(f"{ie.id}  [{tags}]  {ie.summary}")

    _run(_idx, agent=agent)


@memory.command("recall")
@_AGENT_OPT
@click.option("--query", required=True, help="Search query.")
@click.option("--top-k", default=5, show_default=True)
def recall_cmd(agent: str | None, query: str, top_k: int):
    """Search active memories — what the agent would retrieve."""

    async def _recall(bundle, _plugin, _h):
        hits = await bundle.backend.search_memories(query, top_k=top_k)
        if not hits:
            click.echo(f"(no results for {query!r})")
            return
        for e in hits:
            snip = e.content[:160] + ("…" if len(e.content) > 160 else "")
            click.echo(f"{e.id}  imp={e.metadata.get('importance', 5)}  {snip}")

    _run(_recall, agent=agent)


@memory.command("consolidate")
@_AGENT_OPT
@click.option("--chat", "chat_id", default=None, help="Chat id to consolidate (default: --all).")
@click.option("--all", "do_all", is_flag=True, default=False, help="Iterate every chat past its watermark.")
def consolidate_cmd(agent: str | None, chat_id: str | None, do_all: bool):
    """Run the consolidation handler for one chat or all chats with unprocessed turns."""

    async def _consolidate(_bundle, plugin, _h):
        targets: list[str] = []
        if do_all:
            chats = await plugin._services.chat_store.list_chats()
            targets = list(chats.keys())
        elif chat_id:
            targets = [chat_id]
        else:
            click.echo("(specify --chat ID or --all)")
            return
        # Resolve once at this layer (the closure has the agent name via _run)
        resolved_agent = _resolve_agent_name(_discover_project(), agent)
        for cid in targets:
            records = await plugin.run_consolidation_now(cid, agent_name=resolved_agent)
            applied = sum(1 for r in records if r.result == "applied")
            rej = sum(1 for r in records if r.result == "rejected")
            click.echo(f"agent {resolved_agent} / chat {cid}: applied={applied} rejected={rej}")

    _run(_consolidate, agent=agent)


@memory.command("restore")
@_AGENT_OPT
@click.argument("entry_id")
def restore_cmd(agent: str | None, entry_id: str):
    """Restore (un-invalidate) a soft-deleted memory entry."""

    async def _restore(bundle, _plugin, _h):
        entry = await bundle.backend.get_memory(entry_id, include_invalid=True)
        if entry is None:
            click.echo(f"(entry {entry_id} not found)")
            return
        await bundle.op_service.restore(entry_id)
        click.echo(f"restored {entry_id}")

    _run(_restore, agent=agent)


@memory.command("audit")
@_AGENT_OPT
@click.option("--filter", "filter_str", default=None, help="key=value filter, e.g. result=applied or op=ADD")
def audit_cmd(agent: str | None, filter_str: str | None):
    """Print the in-memory audit log (operations applied this process)."""

    async def _audit(bundle, _plugin, _h):
        filt: dict | None = None
        if filter_str:
            k, _, v = filter_str.partition("=")
            filt = {k: v}
        records = await bundle.op_service.audit(filter=filt)
        if not records:
            click.echo("(no audit records)")
            return
        for r in records:
            click.echo(
                f"{r.at}  {r.op.op}  result={r.result}  entry={r.entry_id}  "
                f"reason={r.op.reason!r}  window_turn_ids={r.window_turn_ids}"
            )

    _run(_audit, agent=agent)


@memory.command("jobs")
@click.option("--status", default=None, help="Filter by status: queued|running|succeeded|failed|cancelled")
def jobs_cmd(status: str | None):
    """List JobRunner records for this harness process."""

    async def _jobs(_bundle, _plugin, h):
        filt = {"status": status} if status else None
        recs = await h.jobs.list(filter=filt)
        if not recs:
            click.echo("(no jobs)")
            return
        for r in recs:
            click.echo(f"{r.submitted_at}  {r.status:10s}  {r.id[:8]}  {r.key}")

    _run(_jobs, agent=None)
