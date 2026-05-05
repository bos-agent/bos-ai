import math

from bos.core import Message
from bos.core.history import estimate_message_history_tokens, project_message_history


def test_project_message_history_preserves_structured_content_and_truncates_tool_output():
    structured = [{"type": "text", "text": "Describe this."}]
    long_tool_output = "x" * 151

    projected = project_message_history(
        [
            Message(llm_message={"role": "user", "content": structured}),
            Message(
                llm_message={
                    "role": "tool",
                    "content": long_tool_output,
                    "tool_call_id": "call-1",
                    "name": "Tool",
                }
            ),
        ]
    )

    assert projected[0]["content"] == structured
    assert projected[1]["content"] == ("x" * 147) + "..."
    assert projected[1]["tool_call_id"] == "call-1"
    assert projected[1]["name"] == "Tool"


def test_estimate_message_history_tokens_uses_litellm_metadata(monkeypatch):
    import litellm

    def fake_token_counter(*, model, messages):
        assert model == "test/model"
        assert messages == [{"role": "user", "content": "hello"}]
        return 42

    monkeypatch.setattr(litellm, "token_counter", fake_token_counter)

    estimate = estimate_message_history_tokens(
        [Message(llm_message={"role": "user", "content": "hello"})],
        budget_model="test/model",
    )

    assert estimate.estimated_tokens == 42
    assert estimate.model == "test/model"
    assert estimate.source == "litellm"


def test_estimate_message_history_tokens_fallback_is_conservative(monkeypatch):
    import litellm

    def fake_token_counter(*, model, messages):
        raise RuntimeError("no tokenizer")

    monkeypatch.setattr(litellm, "token_counter", fake_token_counter)
    messages = [Message(llm_message={"role": "user", "content": "hello"})]

    estimate = estimate_message_history_tokens(messages, budget_model="test/model")

    expected = math.ceil(len('[{"content": "hello", "role": "user"}]') / 3) + 8
    assert estimate.estimated_tokens == expected
    assert estimate.model == "test/model"
    assert estimate.source == "fallback"
