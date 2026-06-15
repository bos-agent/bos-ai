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
