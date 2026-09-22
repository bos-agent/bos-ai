"""GatewayMount: single-instance states and the lock watchdog (BEP 17 §3.4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from bos.config import Workspace
from bos.config.schema import AgentConfig, ChannelConfig
from bos.core import BaseChannel, MailBox, ep_channel
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.mailboxes.in_memory import InMemMailRoute
from bos.gateway.state import GatewayRunDir, acquire_singleton_lock
from bos.runner.mount import GatewayMount


@dataclass(frozen=True)
class EchoSettings:
    pass


@ep_channel(name="EchoChannel")
class EchoChannel(BaseChannel[EchoSettings]):
    """A persistent channel that exists only to be created and cancelled, so a
    restart's channel rebuild is observable."""

    SettingsType = EchoSettings

    async def run(self, mailbox: MailBox) -> None:
        await asyncio.Event().wait()


def _workspace(tmp_path, grace: float = 0.1):
    return Workspace(
        tmp_path,
        tmp_path / ".bos",
        {
            "runtime": {
                "gateway": {"port": 0, "shutdown_grace_seconds": grace},
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
    import json

    import httpx
    from conftest import serve_asgi
    from starlette.applications import Starlette
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    rd = GatewayRunDir(tmp_path / ".bos")
    rd.ensure()
    holder = acquire_singleton_lock(rd)
    assert holder is not None

    mount = GatewayMount(lambda: _workspace(tmp_path), runtime_label="embedded")
    await mount.start()
    try:
        assert mount.state == "standby"
        app = mount.build_app()
        assert isinstance(app, Starlette)
        assert mount.build_app() is app  # built once, kept across restarts
        assert mount.status()["state"] == "standby"
        assert mount.status()["runtime"] == "embedded"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            status = await client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["state"] == "standby"

        # …and refuse websockets. Production reaches /ws through the mount's
        # dispatcher, never Gateway.build_app(), so this gate is the only thing
        # standing between a client and a consumer that does not exist.
        async with serve_asgi(app) as addr:
            with pytest.raises(InvalidStatus) as excinfo:
                async with connect(f"ws://{addr}/ws?channel_id=early"):
                    pass
        assert excinfo.value.response.status_code == 503
        assert json.loads(excinfo.value.response.body) == {"ok": False, "error": "standby"}
    finally:
        await mount.stop()
        holder.close()


@pytest.mark.asyncio
async def test_demotion_releases_the_socket_instead_of_stranding_serve(tmp_path):
    """Losing the lock must end the serving, not wedge the process.

    ``Gateway.stop()`` never sets the shutdown event, so a ``serve()`` parked on
    the gateway it captured would stay pending forever after the watchdog tore
    that gateway down — holding the port in front of no runtime, while the
    successor that actually won the lock dies on EADDRINUSE.
    """
    import socket

    from bos.runner.runner import serve

    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=0.05)
    await mount.start()
    assert mount.state == "live"

    served = asyncio.ensure_future(serve(mount))
    for _ in range(200):
        if mount.gateway is not None and mount.gateway.actual_port != 0:
            break
        await asyncio.sleep(0.02)
    assert mount.gateway is not None
    port = mount.gateway.actual_port
    assert port != 0

    # Another gateway replaced the lock file: our handle now locks an orphaned
    # inode, which is exactly what lock_still_owned exists to catch.
    GatewayRunDir(tmp_path / ".bos").lock_file.unlink()

    await asyncio.wait_for(served, timeout=10)
    assert mount.state == "stopped"
    assert mount.gateway is None
    with socket.socket() as probe:  # the port is free again
        probe.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_restart_picks_up_a_new_channel_and_keeps_the_lock(tmp_path):
    """A restart must rebuild the whole Gateway: create_persistent instantiates
    from the list Gateway.__init__ captured, so new [[runtime.channels]] entries
    are only seen by a new instance (BEP 17 §3.5.1)."""
    channels: list[ChannelConfig] = []

    def factory():
        ws = _workspace(tmp_path)
        ws.config.runtime.channels = list(channels)
        return ws

    mount = GatewayMount(factory, lock_poll_seconds=60)
    await mount.start()
    try:
        assert mount.state == "live"
        first = mount.gateway
        assert mount.gateway is not None
        assert mount.gateway.channel_manager.channels == {}

        channels.append(ChannelConfig(type="EchoChannel", channel_id="added", target_actor="main"))
        await mount.restart()

        assert mount.state == "live"
        assert mount.gateway is not first
        assert mount.gateway is not None
        assert "added" in mount.gateway.channel_manager.channels
        # Held throughout: a released lock would let another process take over.
        assert acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos")) is None
    finally:
        await mount.stop()


@pytest.mark.asyncio
async def test_two_consecutive_restarts_do_not_collide(tmp_path):
    """ChannelManager.stop_all() does not clear _channels, so an implementation
    that reused the manager would raise Duplicate channel_id — on the *second*
    restart, not the first."""
    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=60)
    await mount.start()
    try:
        await mount.restart()
        await mount.restart()
        assert mount.state == "live"
    finally:
        await mount.stop()


@pytest.mark.asyncio
async def test_restart_drops_an_agent_that_left_the_config(tmp_path):
    """The other half of Task 1: without AgentRegistry.clear() the deleted agent
    stays registered and callable (BEP 17 §3.5.4)."""
    from bos.core import AgentRegistry

    agents = {"main": {"system_prompt": "hi"}, "researcher": {"system_prompt": "You research."}}

    def factory():
        ws = _workspace(tmp_path)
        ws.config.agents = {name: AgentConfig(**cfg) for name, cfg in agents.items()}
        return ws

    mount = GatewayMount(factory, lock_poll_seconds=60)
    await mount.start()
    try:
        assert AgentRegistry.has_registered("researcher")
        del agents["researcher"]
        await mount.restart()
        assert not AgentRegistry.has_registered("researcher")
        assert AgentRegistry.has_registered("main")
    finally:
        await mount.stop()


@pytest.mark.asyncio
async def test_restart_does_not_wake_serve(tmp_path):
    """A restart must leave a driver parked on the socket.

    ``serve()`` waits on the mount, not on the ``Gateway`` it started with: the
    restart replaces that object, so a wait bound to it would either never fire
    again (the new gateway's ``request_shutdown`` unheard, leaving the process
    unstoppable) or — if the restart reused the demotion signal — fire straight
    away and turn a restart into a stop.
    """
    import socket

    from bos.runner.runner import serve

    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=60)
    await mount.start()
    assert mount.state == "live"

    served = asyncio.ensure_future(serve(mount))
    for _ in range(200):
        if mount.gateway is not None and mount.gateway.actual_port != 0:
            break
        await asyncio.sleep(0.02)
    assert mount.gateway is not None
    port = mount.gateway.actual_port
    assert port != 0

    await mount.restart()
    await asyncio.sleep(0.1)  # give a woken serve() every chance to finish

    assert not served.done()
    assert mount.state == "live"
    assert mount.gateway is not None
    # The socket is the driver's and is never rebound, so the rebuilt gateway
    # must publish the port that is actually bound — not the configured 0.
    assert mount.gateway.actual_port == port
    with socket.socket() as probe, pytest.raises(OSError):
        probe.bind(("127.0.0.1", port))

    # And the *new* gateway can still end the serving — the wait survived the swap.
    mount.gateway.request_shutdown()
    await asyncio.wait_for(served, timeout=10)
    assert mount.state == "stopped"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_failed_restart_stands_down_but_keeps_the_lock(tmp_path):
    """A rebuild that raises must not leave the mount looking live, and must not
    hand the run dir to another process on the way out."""
    calls: list[int] = []

    def factory():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("bad config")
        return _workspace(tmp_path)

    mount = GatewayMount(factory, lock_poll_seconds=60)
    await mount.start()
    try:
        assert mount.state == "live"
        with pytest.raises(RuntimeError, match="bad config"):
            await mount.restart()
        assert mount.state == "standby"
        assert mount.gateway is None
        assert acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos")) is None
        assert mount.status()["state"] == "standby"
    finally:
        await mount.stop()
    assert mount.state == "stopped"
    # stop() released it: the lock outlives a failed restart, not the mount.
    released = acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos"))
    assert released is not None
    released.close()


