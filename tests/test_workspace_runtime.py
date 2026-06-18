"""Tests for final BEP 7 [runtime] gateway config access via Workspace."""

import pytest

from bos.config.workspace import Workspace


def test_runtime_agent_defaults_without_runtime_for_oneshot_helpers():
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
                    "agent_cfg": {
                        "tools": {
                            "enabled": ["ReadFile"],
                            "disabled": ["WriteFile"],
                            "usages": {"ReadFile": "Read only what you need."},
                        },
                        "plugin-bindings": {"SubagentPlugin": {"enabled": ["*"]}},
                    },
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
    assert ws.get_main_agent_kind() == "assistant"
    assert actors["main"].address == "agent@main"
    assert actors["main"].agent_overrides == {
        "tools": ["ReadFile"],
        "exclude_tools": ["WriteFile"],
        "tools_usage": {"ReadFile": "Read only what you need."},
        "plugin-bindings": {"SubagentPlugin": {"enabled": ["*"]}},
    }
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


def test_gateway_runtime_rejects_actor_overflow_agent_overrides():
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        Workspace(
            ".",
            ".bos",
            {
                "runtime": {
                    "default_actor": "main",
                    "actors": {
                        "main": {
                            "agent": "main",
                            "tools": {"enabled": ["ReadFile"]},
                        }
                    },
                }
            },
        )


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


def test_gateway_runtime_rejects_legacy_runtime_agent():
    with pytest.raises(Exception, match="runtime.agent"):
        Workspace(".", ".bos", {"runtime": {"agent": "main", "actors": {"main": {"agent": "main"}}}})


def test_gateway_runtime_rejects_legacy_main_section():
    with pytest.raises(Exception, match=r"\[main\]"):
        Workspace(".", ".bos", {"main": {"agent": "main"}})


@pytest.mark.parametrize("legacy_key", ["name", "bind_address", "target_address"])
def test_gateway_runtime_rejects_legacy_channel_keys(legacy_key):
    channel = {"type": "TelegramChannel", "channel_id": "telegram:daily", legacy_key: "legacy"}
    if legacy_key == "name":
        channel = {"name": "TelegramChannel", "channel_id": "telegram:daily"}

    with pytest.raises(Exception, match=legacy_key):
        Workspace(
            ".",
            ".bos",
            {
                "runtime": {
                    "default_actor": "main",
                    "actors": {"main": {"agent": "main"}},
                    "channels": [channel],
                }
            },
        )


def test_runtime_config_location():
    config = {"runtime": {"location": "docker", "actors": {"main": {"agent": "main"}}}}
    ws = Workspace(".", ".bos", config)
    rt = ws.get_runtime_config()
    assert rt.kind == "docker"


def test_runtime_config_default_location():
    config = {"runtime": {"actors": {"main": {"agent": "main"}}}}
    ws = Workspace(".", ".bos", config)
    rt = ws.get_runtime_config()
    assert rt.kind == "process"
