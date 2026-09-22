"""GatewayMount — the composition root for a gateway the caller drives (BEP 17 §3.3).

``python -m bos.runner`` and an embedded ASGI host use the *same* object, so the
standalone path is not a second implementation. The mount owns the singleton
lock, the live/standby state machine and the lock watchdog; the ``Gateway``
behind it is rebuilt wholesale on every start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.websockets import WebSocket

from bos.gateway.config import ResolvedGatewayConfig
from bos.gateway.http import create_gateway_app, send_ws_denial
from bos.gateway.state import GatewayRunDir, acquire_singleton_lock, lock_is_free, lock_still_owned

if TYPE_CHECKING:
    from bos.config import Workspace
    from bos.gateway import Gateway

logger = logging.getLogger(__name__)


class GatewayMount:
    """Owns at most one live ``Gateway`` for a workspace's run directory.

    ``workspace_factory`` is a callable, not a ``Workspace``: a restart must
    re-read configuration, which means building a new one (BEP 17 §3.5.1).

    ``state`` is one of ``starting``, ``live``, ``standby``, ``restarting`` or
    ``stopped`` (BEP 17 §3.4.3). ``/api/status`` is served in all of them —
    a status route that goes dark in the failure case is useless — and ``/ws``
    in ``live`` alone.
    """

    def __init__(
        self,
        workspace_factory: Callable[[], Workspace],
        *,
        runtime_label: str = "embedded",
        public_base_url: str | None = None,
        lock_poll_seconds: float = 5.0,
    ) -> None:
        self._workspace_factory = workspace_factory
        self._runtime_label = runtime_label
        self._public_base_url = public_base_url
        self._lock_poll_seconds = lock_poll_seconds
        self._state = "stopped"
        self._gateway: Gateway | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._run_dir: GatewayRunDir | None = None
        self._lock: Any = None
        self._watchdog: asyncio.Task[None] | None = None
        self._demoted = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._shutdown_bridge: asyncio.Task[None] | None = None
        self._app: Starlette | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def gateway(self) -> Gateway | None:
        return self._gateway

    async def wait_for_demotion(self) -> None:
        """Block until the watchdog tears a live runtime down (live → standby).

        The mount never stops itself over a lost lock — an embedded host is
        expected to stay up in standby and be promoted again later. A driver
        that owns a socket cannot: the ``Gateway`` it captured is gone and will
        never signal its own shutdown, so it must learn about the demotion here
        and release the port. Only ever set *after* the teardown has finished,
        so a waiter cannot race the watchdog into ``_tear_down_runtime``.
        """
        await self._demoted.wait()

    async def wait_for_shutdown(self) -> None:
        """Block until the *current* gateway is asked to shut down.

        The mount, not the ``Gateway``, is what a driver waits on, because
        ``restart()`` replaces that object: a wait bound to the instance a
        driver started with would never hear the new one's
        ``request_shutdown()`` — a signal handler would stop reaching the
        process. ``_forward_shutdown`` re-arms this on whichever gateway is
        current, so the swap is invisible to the waiter.
        """
        await self._shutdown.wait()

    async def _forward_shutdown(self, gateway: Gateway) -> None:
        """Relay one gateway's shutdown request onto the mount's own signal."""
        await gateway.wait_for_shutdown()
        self._shutdown.set()

    def status(self) -> dict[str, Any]:
        """The mount's view of the runtime, served at ``/api/status``.

        The mount contributes ``state`` and ``holder_pid`` — the two things
        only the mount (not the gateway) knows. ``runtime`` is set here too,
        even though the gateway's own snapshot (applied first, below) already
        carries the identical value the mount injected into it at construction:
        ``/api/status`` must answer in *every* state (BEP 17 §3.4.3), including
        ``standby``, where ``self._gateway`` is ``None`` and there is no
        snapshot to source it from. Without this, the one state where an
        operator most needs to tell "standalone process" from "embedded host"
        apart (BEP 17 §3.4.4) would go dark instead. Redundant when a gateway
        exists, load-bearing when one does not — do not drop it.
        """
        payload: dict[str, Any] = {}
        if self._gateway is not None:
            payload.update(self._gateway.status_snapshot())
        payload.update(
            state=self._state,
            runtime=self._runtime_label,
            holder_pid=None if self._state == "live" else self._holder_pid(),
        )
        return payload

    def _holder_pid(self) -> int | None:
        if self._run_dir is None:
            return None
        try:
            return int(self._run_dir.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    async def start(self) -> None:
        if self._state in {"live", "standby"}:
            return
        self._state = "starting"
        workspace = self._workspace_factory()
        self._run_dir = GatewayRunDir(workspace.bos_dir)
        self._run_dir.ensure()
        try:
            live = await self._acquire_and_bring_up(workspace)
        except BaseException:
            # _acquire_and_bring_up has already rolled its own partial bring-up
            # back, so nothing is held and nothing is half-built. Say so rather
            # than leaving the mount parked in "starting" for good, with a
            # gateway the caller can see but nothing driving it.
            self._state = "stopped"
            raise
        if not live:
            self._state = "standby"
            logger.warning("Another gateway holds the lock for %s — standing by.", workspace.bos_dir)
        self._watchdog = asyncio.ensure_future(self._watch_lock())

    async def acquire(self) -> bool:
        """Try the lock now. Returns True if the mount is live afterwards."""
        if self._state == "live":
            return True
        if self._state != "standby":
            return False
        return await self._acquire_and_bring_up(self._workspace_factory())

    async def _acquire_and_bring_up(self, workspace: Workspace) -> bool:
        """Take the lock and start the runtime. False (and no side effects
        beyond the probe) when another holder owns the lock.

        Nothing global is mutated before the lock is held: ``bootstrap_platform``
        imports extension modules and writes ``os.environ``, which a standby
        instance must not do.
        """
        assert self._run_dir is not None
        if self._gateway is not None:
            # A runtime already exists, so whatever state the mount reports, one
            # is being torn down somewhere else — the demoting watchdog goes to
            # "standby" before its teardown, which takes the whole drain to run.
            # Promoting again would build a second runtime over it, and that
            # teardown would then null the new gateway, close the new harness
            # and release the lock this call had just taken.
            return False
        # A lock already held is this mount's own: a failed restart leaves it in
        # standby still holding it. Re-acquiring would fail — flock binds to an
        # open file description, so a second open() in this process conflicts
        # with our own handle — and that failure is what made a failed restart
        # unrecoverable for an embedded host, which cannot reach stop()/start()
        # from outside its process.
        took_lock = False
        lock = self._lock
        if lock is None:
            lock = acquire_singleton_lock(self._run_dir)
            if lock is None:
                return False
            self._lock = lock
            took_lock = True
        try:
            await self._bring_up_runtime(workspace)
        except BaseException:
            # Roll the whole promotion back. A half-built runtime left behind
            # would make the `self._gateway is not None` guard above refuse
            # every later acquire(), and an orphaned lock would block every
            # other instance on this bos_dir behind a mount that serves
            # nothing. Teardown errors are secondary to the failure that got
            # us here, so they do not replace it.
            with contextlib.suppress(Exception):
                await self._tear_down_runtime(graceful=False)
            self._gateway = None
            self._stack = None
            if took_lock:
                # Only what this call acquired. A retry after a failed restart
                # must keep the lock it already had: releasing it there would
                # drop the singleton as a side effect of a failed rebuild.
                lock.close()
                self._lock = None
            raise
        self._state = "live"
        return True

    async def _bring_up_runtime(self, workspace: Workspace) -> Gateway:
        """Build and start a runtime from *workspace*, under a lock we hold.

        The one place that knows how a ``Gateway`` is assembled — the first
        bring-up and every restart build it the same way, wholesale
        (BEP 17 §3.5.1).
        """
        from bos.gateway import Gateway

        workspace.resolve_agents()
        workspace.bootstrap_platform()
        self._stack = contextlib.AsyncExitStack()
        harness = await self._stack.enter_async_context(workspace.harness())
        gateway = Gateway(
            runtime=workspace.resolve_gateway_runtime(), harness=harness, runtime_label=self._runtime_label
        )
        self._gateway = gateway
        await gateway.start()
        if self._public_base_url is not None:
            gateway.set_public_base_url(self._public_base_url)
        self._demoted.clear()
        self._shutdown.clear()
        self._shutdown_bridge = asyncio.ensure_future(self._forward_shutdown(gateway))
        return gateway

    async def _tear_down_runtime(self, *, graceful: bool = True) -> None:
        """Stop the gateway and close the harness. Leaves the lock alone —
        a restart keeps it (BEP 17 §3.5.1)."""
        if self._shutdown_bridge is not None:
            # Before the gateway goes: the bridge must not outlive the instance
            # it relays for, or a restart would leave a task parked on a dead
            # gateway able to signal a shutdown the live one never asked for.
            self._shutdown_bridge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._shutdown_bridge
            self._shutdown_bridge = None
        if self._gateway is not None:
            await self._gateway.stop(graceful=graceful)
            self._gateway = None
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def restart(self) -> None:
        """Tear the runtime down and rebuild it from freshly read configuration.

        The whole ``Gateway`` is replaced, not restarted: ``create_persistent``
        instantiates from the list ``Gateway.__init__`` captured, and
        ``ChannelManager.stop_all()`` does not clear its registry — reusing the
        manager would raise ``Duplicate channel_id`` on the second restart
        (BEP 17 §3.5.1).

        Configuration and agent definitions reload; Python code does not
        (BEP 17 §3.5.2). The lock is held throughout, and so is the listening
        socket: the host owns it, nothing is rebound, and a driver parked in
        ``serve()`` stays parked — it waits on the mount, never on the gateway
        this replaces, and the demotion signal that *does* end a serving is not
        touched here.
        """
        # ponytail: no mutex — the mount is not re-entrant, and restart() has no
        # caller yet (Layer 3's POST /api/restart is the first). The state
        # machine is what keeps it clear of the watchdog: "restarting" matches
        # neither of its branches, so a mid-restart mount is neither demoted nor
        # promoted under us, and a demotion that got there first has already
        # moved the state off "live", which the guard below refuses. Give it a
        # concurrent caller — a host that can restart while stopping — and this
        # wants an asyncio.Lock around start/stop/restart/acquire: a
        # request_shutdown() aimed at the gateway being torn down lands on a
        # relay that is already cancelled, so the stop is lost until the caller
        # escalates.
        if self._state != "live":
            raise RuntimeError(f"Cannot restart a gateway that is {self._state!r}; it holds no lock.")
        assert self._gateway is not None
        # Carried across the swap: the socket is the host's and is not rebound,
        # so what it bound is still the truth. A fresh Gateway reports the
        # *configured* endpoint, which with ``port = 0`` is not a port anything
        # can connect to — and it publishes that to gateway.state on start().
        endpoint = (self._gateway.actual_host, self._gateway.actual_port)
        self._state = "restarting"
        try:
            await self._tear_down_runtime()
            gateway = await self._bring_up_runtime(self._workspace_factory())
            gateway.set_endpoint(*endpoint)
        except BaseException:
            # Roll the rebuild back to a clean standby. A half-built runtime
            # left behind makes _acquire_and_bring_up's re-entry guard refuse
            # every later acquire(), which is the only way back for an embedded
            # host. Teardown errors are secondary to the failure that got us
            # here, so they do not replace it.
            with contextlib.suppress(Exception):
                await self._tear_down_runtime(graceful=False)
            self._gateway = None
            self._stack = None
            # The lock is kept: this instance is still the singleton, and
            # dropping it would invite another process in while this one is
            # still wired up. acquire() rebuilds from here.
            self._state = "standby"
            raise
        self._state = "live"

    async def stop(self, *, graceful: bool = True) -> None:
        if self._state == "stopped":
            return
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        await self._tear_down_runtime(graceful=graceful)
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        self._state = "stopped"

    async def _watch_lock(self) -> None:
        """Both directions of singleton authority.

        Losing the lock means another gateway took over — stand down rather than
        become a second poller that duplicates delivery. Seeing it free while in
        standby means the holder is gone — take over. One task, one interval.
        """
        while True:
            await asyncio.sleep(self._lock_poll_seconds)
            assert self._run_dir is not None
            # One failing poll must not end the watchdog: it is the only thing
            # that can demote this mount, and a dead one leaves a lost lock
            # unnoticed for the life of the process. Cancellation is a
            # BaseException and still ends the loop, which is what stop() wants.
            try:
                if self._state == "live":
                    if not lock_still_owned(self._run_dir, self._lock):
                        logger.error("Lost singleton lock ownership for %s — standing down.", self._run_dir.bos_dir)
                        # Off "live" before the teardown, which takes the drain's
                        # grace to run: a restart() arriving during it must be
                        # refused outright rather than tear the same runtime down
                        # a second time alongside us.
                        self._state = "standby"
                        try:
                            await self._tear_down_runtime()
                        finally:
                            # Even when the teardown raised — a plugin whose
                            # close() throws is enough. This signal is what
                            # releases the socket: withholding it parks the
                            # driver in serve() forever, holding the port in
                            # front of a runtime that has already lost its lock.
                            # After the teardown, never before: _tear_down_runtime
                            # has cleared _gateway and _stack, so the stop() a
                            # woken driver runs finds nothing to tear down twice.
                            self._gateway = None
                            self._stack = None
                            self._lock = None
                            self._demoted.set()
                elif self._state == "standby":
                    if lock_is_free(self._run_dir) and await self._acquire_and_bring_up(self._workspace_factory()):
                        logger.info("Acquired the singleton lock for %s — going live.", self._run_dir.bos_dir)
            except Exception:
                logger.exception("Singleton lock watchdog iteration failed for %s.", self._run_dir.bos_dir)

    def _current_config(self) -> ResolvedGatewayConfig:
        """Upload settings for the app, from whichever Gateway is current.

        In standby there is none, and the defaults are the right answer: the
        only route served then is /api/status, which reads no config.
        """
        return self._gateway.config if self._gateway is not None else ResolvedGatewayConfig()

    async def _dispatch_ws(self, websocket: WebSocket) -> None:
        # Live only (BEP 17 §3.4.3). The state, not just the presence of a
        # gateway: a restart installs the new instance before its actors and
        # channels are up, and a websocket accepted in that window would get a
        # consumer that does not exist yet.
        if self._state != "live" or self._gateway is None:
            await send_ws_denial(websocket, 503, {"ok": False, "error": self._state})
            return
        await self._gateway.handle_ws(websocket)

    def build_app(self) -> Starlette:
        """The application, built once and indirecting through the mount.

        The host mounts this before ``start()`` and keeps it across restarts, so
        it cannot hold a ``Gateway`` — that object is replaced wholesale
        (BEP 17 §3.3.3).
        """
        if self._app is None:
            self._app = create_gateway_app(
                config_provider=self._current_config,
                status_provider=self.status,
                ws_handler=self._dispatch_ws,
            )
        return self._app
