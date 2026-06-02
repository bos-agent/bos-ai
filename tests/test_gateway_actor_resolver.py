import pytest

from bos.gateway import ActorDescriptor, ActorResolutionError, ActorResolver


def _resolver() -> ActorResolver:
    return ActorResolver(
        {
            "main": ActorDescriptor("main", "agent@main", display_name="Primary", is_default=True),
            "coder": ActorDescriptor("coder", "agent@coder", display_name="Coder"),
        },
        default_actor="main",
    )


def test_actor_resolver_uses_default_actor_without_mention():
    result = _resolver().resolve("summarize this")

    assert result.target_actor == "main"
    assert result.target_address == "agent@main"
    assert result.content == "summarize this"
    assert result.metadata["target_actor"] == "main"
    assert result.metadata["target_address"] == "agent@main"


def test_actor_resolver_strips_known_mention():
    result = _resolver().resolve("@coder fix this")

    assert result.target_actor == "coder"
    assert result.target_address == "agent@coder"
    assert result.content == "fix this"
    assert result.metadata["target_display"] == "Coder"


def test_actor_resolver_rejects_unknown_mention_with_renderable_event():
    with pytest.raises(ActorResolutionError) as exc:
        _resolver().resolve("@missing do work")

    assert exc.value.to_event() == {
        "event": "actor_resolution_error",
        "target_actor": "missing",
        "message": "Unknown actor @missing",
    }


def test_display_name_is_not_routing_key():
    with pytest.raises(ActorResolutionError):
        _resolver().resolve("@Primary do work")
