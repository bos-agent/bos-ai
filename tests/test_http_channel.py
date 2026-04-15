from bos.channels.http import _envelope_from_dict


def test_envelope_from_dict_ignores_client_recipient():
    env = _envelope_from_dict(
        {
            "recipient": "agent@main",
            "content": "hello",
            "content_type": "message",
            "conversation_id": "conv-1",
        },
        sender="channel@http",
        target="channel@user",
    )

    assert env.sender == "channel@http"
    assert env.recipient == "channel@user"
    assert env.content == "hello"
    assert env.content_type == "message"
    assert env.conversation_id == "conv-1"
