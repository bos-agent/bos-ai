import importlib
import logging
import os
from importlib.metadata import entry_points

import click

logger = logging.getLogger(__name__)

PLUGIN_ENTRY_POINT_GROUP = "boscli.commands"

_LAZY_COMMANDS: dict[str, str] = {
    "init": "bos.cli.commands.scaffolding:init",
    "gen": "bos.cli.commands.scaffolding:gen",
    "doctor": "bos.cli.commands.doctor:doctor",
    "inspect": "bos.cli.commands.inspect:inspect",
    "gateway": "bos.cli.commands.agent:gateway",
    "ask": "bos.cli.commands.agent:ask",
    "tui": "bos.cli.commands.agent:tui",
    "memory": "bos.cli.commands.memory:memory",
}


def _load_lazy_command(dotted: str) -> click.Command:
    """Import a command from a 'module.path:attr' string."""
    module_path, attr = dotted.rsplit(":", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, attr)


def _merge_groups(builtin: click.Group, plugin: click.Group) -> click.Group:
    """Merge *plugin* subcommands into *builtin*, skipping collisions."""
    for name in plugin.list_commands(click.Context(plugin)):
        if builtin.get_command(click.Context(builtin), name) is not None:
            logger.warning(
                "CLI plugin subcommand '%s' under group '%s' conflicts with a built-in; skipping",
                name,
                builtin.name,
            )
            continue
        cmd = plugin.get_command(click.Context(plugin), name)
        if cmd is not None:
            builtin.add_command(cmd, name)
    return builtin


class _LazyGroup(click.Group):
    """Click group that lazily imports command modules on first access.

    Commands are discovered from two sources (in priority order):

    1. **Built-in lazy commands** — the ``lazy_commands`` dict passed at
       construction time (``module.path:attr`` strings).
    2. **Entry-point plugins** — any installed package that advertises the
       ``boscli.commands`` `entry-point group
       <https://packaging.python.org/en/latest/specifications/entry-points/>`_.

    Name-collision rules:
    * If both a built-in and a plugin register a :class:`click.Group` under
      the same name, their subcommands are **merged** (built-in subcommands
      win on further collisions).
    * If a plugin name collides with a built-in *command* (non-group), the
      built-in wins and a warning is logged.
    """

    def __init__(self, *args, lazy_commands: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._lazy_commands = lazy_commands or {}
        self._plugin_commands: dict[str, click.Command] | None = None

    # -- plugin discovery (cached) ----------------------------------------

    def _discover_plugins(self) -> dict[str, click.Command]:
        """Load entry-point advertised commands once, return name→Command map."""
        if self._plugin_commands is not None:
            return self._plugin_commands

        self._plugin_commands = {}
        for ep in entry_points(group=PLUGIN_ENTRY_POINT_GROUP):
            try:
                cmd = ep.load()
            except Exception:
                logger.warning("Failed to load CLI plugin entry point '%s'", ep.name, exc_info=True)
                continue
            if not isinstance(cmd, click.Command):
                logger.warning(
                    "CLI plugin entry point '%s' resolved to %s, expected click.Command; skipping",
                    ep.name,
                    type(cmd).__name__,
                )
                continue
            self._plugin_commands[ep.name] = cmd  # type: ignore[assignment]  # Group is a Command
        return self._plugin_commands

    # -- click.Group interface --------------------------------------------

    def list_commands(self, ctx: click.Context) -> list[str]:
        builtin = set(super().list_commands(ctx)) | set(self._lazy_commands)
        plugin = set(self._discover_plugins())
        return sorted(builtin | plugin)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # 1. Already-registered (e.g. from a previous merge)
        if cmd := super().get_command(ctx, cmd_name):
            return self._maybe_merge_plugin(cmd_name, cmd)

        # 2. Built-in lazy command
        if cmd_name in self._lazy_commands:
            cmd = _load_lazy_command(self._lazy_commands[cmd_name])
            return self._maybe_merge_plugin(cmd_name, cmd)

        # 3. Plugin-only command
        plugins = self._discover_plugins()
        return plugins.get(cmd_name)

    def _maybe_merge_plugin(self, name: str, builtin: click.Command) -> click.Command:
        """If a plugin registered the same *name*, merge or warn."""
        plugins = self._discover_plugins()
        plugin = plugins.get(name)
        if plugin is None:
            return builtin

        # Both are groups → merge subcommands
        if isinstance(builtin, click.Group) and isinstance(plugin, click.Group):
            return _merge_groups(builtin, plugin)

        # Collision on a non-group command → built-in wins
        logger.warning(
            "CLI plugin command '%s' conflicts with a built-in command; the built-in takes precedence",
            name,
        )
        return builtin


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@click.group(cls=_LazyGroup, lazy_commands=_LAZY_COMMANDS)
@click.option(
    "-c",
    "--config",
    "config",
    default=None,
    help="Path to a BOS config file or a built-in preset name (e.g. 'coding').",
)
@click.option(
    "-l",
    "--log-level",
    "log_level",
    type=click.Choice(LOG_LEVELS, case_sensitive=False),
    default=None,
    help="Set the logging level (default: ERROR).",
)
@click.pass_context
def cli(ctx, config, log_level):
    """BOS AI CLI"""
    import logging
    import sys

    if log_level is None:
        log_level = os.environ.get("BOS_LOG_LEVEL", "ERROR")
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )

    ctx.ensure_object(dict)
    ctx.obj["CONFIG"] = config or os.environ.get("BOS_CONFIG")


def main():
    cli()


if __name__ == "__main__":
    main()
