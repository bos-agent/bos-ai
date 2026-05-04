# tests/squad/test_history.py
import pytest

from bos.squad.actor import SquadAgent, _filter_tool_noise


class FakeMessageStore:
    def __init__(self, messages=None):
        self._messages = messages or []
        self.saved: list = []

    async def get_messages(self, chat_id, original=False):
        from bos.core import Message
        return [Message(llm_message=m) for m in self._messages]

    async def save_messages(self, chat_id, messages):
        self.saved.extend(messages)

    async def save_summary(self, chat_id, summary):
        pass

    async def list_chats(self):
        return {}


class FakeLLM:
    async def complete(self, messages, **kwargs):
        from bos.core import LLMResponse
        return LLMResponse(content="test response", finish_reason="stop")


class TestFilterToolNoise:
    def test_removes_tool_role_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi!", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "1", "name": "echo", "content": "result"},
            {"role": "assistant", "content": "Done."},
        ]
        result = _filter_tool_noise(messages)
        assert len(result) == 3
        assert result[0] == {"role": "user", "content": "hello"}
        assert result[1] == {"role": "assistant", "content": "Hi!"}
        assert result[2] == {"role": "assistant", "content": "Done."}

    def test_preserves_non_tool_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        assert _filter_tool_noise(messages) == messages

    def test_handles_empty_list(self):
        assert _filter_tool_noise([]) == []

    def test_strips_tool_calls_from_assistant_with_content(self):
        messages = [
            {"role": "assistant", "content": "Let me check.", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
        ]
        result = _filter_tool_noise(messages)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check."
        assert "tool_calls" not in result[0]

    def test_drops_assistant_with_only_tool_calls(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
        ]
        assert _filter_tool_noise(messages) == []


class TestSquadAgentHistory:
    @pytest.mark.asyncio
    async def test_filters_tool_noise_from_history(self):
        store = FakeMessageStore([
            {"role": "user", "content": "search for X"},
            {"role": "assistant", "content": "Looking...", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "1", "name": "search", "content": "found X"},
            {"role": "assistant", "content": "Found X."},
        ])
        agent = SquadAgent(
            message_store=store,
            memory=None,
            consolidator=None,
            skills_loader=None,
            llm=FakeLLM(),
            tools=[],
        )
        history = await agent._get_chat_history("abc123")
        assert len(history) == 3
        assert history[0] == {"role": "user", "content": "search for X"}
        assert history[1] == {"role": "assistant", "content": "Looking..."}
        assert history[2] == {"role": "assistant", "content": "Found X."}
