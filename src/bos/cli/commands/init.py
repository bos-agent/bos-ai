from pathlib import Path

import click

from bos.config import Workspace


@click.command()
@click.pass_context
def init(ctx):
    """Initialize a new BOS workspace."""
    workspace_path = Path(ctx.obj.get("WORKSPACE", ".")).expanduser().resolve()
    bos_dir = workspace_path / ".bos"

    # Create the directory beforehand so Workspace picks it up as the target bos_dir
    bos_dir.mkdir(parents=True, exist_ok=True)

    ws = Workspace(workspace_path)
    try:
        ws.init()
        click.echo(f"Initialized BOS workspace at {bos_dir}")
    except FileExistsError:
        click.echo(f"Workspace already initialized at {bos_dir} (config.toml exists)")
