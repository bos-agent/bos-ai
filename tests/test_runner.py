from bos.runner.runner import _normalize_channel_config_for_runtime


def test_http_channel_binds_all_interfaces_in_docker():
    cfg = _normalize_channel_config_for_runtime(
        {"name": "HttpChannel", "host": "127.0.0.1", "port": 8080},
        runtime_kind="docker",
    )

    assert cfg["host"] == "0.0.0.0"


def test_non_http_channel_config_is_unchanged():
    cfg = {"name": "TelegramChannel", "token": "x"}

    assert _normalize_channel_config_for_runtime(cfg, runtime_kind="docker") == cfg
