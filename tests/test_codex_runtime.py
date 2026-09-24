"""BEP 19 Layer 4a: the Codex runtime.

Stage 1 of 4 over CodexAgent's turn path: construction, config resolution and
lifecycle only. Task 4 adds client/thread handling, Task 5 makes a turn
actually run, Tasks 6-8 add streaming, interrupt/timeout and the approval
handler — ask()/run() raising NotImplementedError here is that staging, not a
gap that this file's tests are meant to cover yet.
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
