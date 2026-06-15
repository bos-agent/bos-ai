import click

from bos.cli import prompts
from bos.cli.prompts import Choice


def test_select_fallback_returns_value_by_number(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: 2)
    choices = [Choice("a", "Apple"), Choice("b", "Banana"), Choice("c", "Cherry")]
    assert prompts.select("pick", choices) == "b"


def test_select_fallback_skips_non_selectable(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: 2)
    choices = [
        Choice("a", "Apple"),
        Choice(None, "-- separator --", selectable=False),
        Choice("b", "Banana"),
    ]
    # selectables are [a, b]; choice #2 -> b
    assert prompts.select("pick", choices) == "b"


def test_select_fallback_default_index(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    captured = {}

    def fake_prompt(msg, **kwargs):
        captured["default"] = kwargs.get("default")
        return kwargs.get("default")

    monkeypatch.setattr(click, "prompt", fake_prompt)
    choices = [Choice("a", "Apple"), Choice("b", "Banana")]
    assert prompts.select("pick", choices, default="b") == "b"
    assert captured["default"] == 2


def test_select_interactive_arrow_down(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    choices = [Choice("a", "Apple"), Choice("b", "Banana"), Choice("c", "Cherry")]
    with create_pipe_input() as inp:
        inp.send_text("\x1b[B\r")  # down arrow, then Enter
        result = prompts.select("pick", choices, _input=inp, _output=DummyOutput())
    assert result == "b"


def test_text_fallback(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "hello")
    assert prompts.text("msg", default="x") == "hello"


def test_password_fallback(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    seen = {}

    def fake_prompt(msg, **kwargs):
        seen.update(kwargs)
        return "secret"

    monkeypatch.setattr(click, "prompt", fake_prompt)
    assert prompts.password("Key") == "secret"
    assert seen.get("hide_input") is True


def test_confirm_fallback(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
    assert prompts.confirm("ok?", default=False) is True


def test_autocomplete_fallback(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: "openai/gpt-4.1")
    assert prompts.autocomplete("Model", ["a", "b"], default="a") == "openai/gpt-4.1"


def test_autocomplete_fallback_defaults_to_first_option(monkeypatch):
    # With no explicit default, an empty answer falls back to the first option.
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    seen = {}

    def fake_prompt(msg, **kwargs):
        seen.update(kwargs)
        return kwargs.get("default")

    monkeypatch.setattr(click, "prompt", fake_prompt)
    assert prompts.autocomplete("Model", ["first", "second"]) == "first"
    assert seen.get("default") == "first"


def _render_select(choices, default, keys="\r"):
    """Drive an interactive select() against a captured vt100 buffer."""
    import io

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    buf = io.StringIO()
    out = Vt100_Output(buf, lambda: Size(rows=24, columns=80), term="xterm-256color")
    with create_pipe_input() as inp:
        inp.send_text(keys)
        result = prompts.select("pick", choices, default=default, _input=inp, _output=out)
    return result, buf.getvalue()


def test_select_interactive_aligns_annotations(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)
    choices = [
        Choice("a", "gemini", "✓ GEMINI_API_KEY"),
        Choice("b", "openrouter", "set OPENROUTER_API_KEY"),
    ]
    _, raw = _render_select(choices, default="a")
    # Short label is padded to the longest label's width so the annotation
    # column lines up across rows.
    assert "❯ gemini    " in raw  # "gemini" + padding to len("openrouter")
    assert "  openrouter" in raw


def test_select_interactive_bolds_selected(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)
    choices = [Choice("a", "gemini", "✓ KEY"), Choice("b", "openai", "set KEY")]
    _, raw = _render_select(choices, default="a")
    # The selected row carries the bold SGR (1); annotations are dimmed (90).
    assert "\x1b[0;1m❯ gemini" in raw
    assert "\x1b[0;90m" in raw
