"""BEP 19 §3.2–§3.4: the external-runtime seam."""

from __future__ import annotations

import pytest
from conftest import _FakeRuntime, create_test_agent


def test_agent_satisfies_agent_port():
    from bos.core.agent import AgentPort

    assert isinstance(create_test_agent(), AgentPort)


def test_agent_port_accepts_a_minimal_duck_type():
    from bos.core.agent import AgentPort, AgentResult

    class Minimal:
        @property
        def name(self) -> str:
            return "minimal"

        def request_stop(self) -> None:
            pass

        async def ask(self, chat_id, content, **kwargs) -> str:
            return "ok"

        async def run(self, chat_id, content, **kwargs) -> AgentResult:
            return AgentResult(output="ok")

    assert isinstance(Minimal(), AgentPort)


def test_agent_port_rejects_an_object_missing_run():
    from bos.core.agent import AgentPort

    class NoRun:
        @property
        def name(self) -> str:
            return "x"

        def request_stop(self) -> None:
            pass

        async def ask(self, chat_id, content, **kwargs) -> str:
            return ""

    assert not isinstance(NoRun(), AgentPort)


@pytest.mark.asyncio
async def test_a_reserved_kind_builds_the_runtime_not_an_agent(tmp_path, fake_runtimes):
    from bos.core.agent import Agent
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("codex", agent_cfg={"permission": "read-only"})
        assert isinstance(agent, _FakeRuntime)
        assert not isinstance(agent, Agent)
        assert agent.name == "codex"


@pytest.mark.asyncio
async def test_an_external_runtime_key_dispatches_and_is_not_passed_on(tmp_path, fake_runtimes):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("george", agent_cfg={"external_runtime": "codex"})
        assert isinstance(agent, _FakeRuntime)
        assert agent.name == "george", "kind stays the agent's own name, BEP 19 §3.2"
        assert agent.cfg["external_runtime"] == "codex", "read, not consumed: inspect reports it"


@pytest.mark.asyncio
async def test_the_runtime_is_closed_with_the_harness(tmp_path, fake_runtimes):
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent("codex")
    assert agent.closed is True


@pytest.mark.asyncio
async def test_a_missing_extra_names_the_extra_to_install(tmp_path, monkeypatch):
    from bos.core import harness as harness_mod
    from bos.core.harness import AgentHarness

    monkeypatch.setitem(harness_mod.EXTERNAL_AGENT_KINDS, "codex", "bos_nonexistent_module:CodexAgent")
    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        with pytest.raises(RuntimeError) as excinfo:
            await harness.create_agent("codex")
    message = str(excinfo.value)
    assert "bos-ai[codex]" in message
    assert "codex" in message


@pytest.mark.asyncio
async def test_a_normal_kind_is_untouched(tmp_path, fake_runtimes):
    from bos.core.agent import Agent
    from bos.core.harness import AgentHarness

    async with AgentHarness(bos_dir=tmp_path, workspace=tmp_path) as harness:
        agent = await harness.create_agent(agent_cfg={"system_prompt": "hi", "tools": []})
        assert isinstance(agent, Agent)
