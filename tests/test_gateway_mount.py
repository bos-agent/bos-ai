"""GatewayMount: single-instance states and the lock watchdog (BEP 17 §3.4)."""

from __future__ import annotations

import asyncio

import pytest

from bos.config import Workspace
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway.state import GatewayRunDir, acquire_singleton_lock
from bos.runner.mount import GatewayMount


def _workspace(tmp_path):
    return Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {"port": 0, "shutdown_grace_seconds": 0.1},
                "main_actor": "main",
                "actors": {"main": {"agent": "main"}},
            },
            # Named rather than imported for their side effects, so the
            # registrations these classes carry are provably the ones in use.
            "harness": {"chat_store": InMemChatStore.__name__, "mail_route": InMemMailRoute.__name__},
            "platform": {"extensions": []},
            "agents": {"main": {"system_prompt": "hi"}},
        },
    )


@pytest.mark.asyncio
async def test_mount_reaches_live_and_holds_the_lock(tmp_path):
    mount = GatewayMount(lambda: _workspace(tmp_path))
    await mount.start()
    try:
        assert mount.state == "live"
        assert mount.gateway is not None
        # A second acquirer must be refused while we hold it.
        assert acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos")) is None
    finally:
        await mount.stop()
    assert mount.state == "stopped"


@pytest.mark.asyncio
async def test_mount_goes_standby_when_the_lock_is_taken(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    rd.pid_file.write_text("4242", encoding="utf-8")

    mount = GatewayMount(lambda: _workspace(tmp_path))
    await mount.start()
    try:
        assert mount.state == "standby"
        assert mount.gateway is None
        assert mount.status()["holder_pid"] == 4242
    finally:
        await mount.stop()
        holder.close()


@pytest.mark.asyncio
async def test_standby_is_promoted_once_the_lock_frees(tmp_path):
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None

    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=0.05)
    await mount.start()
    try:
        assert mount.state == "standby"
        holder.close()  # the other "process" exits
        for _ in range(100):
            if mount.state == "live":
                break
            await asyncio.sleep(0.05)
        assert mount.state == "live"
        assert mount.gateway is not None
    finally:
        await mount.stop()


@pytest.mark.asyncio
async def test_standby_serves_status_without_a_gateway(tmp_path):
    """The app is built once and indirects through the mount, so a standby can
    answer /api/status and refuse websockets while holding no runtime."""
    from aiohttp import web

    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None

    mount = GatewayMount(lambda: _workspace(tmp_path), runtime_label="embedded")
    await mount.start()
    try:
        assert mount.state == "standby"
        app = mount.build_app()
        assert isinstance(app, web.Application)
        assert mount.build_app() is app  # built once, kept across restarts
        assert mount.status()["state"] == "standby"
        assert mount.status()["runtime"] == "embedded"
    finally:
        await mount.stop()
        holder.close()