@pytest.mark.asyncio
async def test_acquire_refuses_while_a_demotion_teardown_is_in_flight(tmp_path):
    """A demoting watchdog reports ``standby`` before it has let go of anything.

    It flips the state first so that a ``restart()`` arriving mid-demotion is
    refused rather than tearing the same runtime down alongside it — which
    leaves a window, as long as the drain, where the mount reads ``standby``
    while the lock and the runtime being torn down are both still there. The
    lock file has been replaced by then, so a fresh acquire *succeeds* on the
    new inode: a host calling ``acquire()`` in that window would build a second
    runtime over the first, and the watchdog would go on to null it, close its
    harness and release the flock this call had just taken.
    """
    mount = GatewayMount(lambda: _workspace(tmp_path, grace=1.0), lock_poll_seconds=0.05)
    await mount.start()
    assert mount.state == "live"
    first = mount.gateway

    # Another gateway replaced the lock file: ours now locks an orphaned inode,
    # and the path itself is free for anyone to take.
    GatewayRunDir(tmp_path / ".bos").lock_file.unlink()

    for _ in range(500):  # catch it *inside* the teardown, not after it
        if mount.state == "standby":
            break
        await asyncio.sleep(0.01)
    assert mount.state == "standby"
    assert mount.gateway is first  # the drain is still running
    assert acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos")) is not None  # and the path is free

    assert await mount.acquire() is False
    assert mount.gateway is first  # nothing was built over the runtime going away

    await asyncio.wait_for(mount.wait_for_demotion(), timeout=10)
    assert mount.gateway is None
    await mount.stop()
    assert mount.state == "stopped"


