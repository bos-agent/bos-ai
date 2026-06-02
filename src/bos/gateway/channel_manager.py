from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any

from bos.config.workspace import ResolvedGatewayChannelConfig
from bos.core import Channel, MailBox, ep_channel

from .channel_context import ChannelRuntimeContext

logger = logging.getLogger(__name__)


class ChannelFactoryError(ValueError):
    """Raised when a configured channel cannot be instantiated safely."""


@dataclass(frozen=True)
class ChannelStatus:
    channel_id: str
    type: str
    kind: str
    display_name: str | None
    address: str
    target_actor: str
    status: str
    identity_key: str | None = None
    channel_conversation_count: int = 0
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "kind": self.kind,
            "display_name": self.display_name,
            "status": self.status,
            "address": self.address,
            "target_actor": self.target_actor,
            "channel_conversation_count": self.channel_conversation_count,
        }
        if self.identity_key is not None:
            payload["identity_key"] = self.identity_key
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass
class ManagedChannel:
    channel: Channel
    type_name: str
    kind: str
    address: str
    mailbox: MailBox | None = None
    task: asyncio.Task[None] | None = None
    status: str = "configured"
    error: str | None = None

    def snapshot(self) -> ChannelStatus:
        return ChannelStatus(
            channel_id=self.channel.channel_id,
            type=self.type_name,
            kind=self.kind,
            display_name=self.channel.display_name,
            address=self.address,
            target_actor=self.channel.target_actor,
            status=self.status,
            identity_key=_safe_identity_key(self.channel.identity_key),
            error=self.error,
        )


class ChannelManager:
    """Gateway-owned channel factory and lifecycle manager.

    The manager owns channel tasks and mailbox binding. It does not proxy normal
    chat traffic; each channel consumes its own mailbox and sends envelopes
    directly through the shared ``MailRoute`` supplied in ``ChannelRuntimeContext``.
    """

    def __init__(self, *, runtime: ChannelRuntimeContext) -> None:
        self.runtime = runtime
        self._channels: dict[str, ManagedChannel] = {}
        self._identity_index: dict[str, str] = {}

    @property
    def channels(self) -> dict[str, ManagedChannel]:
        return dict(self._channels)

    def create_persistent(self, configs: list[ResolvedGatewayChannelConfig]) -> list[ManagedChannel]:
        managed: list[ManagedChannel] = []
        for cfg in configs:
            channel = self._instantiate_channel(cfg)
            managed.append(self.register(channel, type_name=cfg.type, kind="persistent", address=cfg.address))
        return managed

    def register(
        self,
        channel: Channel,
        *,
        type_name: str | None = None,
        kind: str,
        address: str | None = None,
        takeover: bool = False,
    ) -> ManagedChannel:
        existing = self._channels.get(channel.channel_id)
        if existing is not None:
            if not takeover:
                raise ChannelFactoryError(f"Duplicate channel_id: {channel.channel_id!r}")
            self._drop_registration(existing)

        identity_key = channel.identity_key
        if identity_key:
            owner = self._identity_index.get(identity_key)
            if owner is not None and owner != channel.channel_id:
                raise ChannelFactoryError(
                    f"Duplicate channel identity_key {identity_key!r} for {channel.channel_id!r}."
                )

        managed = ManagedChannel(
            channel=channel,
            type_name=type_name or type(channel).__name__,
            kind=kind,
            address=address or f"channel@{channel.channel_id}",
        )
        self._channels[channel.channel_id] = managed
        if identity_key:
            self._identity_index[identity_key] = channel.channel_id
        return managed

    async def start_all(self) -> None:
        for managed in self._channels.values():
            await self.start_channel(managed)
        await self._notify_state_changed()

    async def start_channel(self, managed: ManagedChannel) -> None:
        if managed.task is not None:
            return
        managed.mailbox = self.runtime.mail_route.bind(managed.address)
        managed.status = "running"
        managed.task = asyncio.create_task(
            self._run_channel(managed),
            name=f"bos-channel:{managed.channel.channel_id}",
        )

    async def stop_all(self) -> None:
        tasks = [managed.task for managed in self._channels.values() if managed.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for managed in self._channels.values():
            managed.task = None
            managed.status = "stopped"
        await self._notify_state_changed()

    def status_payload(self) -> dict[str, dict[str, Any]]:
        return {channel_id: managed.snapshot().to_payload() for channel_id, managed in self._channels.items()}

    async def _run_channel(self, managed: ManagedChannel) -> None:
        assert managed.mailbox is not None
        try:
            await managed.channel.run(managed.mailbox)
        except asyncio.CancelledError:
            managed.status = "stopped"
            raise
        except Exception as exc:  # pragma: no cover - exercised by future integration tests
            managed.status = "error"
            managed.error = str(exc)
            logger.exception("Channel %s failed", managed.channel.channel_id)
            await self._notify_state_changed()
            raise

    def _instantiate_channel(self, cfg: ResolvedGatewayChannelConfig) -> Channel:
        ext = ep_channel.get(cfg.type)
        if ext is None:
            raise ChannelFactoryError(f"Unknown channel type {cfg.type!r}.")

        settings = _validate_settings(getattr(ext.fn, "SettingsType", None), cfg.settings)
        channel = ep_channel.invoke(
            cfg.type,
            {
                "channel_id": cfg.channel_id,
                "target_actor": cfg.target_actor,
                "display_name": cfg.display_name,
                "settings": settings,
                "runtime": self.runtime,
            },
        )
        if not isinstance(channel, Channel):
            raise ChannelFactoryError(f"Channel type {cfg.type!r} does not implement the Channel protocol.")
        if channel.channel_id != cfg.channel_id:
            raise ChannelFactoryError(
                f"Channel type {cfg.type!r} returned channel_id {channel.channel_id!r}, expected {cfg.channel_id!r}."
            )
        return channel

    def _drop_registration(self, managed: ManagedChannel) -> None:
        if managed.task is not None:
            managed.task.cancel()
        self._channels.pop(managed.channel.channel_id, None)
        identity_key = managed.channel.identity_key
        if identity_key and self._identity_index.get(identity_key) == managed.channel.channel_id:
            self._identity_index.pop(identity_key, None)

    async def _notify_state_changed(self) -> None:
        if self.runtime.state_changed is not None:
            await self.runtime.state_changed()


def _validate_settings(settings_type: type[Any] | None, raw_settings: dict[str, Any]) -> Any:
    if settings_type is None or settings_type is dict:
        return dict(raw_settings)
    if hasattr(settings_type, "model_validate"):
        return settings_type.model_validate(raw_settings)
    if inspect.isclass(settings_type):
        if isinstance(raw_settings, settings_type):
            return raw_settings
        try:
            return settings_type(**raw_settings)
        except TypeError as exc:
            raise ChannelFactoryError(f"Invalid channel settings for {settings_type.__name__}: {exc}") from exc
    return raw_settings


def _safe_identity_key(identity_key: str | None) -> str | None:
    if identity_key is None:
        return None
    lowered = identity_key.lower()
    if "token" in lowered or "secret" in lowered or "key=" in lowered:
        return None
    return identity_key
