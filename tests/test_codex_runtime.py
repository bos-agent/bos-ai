"""BEP 19 Layer 4a: the Codex runtime.

Stage 2 of 4 over CodexAgent's turn path: construction, config resolution and
lifecycle (Task 3), plus the client and thread lifecycle (Task 4) — lazily
starting one AsyncCodex and mapping a chat_id onto a Codex thread. Task 5
makes a turn actually run, Tasks 6-8 add streaming, interrupt/timeout and the
approval handler — ask()/run() raising NotImplementedError here is that
staging, not a gap that this file's tests are meant to cover yet.
"""

from __future__ import annotations

from typing import Any

import pytest

from bos.extensions.chat_stores.in_memory import InMemChatStore


def _agent(tmp_path, fake_codex, **cfg: Any):
    """Build a CodexAgent with sane defaults: permission="read-only" and no cwd
    override unless the caller passes one. Used by every later task.

    `chat_store` is popped out of **cfg and passed straight to the constructor
    rather than validated as agent config; everything else in **cfg becomes the
    config dict. `fake_codex` is a required parameter (not read here) so every
    caller is guaranteed the `_CODEX_FACTORY` patch is live before the agent
    goes on to build a client from it.
    """
    from bos.extensions.runtimes.codex import CodexAgent

    chat_store = cfg.pop("chat_store", None)
    cfg.setdefault("permission", "read-only")
    return CodexAgent(kind="george", cfg=cfg, chat_store=chat_store, workspace=tmp_path, mcp=lambda: None)


@pytest.fixture
def mem_store() -> InMemChatStore:
    return InMemChatStore()


@pytest.mark.asyncio
async def test_construction_parses_and_resolves_the_config(tmp_path, fake_codex):
    (tmp_path / "services").mkdir()
    agent = _agent(tmp_path, fake_codex, permission="workspace-write", cwd="services", model="gpt-5.1-codex")

    assert agent.name == "george"
    assert agent.resolved_config["cwd"] == str((tmp_path / "services").resolve())
    assert agent.resolved_config["permission"] == "workspace-write"
    assert agent.resolved_config["model"] == "gpt-5.1-codex"


@pytest.mark.asyncio
async def test_construction_rejects_an_invalid_config(tmp_path, fake_codex):
    """The runtime is the only thing that calls parse_external_config — BEP 19 §8.2."""
    from bos.extensions.runtimes.codex import CodexAgent

    with pytest.raises(ValueError) as excinfo:
        CodexAgent(kind="george", cfg={}, chat_store=None, workspace=tmp_path, mcp=lambda: None)
    assert "permission" in str(excinfo.value)


@pytest.mark.asyncio
async def test_construction_rejects_an_escaping_cwd(tmp_path, fake_codex):
    with pytest.raises(ValueError):
        _agent(tmp_path, fake_codex, cwd="../outside")


@pytest.mark.asyncio
async def test_no_app_server_is_started_at_construction(tmp_path, fake_codex):
    """Building an agent must not spawn a child — BEP 19 §3.1 says lazily on first turn."""
    _agent(tmp_path, fake_codex)
    assert fake_codex.instances == []


@pytest.mark.asyncio
async def test_aclose_without_a_client_is_a_noop(tmp_path, fake_codex):
    agent = _agent(tmp_path, fake_codex)
    await agent.aclose()  # must not raise on an agent that never ran a turn


@pytest.mark.asyncio
async def test_it_satisfies_the_external_runtime_protocol(tmp_path, fake_codex):
    from bos.core.agent import ExternalRuntime

    agent = _agent(tmp_path, fake_codex)
    assert isinstance(agent, ExternalRuntime)


# ── Task 4: client and thread lifecycle (BEP 19 §3.6, §3.10.3) ─────────────


@pytest.mark.asyncio
async def test_ensure_client_builds_one_client_per_agent(tmp_path, fake_codex):
    """§3.10.1: one AsyncCodex per CodexAgent, built lazily — not per turn."""
    agent = _agent(tmp_path, fake_codex)
    first = await agent._ensure_client()
    second = await agent._ensure_client()

    assert first is second
    assert len(fake_codex.instances) == 1


@pytest.mark.asyncio
async def test_subscription_auth_preflight_fails_loudly_without_an_account(tmp_path, fake_codex):
    """§3.10.3: fail loudly rather than silently falling back to API-key billing."""
    fake_codex.arm(account_error=RuntimeError("not logged in"))
    agent = _agent(tmp_path, fake_codex)  # auth defaults to "subscription"

    with pytest.raises(RuntimeError) as excinfo:
        await agent._ensure_client()
    assert "codex" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_new_chat_starts_a_thread_with_the_resolved_config(tmp_path, fake_codex, mem_store):
    agent = _agent(tmp_path, fake_codex, permission="workspace-write", cwd="services")
    thread, started = await agent._thread_for("chat-1")

    assert started is True
    (kwargs,) = fake_codex.instances[0].thread_start_calls
    assert kwargs["cwd"] == str((tmp_path / "services").resolve()), "the ABSOLUTE cwd, not the configured one"
    assert kwargs["sandbox"].value == "workspace-write"


@pytest.mark.asyncio
async def test_a_known_chat_resumes_its_thread(tmp_path, fake_codex, mem_store):
    from bos.extensions.runtimes._shared import commit_external_turn

    await commit_external_turn(mem_store, "chat-1", turn_id="t1", user_content="a", response="b",
                               runtime="codex", native_session_id="thread_abc")
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    thread, started = await agent._thread_for("chat-1")

    assert started is False
    assert thread.id == "thread_abc"
    assert fake_codex.instances[0].thread_start_calls == [], "must resume, not start"


@pytest.mark.asyncio
async def test_an_unresumable_thread_is_reported_not_silently_replaced(tmp_path, fake_codex, mem_store):
    """Review Focus 1 — BEP 19 §3.6: BOS does not silently start a fresh session."""
    from openai_codex import CodexError

    from bos.extensions.runtimes._shared import commit_external_turn

    await commit_external_turn(mem_store, "chat-1", turn_id="t1", user_content="a", response="b",
                               runtime="codex", native_session_id="thread_gone")
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    fake_codex.arm(resume_error=CodexError("thread_gone: no such thread"))

    with pytest.raises(RuntimeError) as excinfo:
        await agent._thread_for("chat-1")
    message = str(excinfo.value)
    assert "thread_gone" in message and "codex" in message
    assert fake_codex.instances[0].thread_start_calls == [], "no silent replacement"
