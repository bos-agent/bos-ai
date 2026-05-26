import pytest

from bos.core import Message
from bos.core._chat_store_utils import filter_tool_noise
from bos.core.contract import ContextResult, TokenEstimate
from bos.named_actors.actor import NamedAgent


class FakeChatStore:
    def __init__(self, messages=None):
        self._messages = messages or []
        self.saved: list = []
        self.summaries: list = []

    async def save_turn(self, chat_id, messages, *, turn_id=None):
        self.saved.extend(messages)

    async def get_context(self, chat_id, *, tokenizer_model=None, filter_mode=None):
        filtered = filter_tool_noise(self._messages, mode=filter_mode or "keep_signatures")
        from bos.core._chat_store_utils import project_message
        projected = [project_message(m) for m in filtered]
        return ContextResult(
            messages=projected,
            source_messages=filtered,
            estimated_tokens=100,
            tokenizer_model=tokenizer_model,
            estimation_source="fallback",
            filter_mode=filter_mode or "keep_signatures",
            summary_applied=False,
            summary_message_count_excluded=0,
        )

    async def get_compaction_messages(self, chat_id, *, filter_mode=None):
        return filter_tool_noise(self._messages, mode=filter_mode or "keep_signatures")

    async def estimate_tokens(self, chat_id, *, tokenizer_model=None, filter_mode=None):
        return TokenEstimate(count=100, tokenizer_model=tokenizer_model, source="fallback")

    async def save_summary(self, chat_id, summary):
        self.summaries.append(summary)
        self._messages.append(Message(
            llm_message={"role": "system", "content": f"Chat summary:\n{summary}"},
            is_summary=True,
        ))

    async def get_summary(self, chat_id):
        return None

    async def get_messages(self, chat_id, *, active_only=True):
        return self._messages

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
            Message(llm_message={"role": "user", "content": "hello"}),
            Message(llm_message={
                "role": "assistant",
                "content": "Hi!",
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            }),
            Message(llm_message={"role": "tool", "tool_call_id": "1", "name": "echo", "content": "result"}),
            Message(llm_message={"role": "assistant", "content": "Done."}),
        ]
        result = filter_tool_noise(messages, mode="strip_all")
        result_dicts = [m.llm_message for m in result]
        assert result_dicts == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "assistant", "content": "Done."},
        ]

    def test_drops_assistant_with_only_tool_calls(self):
        messages = [
            Message(llm_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
            }),
        ]
        result = filter_tool_noise(messages, mode="strip_all")
        assert result == []


@pytest.mark.asyncio
async def test_named_agent_renders_metadata_attribution():
    store = FakeChatStore(
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
        kind="test",
        agent_name="test",
        chat_store=store,
        consolidator=None,
        llm=FakeLLM(),
        tools=[],
    )
    result = await agent._chat_store.get_context("abc123", tokenizer_model="test/model")
    history = agent._format_history(result)
    assert history[0] == {"role": "user", "content": "[user -> Bob (architect)]: review this"}
    assert history[1] == {"role": "assistant", "content": "[Bob (architect) -> user]: Looks good."}


@pytest.mark.asyncio
async def test_named_agent_filters_tool_noise_from_history():
    store = FakeChatStore(
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
        kind="test",
        agent_name="test",
        chat_store=store,
        consolidator=None,
        llm=FakeLLM(),
        tools=[],
    )
    result = await agent._chat_store.get_context("abc123")
    history = agent._format_history(result)
    # Default keep_signatures mode: tool messages dropped, signatures inlined
    assert history[0] == {"role": "user", "content": "search for X"}
    assert "Looking..." in history[1]["content"]
    assert "[tool call: search(" in history[1]["content"]
    assert history[2] == {"role": "assistant", "content": "Found X."}


@pytest.mark.asyncio
async def test_named_agent_compaction_passes_message_objects():
    import asyncio

    store = FakeChatStore(
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
    compaction_locks: dict[str, asyncio.Lock] = {}

    def get_lock(chat_id: str) -> asyncio.Lock:
        if chat_id not in compaction_locks:
            compaction_locks[chat_id] = asyncio.Lock()
        return compaction_locks[chat_id]

    agent = NamedAgent(
        kind="test",
        agent_name="test",
        chat_store=store,
        consolidator=consolidator,
        llm=FakeLLM(),
        tools=[],
        max_tokens=1,
        chat_compaction_lock=get_lock,
    )

    # Compaction happens in _load_and_compact_history which also calls _format_history
    await agent._load_and_compact_history("abc123", budget_model="test/model")

    assert consolidator.calls
    assert all(isinstance(message, Message) for message in consolidator.calls[0])
    # Compaction messages are filtered but not attributed; attribution happens in _format_history
    assert consolidator.calls[0][0].llm_message["content"] == "review this large history"
