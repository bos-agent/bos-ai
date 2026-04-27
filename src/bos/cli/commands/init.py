from pathlib import Path

import click

from bos.config import initialize_workspace


@click.command()
@click.pass_context
def init(ctx):
    """Initialize a new BOS workspace."""
    workspace_path = Path(ctx.obj.get("WORKSPACE", ".")).expanduser().resolve()
    try:
        bos_dir = initialize_workspace(workspace_path)
        click.echo(f"Initialized BOS workspace at {bos_dir}")
    except FileExistsError:
        bos_dir = workspace_path / ".bos"
        click.echo(f"Workspace already initialized at {bos_dir} (config.toml exists)")
