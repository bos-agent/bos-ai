"""``boscli doctor`` CLI tests (BEP 9)."""

import socket

from click.testing import CliRunner

from bos.cli.entry import cli


def _invoke(args, **kwargs):
    return CliRunner().invoke(cli, args, **kwargs)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _init_project(workspace, *extra_args):
    result = _invoke(["init", str(workspace), "--yes", "--no-git", *extra_args])
    assert result.exit_code == 0, result.output
    return result


def test_doctor_healthy_project(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.delenv("BOS_MODEL", raising=False)
    _init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _invoke(["doctor"])

    assert result.exit_code == 0, result.output
    assert "config.toml parses and validates" in result.output
    assert "agent spec(s) load" in result.output
    assert "no model configured" in result.output  # warn, not fail
    assert "dynamic port" in result.output  # scaffolds default to port = 0


def test_doctor_checks_fixed_port(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    port = _free_port()
    config_file = tmp_path / ".bos" / "config.toml"
    content = config_file.read_text(encoding="utf-8").replace("port = 0", f"port = {port}")
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = _invoke(["doctor"])

    assert result.exit_code == 0, result.output
    assert f"port {port} free" in result.output


def test_doctor_fails_on_unset_channel_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    _init_project(tmp_path)
    config_file = tmp_path / ".bos" / "config.toml"
    config_file.write_text(
        config_file.read_text(encoding="utf-8")
        + '\n[[runtime.channels]]\ntype = "TelegramChannel"\nchannel_id = "telegram+main"\n'
        'display_name = "Telegram"\ntarget_actor = "main"\nsettings = { token_env = "TELEGRAM_BOT_TOKEN" }\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = _invoke(["doctor"])

    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN" in result.output


def test_doctor_fails_on_missing_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    _init_project(tmp_path)
    (tmp_path / ".bos" / ".env").unlink()
    monkeypatch.chdir(tmp_path)

    result = _invoke(["doctor"])

    assert result.exit_code == 1
    assert "missing: .env" in result.output


def test_doctor_package_flags_uninstalled_entry_point(tmp_path, monkeypatch):
    monkeypatch.delenv("BOS_CONFIG", raising=False)
    target = tmp_path / "pkgproj"
    target.mkdir()
    _invoke(["init", str(target), "--yes", "--no-git", "--archetype", "package"])
    monkeypatch.chdir(target)

    result = _invoke(["doctor"])

    # the package is not installed in the test interpreter — doctor must say so
    assert result.exit_code == 1
    assert "bos.exts entry point" in result.output
    assert "uv run boscli" in result.output