@pytest.mark.asyncio
async def test_escalating_through_serve_gives_up_the_remaining_grace(tmp_path, monkeypatch):
    """A second signal must reach the drain, through ``serve()``.

    ``serve()`` shields only the socket cleanup. Shielding the whole stop
    instead puts the drain inside a child task, where the cancel lands on
    ``asyncio.shield`` and is swallowed — so Ctrl-C twice costs a *full* grace
    rather than cutting the remaining one short, while still printing
    "Stopping now.". ``Gateway.stop()``'s own escalation test cancels that call
    directly and cannot see this.
    """
    import socket

    from bos.runner.runner import serve

    mount = GatewayMount(lambda: _workspace(tmp_path, grace=30), lock_poll_seconds=60)
    await mount.start()
    assert mount.state == "live"
    gateway = mount.gateway
    assert gateway is not None

    in_drain = asyncio.Event()

    async def _drain(grace):  # a turn that takes its whole grace to close
        in_drain.set()
        await asyncio.sleep(grace)

    monkeypatch.setattr(gateway.actor_manager, "drain_all", _drain)

    served = asyncio.ensure_future(serve(mount))
    for _ in range(200):
        if gateway.actual_port != 0:
            break
        await asyncio.sleep(0.02)
    port = gateway.actual_port
    assert port != 0

    gateway.request_shutdown()  # first signal: drain
    await asyncio.wait_for(in_drain.wait(), timeout=5)
    served.cancel()  # second signal: stop now
    # Far below the 30s grace: with the drain shielded, this times out.
    await asyncio.wait_for(asyncio.gather(served, return_exceptions=True), timeout=5)

    # Cutting the drain short must not cost the teardown.
    assert mount.state == "stopped"
    assert mount.gateway is None
    with socket.socket() as probe:  # the socket went with it
        probe.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_a_failing_demotion_teardown_still_releases_the_socket(tmp_path, monkeypatch):
    """A raise from the teardown must not kill the watchdog mid-demotion.

    A plugin whose ``close()`` throws is enough. ``_demoted`` is what wakes a
    driver parked in ``serve()``; withholding it leaves that driver holding the
    port forever, in front of a half-torn-down runtime whose lock is already
    gone — and the failure surfaces only as "Task exception was never
    retrieved" at GC.
    """
    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=0.05)
    await mount.start()
    assert mount.state == "live"
    gateway = mount.gateway
    assert gateway is not None

    async def _boom(**_):
        raise RuntimeError("a plugin's close() threw")

    monkeypatch.setattr(gateway, "stop", _boom)

    # Another gateway replaced the lock file and took it: ours locks an orphaned
    # inode, and the path is no longer free, so no self-promotion follows.
    rd = GatewayRunDir(tmp_path / ".bos")
    rd.lock_file.unlink()
    holder = acquire_singleton_lock(rd)
    assert holder is not None
    try:
        await asyncio.wait_for(mount.wait_for_demotion(), timeout=10)
        assert mount.state == "standby"
        assert mount.gateway is None
        assert mount._watchdog is not None and not mount._watchdog.done()  # still watching
    finally:
        await mount.stop()
        holder.close()


