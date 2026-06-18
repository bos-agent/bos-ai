"""``boscli memory`` — read + operate on the memory backend."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import click

import bos.exts  # noqa: F401 — registers default ep impls (chat_store, mail_route, ...)
from bos.cli.commands.scaffolding import _discover_project
from bos.plugins.memory.plugin import MemoryHarnessPlugin


def _memory_config(ws) -> dict[str, Any]:
    """Resolve the MemoryPlugin config: defaults overlaid with user [exts.ep_plugin.MemoryPlugin]."""
    plugin = MemoryHarnessPlugin()
    cfg = dict(plugin.default_config())
    if getattr(ws.config, "exts", None) is not None:
        user = ws.config.exts.model_dump().get("ep_plugin", {}).get("MemoryPlugin", {}) or {}
        cfg.update(user)
    return cfg


def _run(coro_factory: Callable[[MemoryHarnessPlugin, Any], Awaitable[Any]]) -> Any:
    """Open harness + memory plugin, run coro_factory(plugin, harness), close — one event loop."""

    async def _do():
        ws = _discover_project()
        h = ws.harness()
        await h.__aenter__()
        try:
            plugin = MemoryHarnessPlugin()
            plugin._cfg = _memory_config(ws)
            await plugin.setup(h._plugin_services)
            return await coro_factory(plugin, h)
        finally:
            await h.__aexit__(None, None, None)

    return asyncio.run(_do())


@click.group(name="memory")
def memory():
    """Memory backend admin commands."""


@memory.command("list")
@click.option("--limit", default=20, show_default=True, help="Max entries to show.")
def list_cmd(limit: int):
    """List active memory entries (importance-ordered)."""

    async def _list(plugin, _h):
        entries = await plugin._backend.search_memories("", top_k=limit)
        if not entries:
            click.echo("(no memories)")
            return
        for e in entries:
            click.echo(f"{e.id}  imp={e.metadata.get('importance', 5)}  {e.content[:80]}")

    _run(_list)


@memory.command("show")
@click.argument("entry_id")
def show_cmd(entry_id: str):
    """Show full content of a memory entry by id."""

    async def _show(plugin, _h):
        e = await plugin._backend.get_memory(entry_id, include_invalid=True)
        if e is None:
            click.echo(f"(entry {entry_id} not found)")
            return
        click.echo(f"id: {e.id}")
        click.echo(f"tags: {e.tags}")
        click.echo(f"created_at: {e.created_at}")
        click.echo(f"metadata: {json.dumps(e.metadata, indent=2, sort_keys=True)}")
        click.echo("---")
        click.echo(e.content)

    _run(_show)


@memory.command("index")
def index_cmd():
    """Print the in-context index (id, tags, summary), importance-ordered."""

    async def _idx(plugin, _h):
        idx = await plugin._backend.list_index()
        if not idx:
            click.echo("(empty index)")
            return
        for ie in idx:
            tags = ",".join(ie.tags) if ie.tags else ""
            click.echo(f"{ie.id}  [{tags}]  {ie.summary}")

    _run(_idx)


@memory.command("recall")
@click.option("--query", required=True, help="Search query.")
@click.option("--top-k", default=5, show_default=True)
def recall_cmd(query: str, top_k: int):
    """Search active memories — what the agent would retrieve."""

    async def _recall(plugin, _h):
        hits = await plugin._backend.search_memories(query, top_k=top_k)
        if not hits:
            click.echo(f"(no results for {query!r})")
            return
        for e in hits:
            snip = e.content[:160] + ("…" if len(e.content) > 160 else "")
            click.echo(f"{e.id}  imp={e.metadata.get('importance', 5)}  {snip}")

    _run(_recall)


@memory.command("consolidate")
@click.option("--chat", "chat_id", default=None, help="Chat id to consolidate (default: --all).")
@click.option("--all", "do_all", is_flag=True, default=False, help="Iterate every chat past its watermark.")
@click.option(
    "--dry-run/--apply",
    default=True,
    show_default=True,
    help="Dry-run validates + audits but does not mutate the backend.",
)
def consolidate_cmd(chat_id: str | None, do_all: bool, dry_run: bool):
    """Run the consolidation handler for one chat or all chats with unprocessed turns."""

    async def _consolidate(plugin, _h):
        targets: list[str] = []
        if do_all:
            chats = await plugin._services.chat_store.list_chats()
            targets = list(chats.keys())
        elif chat_id:
            targets = [chat_id]
        else:
            click.echo("(specify --chat ID or --all)")
            return
        for cid in targets:
            records = await plugin.run_consolidation_now(cid, dry_run=dry_run)
            applied = sum(1 for r in records if r.result == "applied")
            drun = sum(1 for r in records if r.result == "dry_run")
            rej = sum(1 for r in records if r.result == "rejected")
            click.echo(f"chat {cid}: consolidated — applied={applied} dry_run={drun} rejected={rej}")

    _run(_consolidate)


@memory.command("restore")
@click.argument("entry_id")
def restore_cmd(entry_id: str):
    """Restore (un-invalidate) a soft-deleted memory entry."""

    async def _restore(plugin, _h):
        entry = await plugin._backend.get_memory(entry_id, include_invalid=True)
        if entry is None:
            click.echo(f"(entry {entry_id} not found)")
            return
        await plugin._operation_service.restore(entry_id)
        click.echo(f"restored {entry_id}")

    _run(_restore)


@memory.command("audit")
@click.option("--filter", "filter_str", default=None, help="key=value filter, e.g. result=applied or op=ADD")
def audit_cmd(filter_str: str | None):
    """Print the in-memory audit log (operations applied this process)."""

    async def _audit(plugin, _h):
        filt: dict | None = None
        if filter_str:
            k, _, v = filter_str.partition("=")
            filt = {k: v}
        records = await plugin._operation_service.audit(filter=filt)
        if not records:
            click.echo("(no audit records)")
            return
        for r in records:
            click.echo(f"{r.at}  {r.op.op}  result={r.result}  entry={r.entry_id}  reason={r.op.reason!r}")

    _run(_audit)


@memory.command("jobs")
@click.option("--status", default=None, help="Filter by status: queued|running|succeeded|failed|cancelled")
def jobs_cmd(status: str | None):
    """List JobRunner records for this harness process."""

    async def _jobs(_plugin, h):
        filt = {"status": status} if status else None
        recs = await h.jobs.list(filter=filt)
        if not recs:
            click.echo("(no jobs)")
            return
        for r in recs:
            click.echo(f"{r.submitted_at}  {r.status:10s}  {r.id[:8]}  {r.key}")

    _run(_jobs)
