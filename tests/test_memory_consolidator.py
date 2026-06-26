"""DefaultMemoryConsolidator — propose() over a stub AgentRunner."""

import pytest


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


class _StubAgentRunner:
    """Stands in for the disposable consolidation agent (BEP 12): records the
    run() call and returns a pre-canned validated payload, or raises."""

    def __init__(self, payload, *, error: Exception | None = None):
        self._payload = payload
        self._error = error
        self.calls = []

    async def run(self, message, *, kind=None, agent_cfg=None, schema=None, parent=None, model=None):
        from bos.core import AgentResult

        self.calls.append(
            {"message": message, "kind": kind, "agent_cfg": agent_cfg, "schema": schema, "parent": parent}
        )
        if self._error is not None:
            raise self._error
        return AgentResult(output=self._payload, structured=True)


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
        blm = _StubAgentRunner(payload)
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
    async def test_structured_output_error_raises_consolidation_unavailable(self):
        """A failed structured proposal is a failure, not 'nothing to consolidate'.
        It must raise so the job leaves the watermark in place and retries the turns."""
        from bos.core.agent import StructuredOutputError
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            ConsolidationUnavailable,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )

        blm = _StubAgentRunner({}, error=StructuredOutputError("no valid structured output"))
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

        blm = _StubAgentRunner({"operations": []})
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
        sent_schema = blm.calls[0]["schema"]
        assert sent_schema["type"] == "object"
        assert "operations" in sent_schema["properties"]
        item_schema = sent_schema["properties"]["operations"]["items"]
        assert "op" in item_schema["properties"]
        assert set(item_schema["required"]) >= {"op", "reason"}
        # Off-turn disposable agent: system prompt via agent_cfg, no parent turn.
        assert blm.calls[0]["agent_cfg"]["system_prompt"]
        assert blm.calls[0]["parent"] is None

    @pytest.mark.asyncio
    async def test_prompt_includes_transcript_and_candidates(self):
        from bos.core.contract import Message
        from bos.plugins.memory.consolidator import (
            ConsolidationPolicy,
            DefaultMemoryConsolidator,
            MemoryConsolidationRequest,
        )
        from bos.plugins.memory.scoped_memory import MemoryEntry

        blm = _StubAgentRunner({"operations": []})
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
        prompt = blm.calls[0]["message"]
        assert "dark mode" in prompt
        assert "m1" in prompt
        assert "prefers light mode" in prompt
        assert "existing user maxim text" in prompt
