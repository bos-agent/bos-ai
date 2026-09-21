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

from aiohttp import web

from bos.gateway.config import ResolvedGatewayConfig
from bos.gateway.http import create_gateway_app, resolve_gateway_api_key
from bos.gateway.state import GatewayRunDir, acquire_singleton_lock, lock_is_free, lock_still_owned

if TYPE_CHECKING:
    from bos.config import Workspace
    from bos.gateway import Gateway

logger = logging.getLogger(__name__)


class GatewayMount:
    """Owns at most one live ``Gateway`` for a workspace's run directory.

    ``workspace_factory`` is a callable, not a ``Workspace``: a restart must
    re-read configuration, which means building a new one (BEP 17 §3.5.1).
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
        self._app: web.Application | None = None

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

    def status(self) -> dict[str, Any]:
        """The mount's view of the runtime, served at ``/api/status``.

        The mount's own fields are applied *after* the gateway's snapshot: the
        snapshot hard-codes ``runtime: "process"`` (it cannot know where it is
        hosted), and overwriting the mount's label with it would make
        ``runtime_label`` meaningless for an embedded host.
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
        if not await self._acquire_and_bring_up(workspace):
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
        self._lock = acquire_singleton_lock(self._run_dir)
        if self._lock is None:
            return False
        from bos.gateway import Gateway

        workspace.resolve_agents()
        workspace.bootstrap_platform()
        self._stack = contextlib.AsyncExitStack()
        harness = await self._stack.enter_async_context(workspace.harness())
        self._gateway = Gateway(runtime=workspace.resolve_gateway_runtime(), harness=harness)
        await self._gateway.start()
        if self._public_base_url is not None:
            self._gateway.set_public_base_url(self._public_base_url)
        self._demoted.clear()
        self._state = "live"
        return True

    async def _tear_down_runtime(self, *, graceful: bool = True) -> None:
        """Stop the gateway and close the harness. Leaves the lock alone —
        a restart keeps it (BEP 17 §3.5.1)."""
        if self._gateway is not None:
            await self._gateway.stop(graceful=graceful)
            self._gateway = None
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

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
            if self._state == "live":
                if not lock_still_owned(self._run_dir, self._lock):
                    logger.error("Lost singleton lock ownership for %s — standing down.", self._run_dir.bos_dir)
                    await self._tear_down_runtime()
                    self._lock = None
                    self._state = "standby"
                    # After the teardown, never before: _tear_down_runtime has
                    # already cleared _gateway and _stack, so the stop() a woken
                    # driver runs finds nothing left to tear down a second time.
                    self._demoted.set()
            elif self._state == "standby":
                if lock_is_free(self._run_dir) and await self._acquire_and_bring_up(self._workspace_factory()):
                    logger.info("Acquired the singleton lock for %s — going live.", self._run_dir.bos_dir)

    def _current_config(self) -> ResolvedGatewayConfig:
        """Upload settings for the app, from whichever Gateway is current.

        In standby there is none, and the defaults are the right answer: the
        only route served then is /api/status, which reads no config.
        """
        return self._gateway.config if self._gateway is not None else ResolvedGatewayConfig()

    async def _dispatch_ws(self, request: web.Request) -> web.StreamResponse:
        if self._gateway is None:
            return web.json_response({"ok": False, "error": self._state}, status=503)
        return await self._gateway.handle_ws(request)

    def build_app(self) -> web.Application:
        """The application, built once and indirecting through the mount.

        The host mounts this before ``start()`` and keeps it across restarts, so
        it cannot hold a ``Gateway`` — that object is replaced wholesale
        (BEP 17 §3.3.3). Layer 2 swaps the body for a Starlette app; the
        indirection is what makes that swap local.
        """
        if self._app is None:
            self._app = create_gateway_app(
                config_provider=self._current_config,
                api_key=resolve_gateway_api_key(self._current_config()),
                status_provider=self.status,
                ws_handler=self._dispatch_ws,
            )
        return self._app
