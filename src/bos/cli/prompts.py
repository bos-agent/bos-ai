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


def _select_interactive(message: str, choices: list[Choice], selectables: list[Choice], default, _input, _output):
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    pos = 0
    if default is not None:
        for i, choice in enumerate(selectables):
            if choice.value == default:
                pos = i
                break

    def get_text():
        current = selectables[pos].value
        lines = []
        for choice in choices:
            if not choice.selectable:
                lines.append(("class:sep", f"    {choice.label}\n"))
                continue
            selected = choice.value == current
            cursor = "❯ " if selected else "  "
            style = "class:cursor" if selected else ""
            text = f"{cursor}{choice.label}"
            if choice.annotation:
                text += f"    {choice.annotation}"
            lines.append((style, text + "\n"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        nonlocal pos
        pos = (pos - 1) % len(selectables)

    @kb.add("down")
    def _(event):
        nonlocal pos
        pos = (pos + 1) % len(selectables)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=selectables[pos].value)

    @kb.add("c-c")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    click.echo(message)
    kwargs = {}
    if _input is not None:
        kwargs["input"] = _input
    if _output is not None:
        kwargs["output"] = _output
    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(get_text), always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        **kwargs,
    )
    try:
        return app.run()
    except KeyboardInterrupt:
        raise click.Abort()
