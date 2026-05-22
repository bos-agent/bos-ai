import pytest

from bos.core import Message
from bos.named_actors.actor import NamedAgent, _filter_tool_noise


class FakeMessageStore:
    def __init__(self, messages=None):
        self._messages = messages or []
        self.saved: list = []

    async def get_messages(self, chat_id, original=False):
        return self._messages

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


class RecordingConsolidator:
    def __init__(self):
        self.calls: list[list[Message]] = []

    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str:
        self.calls.append(messages)
        return "summary"


class TestFilterToolNoise:
    def test_removes_tool_role_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "Hi!",
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "1", "name": "echo", "content": "result"},
            {"role": "assistant", "content": "Done."},
        ]
        result = _filter_tool_noise(messages)
        assert result == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "assistant", "content": "Done."},
        ]

    def test_drops_assistant_with_only_tool_calls(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
            },
        ]
        assert _filter_tool_noise(messages) == []


@pytest.mark.asyncio
async def test_named_agent_renders_metadata_attribution():
    store = FakeMessageStore(
        [
            Message(
                llm_message={"role": "user", "content": "review this"},
                metadata={
                    "speaker_type": "user",
                    "to_actor": "bob",
                    "to_display": "Bob (architect)",
                },
            ),
            Message(
                llm_message={"role": "assistant", "content": "Looks good."},
                metadata={
                    "speaker_type": "actor",
                    "from_actor": "bob",
                    "from_display": "Bob (architect)",
                    "to": "user",
                },
            ),
        ]
    )
    agent = NamedAgent(
        message_store=store,
        consolidator=None,
        llm=FakeLLM(),
        tools=[],
    )
    history = await agent._get_chat_history("abc123", budget_model="test/model")
    assert history[0] == {"role": "user", "content": "[user -> Bob (architect)]: review this"}
    assert history[1] == {"role": "assistant", "content": "[Bob (architect) -> user]: Looks good."}


@pytest.mark.asyncio
async def test_named_agent_filters_tool_noise_from_history():
    store = FakeMessageStore(
        [
            Message(llm_message={"role": "user", "content": "search for X"}),
            Message(
                llm_message={
                    "role": "assistant",
                    "content": "Looking...",
                    "tool_calls": [{"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
                }
            ),
            Message(llm_message={"role": "tool", "tool_call_id": "1", "name": "search", "content": "found X"}),
            Message(llm_message={"role": "assistant", "content": "Found X."}),
        ]
    )
    agent = NamedAgent(
        message_store=store,
        consolidator=None,
        llm=FakeLLM(),
        tools=[],
    )
    history = await agent._get_chat_history("abc123")
    assert history == [
        {"role": "user", "content": "search for X"},
        {"role": "assistant", "content": "Looking..."},
        {"role": "assistant", "content": "Found X."},
    ]


@pytest.mark.asyncio
async def test_named_agent_compaction_passes_message_objects():
    store = FakeMessageStore(
        [
            Message(
                llm_message={"role": "user", "content": "review this large history"},
                metadata={
                    "speaker_type": "user",
                    "to_actor": "bob",
                    "to_display": "Bob (architect)",
                },
            ),
        ]
    )
    consolidator = RecordingConsolidator()
    agent = NamedAgent(
        message_store=store,
        consolidator=consolidator,
        llm=FakeLLM(),
        tools=[],
        max_tokens=1,
    )

    await agent._get_chat_history("abc123", budget_model="test/model")

    assert consolidator.calls
    assert all(isinstance(message, Message) for message in consolidator.calls[0])
    assert consolidator.calls[0][0].llm_message["content"] == "[user -> Bob (architect)]: review this large history"
