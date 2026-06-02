"""Tests for BEP6 [runtime] section access via Workspace."""

import pytest

from bos.config.workspace import Workspace


def test_runtime_agent_defaults():
    """runtime.agent defaults to _default."""
    ws = Workspace(".", ".bos", {})
    assert ws.get_main_agent_kind() == "_default"


def test_gateway_runtime_resolves_final_config_shape():
    config = {
        "runtime": {
            "location": "process",
            "default_actor": "main",
            "gateway": {"host": "0.0.0.0", "port": 7000, "api_key_env": "BOS_TEST_KEY"},
            "actors": {
                "main": {
                    "agent": "assistant",
                    "display_name": "Main",
                    "restart_on_error": True,
                    "max_restarts": 3,
                    "tools": ["ReadFile"],
                },
                "coder": {"agent": "researcher", "display_name": "Coder"},
            },
            "channels": [
                {
                    "type": "TelegramChannel",
                    "channel_id": "telegram:daily",
                    "display_name": "Daily",
                    "target_actor": "coder",
                    "settings": {"bot_id": "123", "token_env": "TELEGRAM_TOKEN"},
                }
            ],
        }
    }
    ws = Workspace(".", ".bos", config)

    gateway = ws.resolve_gateway_config()
    actors = ws.resolve_gateway_actors()
    channels = ws.resolve_gateway_channels()

    assert gateway.host == "0.0.0.0"
    assert gateway.port == 7000
    assert gateway.api_key_env == "BOS_TEST_KEY"
    assert ws.resolve_default_actor() == "main"
    assert actors["main"].address == "agent@main"
    assert actors["main"].agent_overrides == {"tools": ["ReadFile"]}
    assert channels[0].type == "TelegramChannel"
    assert channels[0].channel_id == "telegram:daily"
    assert channels[0].address == "channel@telegram:daily"
    assert channels[0].target_actor == "coder"
    assert channels[0].settings["bot_id"] == "123"


def test_gateway_runtime_requires_actors():
    ws = Workspace(".", ".bos", {"runtime": {"default_actor": "main"}})

    with pytest.raises(ValueError, match="runtime.actors"):
        ws.resolve_gateway_actors()


def test_gateway_runtime_rejects_invalid_default_actor():
    ws = Workspace(".", ".bos", {"runtime": {"default_actor": "missing", "actors": {"main": {"agent": "main"}}}})

    with pytest.raises(ValueError, match="runtime.default_actor"):
        ws.resolve_default_actor()


def test_gateway_runtime_rejects_duplicate_channel_ids():
    config = {
        "runtime": {
            "default_actor": "main",
            "actors": {"main": {"agent": "main"}},
            "channels": [
                {"type": "TelegramChannel", "channel_id": "dup"},
                {"type": "SlackChannel", "channel_id": "dup"},
            ],
        }
    }
    ws = Workspace(".", ".bos", config)

    with pytest.raises(ValueError, match="Duplicate channel_id"):
        ws.resolve_gateway_channels()


def test_gateway_runtime_rejects_http_channel_config():
    config = {
        "runtime": {
            "default_actor": "main",
            "actors": {"main": {"agent": "main"}},
            "channels": [{"type": "HttpChannel", "channel_id": "http"}],
        }
    }
    ws = Workspace(".", ".bos", config)

    with pytest.raises(ValueError, match="HttpChannel"):
        ws.resolve_gateway_channels()


def test_runtime_agent_explicit():
    """runtime.agent can be explicitly set."""
    config = {"runtime": {"agent": "main", "location": "process"}}
    ws = Workspace(".", ".bos", config)
    assert ws.get_main_agent_kind() == "main"


def test_main_agent_address_is_stable():
    """get_main_agent_address always returns agent@main regardless of agent kind."""
    config = {"runtime": {"agent": "researcher", "location": "process"}}
    ws = Workspace(".", ".bos", config)
    assert ws.get_main_agent_address() == "agent@main"


def test_resolve_channels_uses_explicit_config():
    """resolve_channels reads from runtime.channels."""
    config = {
        "runtime": {
            "agent": "main",
            "location": "process",
            "channels": [
                {
                    "name": "HttpChannel",
                    "bind_address": "channel@http",
                    "target_address": "agent@main",
                    "host": "0.0.0.0",
                    "port": 8080,
                }
            ],
        }
    }
    ws = Workspace(".", ".bos", config)
    channels = ws.resolve_channels(runtime_kind="process")
    assert len(channels) == 1
    assert channels[0].name == "HttpChannel"
    assert channels[0].bind_address == "channel@http"
    assert channels[0].options["host"] == "0.0.0.0"
    assert channels[0].options["port"] == 8080


def test_resolve_channels_defaults_when_empty():
    """When no channels configured, returns a default HttpChannel."""
    config = {"runtime": {"agent": "main", "location": "process"}}
    ws = Workspace(".", ".bos", config)
    channels = ws.resolve_channels()
    assert len(channels) == 1
    assert channels[0].name == "HttpChannel"


def test_resolve_channels_rejects_duplicate_bind_addresses():
    """Duplicate bind_address across channels raises ValueError."""
    config = {
        "runtime": {
            "agent": "main",
            "location": "process",
            "channels": [
                {"name": "A", "bind_address": "channel@dup", "target_address": "agent@main"},
                {"name": "B", "bind_address": "channel@dup", "target_address": "agent@main"},
            ],
        }
    }
    ws = Workspace(".", ".bos", config)
    with pytest.raises(ValueError, match="Duplicate channel bind_address"):
        ws.resolve_channels()


def test_resolve_channels_rejects_channel_to_channel_topology():
    """Channel-to-channel routing is rejected."""
    config = {
        "runtime": {
            "agent": "main",
            "location": "process",
            "channels": [
                {"name": "A", "bind_address": "channel@a", "target_address": "channel@b"},
            ],
        }
    }
    ws = Workspace(".", ".bos", config)
    with pytest.raises(ValueError, match="targets unknown channel address"):
        ws.resolve_channels()


def test_runtime_config_location():
    """runtime.location maps to get_runtime_config().kind."""
    config = {"runtime": {"agent": "main", "location": "docker"}}
    ws = Workspace(".", ".bos", config)
    rt = ws.get_runtime_config()
    assert rt.kind == "docker"


def test_runtime_config_default_location():
    """Default location is process."""
    config = {"runtime": {"agent": "main"}}
    ws = Workspace(".", ".bos", config)
    rt = ws.get_runtime_config()
    assert rt.kind == "process"
