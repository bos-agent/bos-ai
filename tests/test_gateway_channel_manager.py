import asyncio
from dataclasses import dataclass

import pytest

from bos.config.workspace import ResolvedGatewayChannelConfig
from bos.core import BaseChannel, MailBox, ep_channel
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.gateway import ActorResolver, ChannelFactoryError, ChannelManager, ChannelRuntimeContext
from bos.gateway.core.chat_coordinator import ChatCoordinator


@dataclass(frozen=True)
class DemoSettings:
    bot_id: str
    token_env: str = "TOKEN"


@ep_channel(name="TestManagedChannel")
class DemoManagedChannel(BaseChannel[DemoSettings]):
    SettingsType = DemoSettings

    @property
    def identity_key(self) -> str | None:
        return f"demo:bot:{self._settings.bot_id}"

    async def run(self, mailbox: MailBox) -> None:
        await asyncio.Event().wait()


@ep_channel(name="TestBadChannel")
class TestBadChannel:
    pass


class FakeMailRoute:
    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind(self, address: str):
        self.bound.append(address)
        return object()

    async def deliver(self, env):
        raise AssertionError("not used")


def _runtime(state_changed=None):
    return ChannelRuntimeContext(
        actor_resolver=ActorResolver({"main": "agent@main"}, default_actor="main"),
        chat_coordinator=ChatCoordinator(InMemChatStore()),
        mail_route=FakeMailRoute(),
        state_changed=state_changed,
    )


def _cfg(channel_id: str, *, bot_id: str = "123") -> ResolvedGatewayChannelConfig:
    return ResolvedGatewayChannelConfig(
        type="TestManagedChannel",
        channel_id=channel_id,
        address=f"channel@{channel_id}",
        target_actor="main",
        display_name="Demo",
        settings={"bot_id": bot_id},
    )


@pytest.mark.asyncio
async def test_channel_manager_factory_passes_runtime_and_validated_settings():
    runtime = _runtime()
    manager = ChannelManager(runtime=runtime)

    [managed] = await manager.create_persistent([_cfg("demo")])

    assert managed.channel.channel_id == "demo"
    assert managed.channel.target_actor == "main"
    assert managed.channel.display_name == "Demo"
    assert managed.channel.identity_key == "demo:bot:123"
    assert managed.channel._runtime is runtime
    assert isinstance(managed.channel._settings, DemoSettings)
    assert managed.address == "channel@demo"
    assert manager.status_payload()["demo"]["identity_key"] == "demo:bot:123"


@pytest.mark.asyncio
async def test_channel_manager_rejects_duplicate_identity_key():
    manager = ChannelManager(runtime=_runtime())

    with pytest.raises(ChannelFactoryError, match="identity_key"):
        await manager.create_persistent([_cfg("demo-a", bot_id="same"), _cfg("demo-b", bot_id="same")])


def test_channel_manager_rejects_duplicate_dynamic_channel_without_takeover():
    manager = ChannelManager(runtime=_runtime())
    first = DemoManagedChannel(channel_id="ws:one", target_actor="main", display_name=None, settings=DemoSettings("a"))
    second = DemoManagedChannel(channel_id="ws:one", target_actor="main", display_name=None, settings=DemoSettings("a"))

    manager.register(first, kind="dynamic")

    with pytest.raises(ChannelFactoryError, match="Duplicate channel_id"):
        manager.register(second, kind="dynamic")


def test_channel_manager_allows_dynamic_takeover_same_channel_id():
    manager = ChannelManager(runtime=_runtime())
    first = DemoManagedChannel(channel_id="ws:one", target_actor="main", display_name=None, settings=DemoSettings("a"))
    second = DemoManagedChannel(channel_id="ws:one", target_actor="main", display_name=None, settings=DemoSettings("a"))

    manager.register(first, kind="dynamic")
    replacement = manager.register(second, kind="dynamic", takeover=True)

    assert replacement.channel is second
    assert manager.channels["ws:one"].channel is second


@pytest.mark.asyncio
async def test_channel_factory_rejects_unknown_or_non_protocol_channel():
    manager = ChannelManager(runtime=_runtime())
    unknown = ResolvedGatewayChannelConfig(
        type="MissingChannel",
        channel_id="missing",
        address="channel@missing",
        target_actor="main",
        settings={},
    )
    bad = ResolvedGatewayChannelConfig(
        type="TestBadChannel",
        channel_id="bad",
        address="channel@bad",
        target_actor="main",
        settings={},
    )

    with pytest.raises(ChannelFactoryError, match="Unknown channel"):
        await manager.create_persistent([unknown])
    with pytest.raises(ChannelFactoryError, match="Channel protocol"):
        await manager.create_persistent([bad])


@pytest.mark.asyncio
async def test_channel_manager_binds_mailboxes_and_notifies_state_change():
    notifications = 0

    async def state_changed() -> None:
        nonlocal notifications
        notifications += 1

    runtime = _runtime(state_changed=state_changed)
    manager = ChannelManager(runtime=runtime)
    await manager.create_persistent([_cfg("demo")])

    await manager.start_all()
    await manager.stop_all()

    assert runtime.mail_route.bound == ["channel@demo"]
    assert notifications == 2
