"""BEP 19 §3.4, §3.4.1, §3.5.1: per-agent config for an external runtime."""

from __future__ import annotations

import pytest


@pytest.fixture
def parse(tmp_path):
    from bos.extensions.runtimes._shared import parse_external_config

    (tmp_path / "services").mkdir()

    def _parse(**cfg):
        cfg.setdefault("permission", "read-only")
        return parse_external_config(cfg, runtime="codex", workspace=tmp_path)

    return _parse


def test_a_minimal_config_resolves(parse, tmp_path):
    resolved = parse()
    assert resolved.runtime == "codex"
    assert resolved.cwd == tmp_path.resolve()
    assert resolved.permission == "read-only"
    assert resolved.auth == "subscription"
    assert resolved.mcp_tools == ()


def test_cwd_resolves_under_the_workspace(parse, tmp_path):
    assert parse(cwd="services").cwd == (tmp_path / "services").resolve()


def test_a_missing_permission_names_the_key_and_its_values(tmp_path):
    from bos.extensions.runtimes._shared import parse_external_config

    with pytest.raises(ValueError) as excinfo:
        parse_external_config({}, runtime="codex", workspace=tmp_path)
    message = str(excinfo.value)
    assert "permission" in message
    for value in ("read-only", "workspace-write", "full-access"):
        assert value in message


def test_an_escaping_cwd_is_rejected(parse, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        parse(cwd="../outside")
    assert "outside" in str(excinfo.value)
    assert str(tmp_path.resolve()) in str(excinfo.value)


def test_a_symlinked_cwd_that_points_outside_is_rejected(parse, tmp_path):
    """Review Focus 1: Path.resolve() follows links, so the check must be post-resolve."""
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        parse(cwd="link")


def test_an_unknown_key_names_itself(parse):
    with pytest.raises(ValueError) as excinfo:
        parse(permisson="read-only")
    assert "permisson" in str(excinfo.value)


def test_a_resolver_written_external_runtime_is_accepted(parse):
    """Task 3 rejects a hand-written one at the config layer; here it is legitimate."""
    assert parse(external_runtime="codex").runtime == "codex"


def test_both_prompt_keys_is_an_error(parse):
    with pytest.raises(ValueError) as excinfo:
        parse(system_prompt="a", base_instructions="b")
    assert "system_prompt" in str(excinfo.value)
    assert "base_instructions" in str(excinfo.value)


def test_dropped_bos_keys_are_ignored_not_rejected(parse, caplog):
    """BEP 19 §3.9: these have no counterpart; they are dropped with one log line."""
    import logging

    with caplog.at_level(logging.DEBUG):
        resolved = parse(max_tokens=1000, plugins={"enabled": []}, tools=None, max_iterations=9)
    assert resolved.permission == "read-only"
    assert any("max_tokens" in record.message for record in caplog.records)


def test_mcp_tools_is_a_tuple_and_star_is_rejected(parse):
    assert parse(mcp_tools=["A", "B"]).mcp_tools == ("A", "B")
    with pytest.raises(ValueError) as excinfo:
        parse(mcp_tools=["*"])
    assert "*" in str(excinfo.value)
