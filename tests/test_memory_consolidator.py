"""DefaultMemoryConsolidator — propose() over a stub BackgroundLLM."""

import json

import pytest

from bos.core.agent import LLMResponse


class TestStructural:
    def test_request_and_policy_exist(self):
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            MemoryConsolidationRequest,
            MemoryConsolidator,
        )

        pol = ConsolidationPolicy(enabled=True, retention_days=30)
        assert pol.enabled is True
        req = MemoryConsolidationRequest(
            chat_id="c1",
            actor_name="A",
            base_revision=4,
            trigger="manual",
            transcript_window=[],
            raw_appends=[],
            candidate_memories=[],
            active_maxims={},
            policy=pol,
        )
        assert req.chat_id == "c1"
        assert hasattr(MemoryConsolidator, "propose")

    def test_schema_requires_source_turn_ids(self):
        """source_turn_ids must be required so the model populates it rather than
        dropping it as an optional field under provider controlled generation."""
        from bos.plugins.memory.consolidator import _RESPONSE_SCHEMA

        item_required = _RESPONSE_SCHEMA["properties"]["operations"]["items"]["required"]
        assert "source_turn_ids" in item_required


class _StubBackgroundLLM:
    def __init__(self, payload, *, raw: str | None = None):
        self._payload = payload
        self._raw = raw
        self.calls = []

    async def ask(self, **kwargs):
        self.calls.append(kwargs)
        content = self._raw if self._raw is not None else json.dumps(self._payload)
        return LLMResponse(content=content)


class TestDefaultConsolidator:
    @pytest.mark.asyncio
    async def test_parses_operations_payload(self):
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )

        payload = {
            "operations": [
                {"op": "ADD", "reason": "stable preference", "content": "likes dark mode", "importance": 7},
                {"op": "NOOP", "reason": "considered, declined"},
            ]
        }
        blm = _StubBackgroundLLM(payload)
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1",
            actor_name=None,
            base_revision=1,
            trigger="manual",
            transcript_window=[],
            raw_appends=[],
            candidate_memories=[],
            active_maxims={"user": ""},
            policy=ConsolidationPolicy(),
        )
        ops = await c.propose(req)
        assert [o.op for o in ops] == ["ADD", "NOOP"]
        assert ops[0].importance == 7
        assert ops[1].reason == "considered, declined"

    @pytest.mark.asyncio
    async def test_unparseable_response_raises_consolidation_unavailable(self):
        """A non-JSON response is a failure, not 'nothing to consolidate'. It must
        raise so the job leaves the watermark in place and retries the turns."""
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            ConsolidationUnavailable,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )

        blm = _StubBackgroundLLM({}, raw="not json at all")
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1",
            actor_name=None,
            base_revision=1,
            trigger="manual",
            transcript_window=[],
            raw_appends=[],
            candidate_memories=[],
            active_maxims={},
            policy=ConsolidationPolicy(),
        )
        with pytest.raises(ConsolidationUnavailable):
            await c.propose(req)

    @pytest.mark.asyncio
    async def test_response_schema_required_keys(self):
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )

        blm = _StubBackgroundLLM({"operations": []})
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1",
            actor_name=None,
            base_revision=1,
            trigger="manual",
            transcript_window=[],
            raw_appends=[],
            candidate_memories=[],
            active_maxims={},
            policy=ConsolidationPolicy(),
        )
        await c.propose(req)
        sent_schema = blm.calls[0]["response_schema"]
        assert sent_schema["type"] == "object"
        assert "operations" in sent_schema["properties"]
        item_schema = sent_schema["properties"]["operations"]["items"]
        assert "op" in item_schema["properties"]
        assert set(item_schema["required"]) >= {"op", "reason"}

    @pytest.mark.asyncio
    async def test_prompt_includes_transcript_and_candidates(self):
        from bos.core.contract import Message
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )
        from bos.plugins.memory.scoped_memory import MemoryEntry

        blm = _StubBackgroundLLM({"operations": []})
        c = DefaultMemoryConsolidator(blm, maxim_keys={"user"})
        req = MemoryConsolidationRequest(
            chat_id="c1",
            actor_name=None,
            base_revision=1,
            trigger="manual",
            transcript_window=[Message(llm_message={"role": "user", "content": "I prefer dark mode"})],
            raw_appends=[],
            candidate_memories=[MemoryEntry(id="m1", content="prefers light mode")],
            active_maxims={"user": "existing user maxim text"},
            policy=ConsolidationPolicy(),
        )
        await c.propose(req)
        prompt = blm.calls[0]["messages"][-1]["content"]
        assert "dark mode" in prompt
        assert "m1" in prompt
        assert "prefers light mode" in prompt
        assert "existing user maxim text" in prompt
