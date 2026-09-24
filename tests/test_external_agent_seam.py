"""BEP 19 §3.2–§3.4: the external-runtime seam."""

from __future__ import annotations

from conftest import create_test_agent


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
