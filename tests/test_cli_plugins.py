"""Tests for boscli.commands entry-point CLI plugin discovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner

from bos.cli.entry import _LazyGroup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ep(name: str, obj: object) -> MagicMock:
    """Create a mock entry-point whose .load() returns *obj*."""
    ep = MagicMock()
    ep.name = name
    ep.load.return_value = obj
    return ep


def _build_group(lazy_commands: dict[str, str] | None = None) -> _LazyGroup:
    """Build a bare _LazyGroup with no lazy commands (unless provided)."""
    return _LazyGroup(name="cli", lazy_commands=lazy_commands or {})


# ---------------------------------------------------------------------------
# Plugin-only command
# ---------------------------------------------------------------------------


@patch("bos.cli.entry.entry_points")
def test_plugin_command_appears_in_list(mock_ep):
    """A plugin-only command shows up in list_commands."""
    plugin_cmd = click.Command("greet", callback=lambda: None)
    mock_ep.return_value = [_make_ep("greet", plugin_cmd)]

    group = _build_group()
    ctx = click.Context(group)
    assert "greet" in group.list_commands(ctx)


@patch("bos.cli.entry.entry_points")
def test_plugin_command_is_resolvable(mock_ep):
    """get_command returns a plugin-supplied command."""
    plugin_cmd = click.Command("greet", callback=lambda: None)
    mock_ep.return_value = [_make_ep("greet", plugin_cmd)]

    group = _build_group()
    ctx = click.Context(group)
    assert group.get_command(ctx, "greet") is plugin_cmd


# ---------------------------------------------------------------------------
# Command collision — built-in wins
# ---------------------------------------------------------------------------


@patch("bos.cli.entry.entry_points")
def test_builtin_command_wins_on_collision(mock_ep, caplog):
    """When a plugin registers the same name as a built-in *command*,
    the built-in takes precedence and a warning is logged."""
    builtin_cmd = click.Command("hello", callback=lambda: "builtin")
    plugin_cmd = click.Command("hello", callback=lambda: "plugin")
    mock_ep.return_value = [_make_ep("hello", plugin_cmd)]

    group = _build_group()
    group.add_command(builtin_cmd)

    ctx = click.Context(group)
    import logging

    with caplog.at_level(logging.WARNING, logger="bos.cli.entry"):
        result = group.get_command(ctx, "hello")

    assert result is builtin_cmd
    assert any("conflicts with a built-in" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# Group merge — subcommands combined
# ---------------------------------------------------------------------------


@patch("bos.cli.entry.entry_points")
def test_groups_are_merged(mock_ep):
    """When both built-in and plugin register a Group under the same name,
    plugin subcommands are merged into the built-in group."""
    builtin_group = click.Group("tools")
    builtin_group.add_command(click.Command("lint", callback=lambda: None))

    plugin_group = click.Group("tools")
    plugin_group.add_command(click.Command("format", callback=lambda: None))

    mock_ep.return_value = [_make_ep("tools", plugin_group)]

    parent = _build_group()
    parent.add_command(builtin_group)

    ctx = click.Context(parent)
    merged = parent.get_command(ctx, "tools")

    assert isinstance(merged, click.Group)
    sub_names = merged.list_commands(click.Context(merged))
    assert "lint" in sub_names
    assert "format" in sub_names


@patch("bos.cli.entry.entry_points")
def test_group_merge_builtin_subcommand_wins(mock_ep, caplog):
    """During group merge, a subcommand collision is skipped with a warning."""
    builtin_group = click.Group("tools")
    builtin_group.add_command(click.Command("lint", callback=lambda: "builtin"))

    plugin_group = click.Group("tools")
    plugin_group.add_command(click.Command("lint", callback=lambda: "plugin"))
    plugin_group.add_command(click.Command("format", callback=lambda: None))

    mock_ep.return_value = [_make_ep("tools", plugin_group)]

    parent = _build_group()
    parent.add_command(builtin_group)

    import logging

    ctx = click.Context(parent)
    with caplog.at_level(logging.WARNING, logger="bos.cli.entry"):
        merged = parent.get_command(ctx, "tools")

    # "lint" should still be the built-in
    lint_cmd = merged.get_command(click.Context(merged), "lint")
    assert lint_cmd.callback() == "builtin"
    # "format" should have been added from the plugin
    assert merged.get_command(click.Context(merged), "format") is not None
    assert any("conflicts with a built-in" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# Broken / invalid entry points
# ---------------------------------------------------------------------------


@patch("bos.cli.entry.entry_points")
def test_broken_entry_point_is_skipped(mock_ep, caplog):
    """An entry point that raises on .load() is skipped gracefully."""
    bad_ep = MagicMock()
    bad_ep.name = "broken"
    bad_ep.load.side_effect = ImportError("no such module")

    mock_ep.return_value = [bad_ep]

    import logging

    group = _build_group()
    ctx = click.Context(group)
    with caplog.at_level(logging.WARNING, logger="bos.cli.entry"):
        cmds = group.list_commands(ctx)

    assert "broken" not in cmds
    assert any("Failed to load" in msg for msg in caplog.messages)


@patch("bos.cli.entry.entry_points")
def test_non_command_entry_point_is_skipped(mock_ep, caplog):
    """An entry point that resolves to a non-click object is skipped."""
    mock_ep.return_value = [_make_ep("notcmd", "just a string")]

    import logging

    group = _build_group()
    ctx = click.Context(group)
    with caplog.at_level(logging.WARNING, logger="bos.cli.entry"):
        cmds = group.list_commands(ctx)

    assert "notcmd" not in cmds
    assert any("expected click.Command" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# Integration: existing built-in commands still work
# ---------------------------------------------------------------------------


def test_existing_commands_still_listed():
    """Smoke test: the real CLI group still lists the expected built-in commands."""
    from bos.cli.entry import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("init", "gen", "doctor", "gateway", "ask", "tui"):
        assert name in result.output
