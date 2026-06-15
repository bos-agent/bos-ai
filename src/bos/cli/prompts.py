"""Interactive terminal prompt helpers for the CLI wizard.

Each helper uses prompt_toolkit widgets on an interactive TTY and falls back to
plain click prompts otherwise (``--yes``, pipes, CI). The fallback preserves the
historical numbered behavior so non-interactive callers and tests are stable.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass

import click


@dataclass
class Choice:
    """One row in a ``select`` menu. Non-selectable rows render as separators."""

    value: object
    label: str
    annotation: str = ""
    selectable: bool = True


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def select(message: str, choices: Sequence[Choice], *, default=None, _input=None, _output=None):
    selectables = [c for c in choices if c.selectable]
    if not selectables:
        raise ValueError("select() needs at least one selectable choice")
    if not is_interactive():
        return _select_fallback(message, selectables, default)
    return _select_interactive(message, list(choices), selectables, default, _input, _output)


def _select_fallback(message: str, selectables: list[Choice], default):
    click.echo(message)
    default_n = 1
    for i, choice in enumerate(selectables, start=1):
        suffix = f"  ({choice.annotation})" if choice.annotation else ""
        click.echo(f"  {i}. {choice.label}{suffix}")
        if default is not None and choice.value == default:
            default_n = i
    n = click.prompt("Choice", type=click.IntRange(1, len(selectables)), default=default_n)
    return selectables[int(n) - 1].value
