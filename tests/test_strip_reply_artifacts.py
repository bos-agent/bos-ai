from bos.core._utils import _strip_reply_artifacts, _strip_think


def test_strip_think_removes_think_blocks():
    assert _strip_think("<think>secret</think>Hello") == "Hello"
    assert _strip_think("no tags here") == "no tags here"
    assert _strip_think("") is None


def test_strips_parroted_attribution_label_and_thought_multi_actor():
    # Thinking models in multi-actor chats parrot the "[assistant: X]" label
    # they see in history, often after a "[thought: ...]" prefix. The thought
    # text itself can contain ']' so a naive bracket match would fail.
    raw = "[thought: weigh options [a] vs [b], answer directly.[assistant: Main]\nThe answer."
    assert _strip_reply_artifacts(raw, strip_labels=True) == "The answer."


def test_strips_said_form_label():
    raw = "[thought: hmm][assistant Researcher said]\nFindings here"
    assert _strip_reply_artifacts(raw, strip_labels=True) == "Findings here"


def test_strips_leading_thought_without_label_single_actor():
    raw = "[thought: brief plan] The actual reply."
    assert _strip_reply_artifacts(raw, strip_labels=False) == "The actual reply."


def test_leaves_plain_reply_untouched():
    assert _strip_reply_artifacts("Just a normal answer.", strip_labels=True) == "Just a normal answer."


def test_does_not_strip_literal_label_in_prose_when_labels_off():
    # Single-actor turns never parrot labels, so a literal "[assistant: X]" in
    # the reply (e.g. documentation) must be preserved.
    raw = "Add the line [assistant: Main] to your config."
    assert _strip_reply_artifacts(raw, strip_labels=False) == raw


def test_empty_and_thought_only_collapse_to_none():
    assert _strip_reply_artifacts("", strip_labels=True) is None
    assert _strip_reply_artifacts("[thought: only thinking, no answer]", strip_labels=False) is None
