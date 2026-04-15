import importlib

import click

_LAZY_COMMANDS: dict[str, str] = {
    "auth":    "bos.cli.commands.auth:auth",
    "init":    "bos.cli.commands.init:init",
    "start":   "bos.cli.commands.agent:start",
    "stop":    "bos.cli.commands.agent:stop",
    "status":  "bos.cli.commands.agent:status",
    "restart": "bos.cli.commands.agent:restart",
    "tui":     "bos.cli.commands.agent:tui",
}


class _LazyGroup(click.Group):
    """Click group that lazily imports command modules on first access."""

    def __init__(self, *args, lazy_commands: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._lazy_commands = lazy_commands or {}

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(set(super().list_commands(ctx)) | set(self._lazy_commands))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.BaseCommand | None:
        if cmd := super().get_command(ctx, cmd_name):
            return cmd
        if cmd_name in self._lazy_commands:
            module_path, attr = self._lazy_commands[cmd_name].rsplit(":", 1)
            mod = importlib.import_module(module_path)
            return getattr(mod, attr)
        return None


@click.group(cls=_LazyGroup, lazy_commands=_LAZY_COMMANDS)
@click.option(
    "-w",
    "--workspace",
    type=click.Path(exists=False, file_okay=False, dir_okay=True),
    default=".",
    help="Path to the workspace directory.",
)
@click.pass_context
def cli(ctx, workspace):
    """BOS AI CLI"""
    ctx.ensure_object(dict)
    ctx.obj["WORKSPACE"] = workspace


def main():
    cli()


if __name__ == "__main__":
    main()
