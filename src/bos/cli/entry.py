import importlib
import os

import click

_LAZY_COMMANDS: dict[str, str] = {
    "auth": "bos.cli.commands.auth:auth",
    "init": "bos.cli.commands.init:init",
    "gateway": "bos.cli.commands.agent:gateway",
    "ask": "bos.cli.commands.agent:ask",
    "tui": "bos.cli.commands.agent:tui",
}

if os.environ.get("BOS_DEV"):
    _LAZY_COMMANDS["debug"] = "bos.cli.commands.debug:debug"


class _LazyGroup(click.Group):
    """Click group that lazily imports command modules on first access."""

    def __init__(self, *args, lazy_commands: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._lazy_commands = lazy_commands or {}

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(set(super().list_commands(ctx)) | set(self._lazy_commands))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd := super().get_command(ctx, cmd_name):
            return cmd
        if cmd_name in self._lazy_commands:
            module_path, attr = self._lazy_commands[cmd_name].rsplit(":", 1)
            mod = importlib.import_module(module_path)
            return getattr(mod, attr)
        return None


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
    import os
    import sys

    if log_level is None:
        log_level = os.environ.get("BOS_LOG_LEVEL", "ERROR")
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )

    ctx.ensure_object(dict)
    ctx.obj["CONFIG"] = config or os.environ.get("BOS_CONFIG")


def main():
    cli()


if __name__ == "__main__":
    main()