@pytest.mark.asyncio
async def test_a_failed_bring_up_leaves_no_lock_and_no_runtime(tmp_path, monkeypatch):
    """A promotion that raises must roll the whole thing back.

    The lock and the ``Gateway`` are both assigned before the bring-up can
    fail. A half-built runtime makes the ``_gateway is not None`` guard refuse
    every later ``acquire()``, and an orphaned flock blocks every other
    instance on this ``bos_dir`` behind a mount that serves nothing.
    """

    async def _boom(self):
        raise RuntimeError("bring-up failed")

    monkeypatch.setattr("bos.gateway.Gateway.start", _boom)

    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=60)
    with pytest.raises(RuntimeError, match="bring-up failed"):
        await mount.start()

    assert mount.state == "stopped"  # not stuck in "starting"
    assert mount.gateway is None
    assert mount._watchdog is None
    freed = acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos"))
    assert freed is not None  # the lock did not outlive the failure
    freed.close()


@pytest.mark.asyncio
async def test_a_failed_restart_is_recoverable_with_acquire(tmp_path, monkeypatch):
    """A restart that fails must not wedge the mount.

    It leaves standby while still holding the lock — correct, this instance is
    still the singleton — so nothing promotes it back on its own: the watchdog's
    standby branch waits for a lock *we* hold. An embedded host cannot reach
    stop()/start() from outside its process, so acquire() has to be the way
    back, and it must work with the lock already held (BEP 17 §3.4.3).
    """
    mount = GatewayMount(lambda: _workspace(tmp_path), lock_poll_seconds=60)
    await mount.start()
    try:
        assert mount.state == "live"

        boom = RuntimeError("bring-up failed")
        calls = {"n": 0}
        real_bring_up = mount._bring_up_runtime

        async def _failing_bring_up(workspace):
            calls["n"] += 1
            if calls["n"] == 1:
                raise boom
            return await real_bring_up(workspace)

        monkeypatch.setattr(mount, "_bring_up_runtime", _failing_bring_up)

        with pytest.raises(RuntimeError, match="bring-up failed"):
            await mount.restart()

        # Rolled back, not half-built: a leftover gateway makes the re-entry
        # guard in _acquire_and_bring_up refuse every later acquire().
        assert mount.state == "standby"
        assert mount.gateway is None
        assert mount._stack is None
        # Still the singleton — dropping the lock here would invite a second
        # gateway in while this one is still wired up.
        assert acquire_singleton_lock(GatewayRunDir(tmp_path / ".bos")) is None

        assert await mount.acquire() is True
        assert mount.state == "live"
        assert mount.gateway is not None
    finally:
        await mount.stop()
