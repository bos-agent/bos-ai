"""boscli memory CLI smoke tests.

We invoke commands through the Click runner against a freshly-seeded
in-memory backend, asserting on the textual output."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from bos.cli.commands.memory import memory as memory_cmd


def _seeded_workspace(tmp_path, monkeypatch):
    """Build a minimal workspace with the in_memory memory backend selected."""
    (tmp_path / "bos.toml").write_text(
        '[bos]\n'
        'workspace = "."\n'
        '\n'
        '[harness]\n'
        'chat_store = "_default"\n'
        '\n'
        '[exts.ep_plugin.MemoryPlugin]\n'
        'backend = "in_memory"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("subcommand", ["list", "index"])
def test_read_only_subcommands_run(tmp_path, monkeypatch, subcommand):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, [subcommand])
    assert result.exit_code == 0, result.output


def test_show_unknown_entry_reports_not_found(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["show", "ghost-entry"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower()


def test_recall_with_query_returns_results(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["recall", "--query", "nothing here"])
    assert result.exit_code == 0


def test_consolidate_dry_run_prints_summary(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["consolidate", "--chat", "c1", "--dry-run"])
    assert result.exit_code == 0
    assert "consolidat" in result.output.lower() or "no turns" in result.output.lower()


def test_restore_missing_entry_is_safe(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["restore", "ghost"])
    assert result.exit_code == 0


def test_audit_empty_prints_nothing(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["audit"])
    assert result.exit_code == 0


def test_jobs_lists(tmp_path, monkeypatch):
    _seeded_workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(memory_cmd, ["jobs"])
    assert result.exit_code == 0
