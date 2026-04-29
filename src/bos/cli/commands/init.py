import subprocess
from pathlib import Path

import click

from bos.config import WorkspaceResolutionError, initialize_workspace

_GITIGNORE_CONTENT = """\
.env
run
"""


@click.command()
@click.option("--dotbos", is_flag=True, default=False, help="Use .bos/config.toml layout instead of bos.toml.")
@click.option("--git", "init_git", is_flag=True, default=False, help="Run git init and create a .gitignore.")
@click.pass_context
def init(ctx, dotbos: bool, init_git: bool):
    """Initialize a new BOS workspace."""
    workspace_path = Path(ctx.obj.get("WORKSPACE", ".")).expanduser().resolve()
    try:
        bos_dir = initialize_workspace(workspace_path, dotbos=dotbos)
        click.echo(f"Initialized BOS workspace at {bos_dir}")
    except WorkspaceResolutionError as exc:
        raise click.ClickException(str(exc))

    if init_git:
        subprocess.run(["git", "init", str(workspace_path)], check=True)
        gitignore = workspace_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
            click.echo(f"Created {gitignore}")
        else:
            click.echo(f"{gitignore} already exists, skipping.")
