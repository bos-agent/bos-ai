from bos.core import BaseChannel, ep_channel


def test_base_channel_stores_gateway_constructor_fields():
    runtime = object()
    settings = {"token_env": "TOKEN"}
    channel = BaseChannel(
        channel_id="telegram:daily",
        target_actor="main",
        display_name="Daily",
        settings=settings,
        runtime=runtime,
    )

    assert channel.channel_id == "telegram:daily"
    assert channel.target_actor == "main"
    assert channel.display_name == "Daily"
    assert channel.identity_key is None
    assert channel._settings is settings
    assert channel._runtime is runtime


def test_http_channel_is_not_registered_as_runtime_channel():
    assert not ep_channel.has("HttpChannel")
