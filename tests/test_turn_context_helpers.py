"""TurnContext helpers — get_last_user_text() reliably extracts the incoming
user message regardless of whether content is a str or a list of parts."""

from bos.core.agent import TurnContext
from bos.core.contract import Message


def _ctx(messages: list[Message]) -> TurnContext:
    ctx = TurnContext(agent_name="A", chat_id="c1", turn_id="t1")
    ctx.current = list(messages)
    return ctx


def _msg(role: str, content) -> Message:
    return Message(llm_message={"role": role, "content": content})


class TestGetLastUserText:
    def test_empty_returns_empty_string(self):
        assert _ctx([]).get_last_user_text() == ""

    def test_no_user_message_returns_empty(self):
        assert _ctx([_msg("assistant", "hi"), _msg("tool", "result")]).get_last_user_text() == ""

    def test_string_content(self):
        assert _ctx([_msg("user", "what is up?")]).get_last_user_text() == "what is up?"

    def test_returns_most_recent_user_message(self):
        ctx = _ctx([_msg("user", "first"), _msg("assistant", "ack"), _msg("user", "second")])
        assert ctx.get_last_user_text() == "second"

    def test_list_of_text_parts_concatenates(self):
        parts = [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]
        assert _ctx([_msg("user", parts)]).get_last_user_text() == "hello world"

    def test_list_with_non_text_parts_skips_them(self):
        parts = [
            {"type": "text", "text": "before "},
            {"type": "image", "source": {"kind": "url", "value": "http://x"}},
            {"type": "text", "text": "after"},
        ]
        assert _ctx([_msg("user", parts)]).get_last_user_text() == "before after"

    def test_missing_content_returns_empty(self):
        ctx = TurnContext(agent_name="A", chat_id="c1", turn_id="t1")
        ctx.current = [Message(llm_message={"role": "user"})]
        assert ctx.get_last_user_text() == ""
