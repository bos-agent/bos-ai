from bos.channels.broadcast import BroadcastChannel
from bos.core import Envelope


def test_broadcast_rewrites_inbound_to_canonical_conversation_id():
    channel = BroadcastChannel(["channel@http", "channel@telegram"], "agent@main")
    canonical = channel._conversation_id

    channel._remember_member_conversation("channel@http", "http-local-1")
    channel._remember_member_conversation("channel@telegram", "telegram:42")

    assert channel._conversation_for_member("channel@http") == "http-local-1"
    assert channel._conversation_for_member("channel@telegram") == "telegram:42"
    assert canonical != channel._conversation_for_member("channel@telegram")


def test_rotate_conversation_updates_http_but_keeps_telegram_sticky_id():
    channel = BroadcastChannel(["channel@http", "channel@telegram"], "agent@main")
    old_canonical = channel._conversation_id
    channel._remember_member_conversation("channel@http", "http-local-1")
    channel._remember_member_conversation("channel@telegram", "telegram:42")

    channel._rotate_conversation("channel@http", requested_local_id="http-local-2")

    assert channel._conversation_id != old_canonical
    assert channel._conversation_for_member("channel@http") == "http-local-2"
    assert channel._conversation_for_member("channel@telegram") == "telegram:42"


def test_new_conversation_request_detects_channel_and_slash_commands():
    channel = BroadcastChannel(["channel@http"], "agent@main")

    assert channel._is_new_conversation_request(
        Envelope(
            sender="channel@http",
            recipient="channel@user",
            content="new_conversation",
            content_type="channel_command",
        )
    )
    assert channel._is_new_conversation_request(
        Envelope(sender="channel@telegram", recipient="channel@user", content="/new", content_type="command")
    )
    assert not channel._is_new_conversation_request(
        Envelope(sender="channel@telegram", recipient="channel@user", content="/history", content_type="command")
    )
