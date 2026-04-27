from pathlib import Path

import click

from bos.core import _flock


@click.group(name="auth")
def auth():
    """Manage authentication for BOS AI."""
    pass


@auth.command(name="codex")
@click.option("--name", default="default", help="Name of the credential.")
def codex(name: str):
    """Authenticate with OpenAI Codex."""
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
    except ImportError:
        raise click.ClickException("oauth_cli_kit not installed. Run: uv pip install oauth-cli-kit")

    creds_dir = Path.home() / ".config" / "bos"
    creds_dir.mkdir(parents=True, exist_ok=True)

    token_path = creds_dir / f"codex_auth.{name}.json"
    from oauth_cli_kit.storage import FileTokenStorage

    storage = FileTokenStorage(str(token_path))

    token = None
    try:
        token = get_token(storage=storage)
    except Exception:
        pass

    if not (token and token.access):
        click.echo("Starting interactive OAuth login...\n")
        token = login_oauth_interactive(
            print_fn=lambda s: click.echo(s),
            prompt_fn=lambda s: click.prompt(s),
            originator="tradingdesk",
            storage=storage,
        )

    if not (token and token.access):
        raise click.ClickException("Authentication failed")

    click.echo(f"\n✓ Authenticated with OpenAI Codex ({getattr(token, 'account_id', '') or name})")
    click.echo(f"  Credentials saved to: {token_path}")


@auth.command(name="antigravity")
@click.option("--name", default="default", help="Name of the credential.")
def antigravity(name: str):
    """Authenticate with Google Antigravity."""
    import asyncio
    import json
    import webbrowser

    from bos.extensions.providers.antigravity_provider import login_antigravity

    creds_dir = Path.home() / ".config" / "bos"
    creds_dir.mkdir(parents=True, exist_ok=True)
    token_path = creds_dir / f"antigravity_auth.{name}.json"

    def on_url(url: str, msg: str):
        click.echo(f"{msg}\n\n  {url}\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        creds = asyncio.run(
            login_antigravity(
                on_auth=on_url,
                on_progress=lambda msg: click.echo(f"  {msg}"),
            )
        )
        with _flock(token_path):
            token_path.write_text(json.dumps(dict(creds.__dict__)))
        click.echo(f"\n✓ Authenticated with Google Antigravity ({creds.email or ''})")
        click.echo(f"  Credentials saved to: {token_path}")
    except Exception as e:
        raise click.ClickException(f"Authentication failed: {e}")


@auth.command(name="gemini-cli")
@click.option("--name", default="default", help="Name of the credential.")
def gemini_cli(name: str) -> None:
    """Authenticate with Gemini CLI.

    This command initiates an interactive OAuth 2.0 PKCE flow to authenticate with
    Google Cloud Code Assist using your personal Google account.
    """
    import asyncio
    import json
    import webbrowser

    from bos.extensions.providers.gemini_cli_provider import login_gemini_cli

    click.echo("\n" + "=" * 50)
    click.echo(" Gemini CLI Authentication")
    click.echo("=" * 50)

    creds_dir = Path.home() / ".config" / "bos"
    creds_dir.mkdir(parents=True, exist_ok=True)
    token_path = creds_dir / f"gemini_cli_auth.{name}.json"

    def on_url(url: str, msg: str):
        click.echo(f"{msg}\n\n  {url}\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        creds = asyncio.run(
            login_gemini_cli(
                on_auth=on_url,
                on_progress=lambda msg: click.echo(f"  {msg}"),
            )
        )
        with _flock(token_path):
            token_path.write_text(json.dumps(dict(creds.__dict__)))
        click.echo(f"\n✓ Authenticated with Gemini CLI ({creds.email or ''})")
        click.echo(f"  Credentials saved to: {token_path}")
    except Exception as e:
        raise click.ClickException(f"Authentication failed: {e}")
