"""BEP 19 Layer 4a: the Codex runtime.

Stage 4 of 4 over CodexAgent's turn path: construction, config resolution and
lifecycle (Task 3), the client and thread lifecycle (Task 4), a turn that
runs and persists itself (Task 5), that turn streamed as TurnEvents (Task 6),
interrupt, cooperative stop, timeout, per-chat concurrency and a
bounded-wait aclose() (Task 7), and here — the approval handler that refuses
every escalation past the sandbox (Task 8).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from datetime import datetime
from typing import Any

import pytest
from conftest import HANG
from openai_codex import ImageInput, LocalImageInput, MentionInput, TextInput, TurnResult
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CommandExecutionStatus,
    CommandExecutionThreadItem,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    ThreadItem,
    ThreadTokenUsage,
    TokenUsageBreakdown,
    Turn,
    TurnError,
    TurnStatus,
)
from openai_codex.models import (
    ItemCompletedNotification,
    ItemStartedNotification,
    Notification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    UnknownNotification,
)

from bos.core.agent import AbortTurn
from bos.extensions.chat_stores.in_memory import InMemChatStore
from bos.extensions.runtimes.codex import _LEGACY_REJECTION


def _agent(tmp_path, fake_codex, **cfg: Any):
    """Build a CodexAgent with sane defaults: permission="read-only" and no cwd
    override unless the caller passes one. Used by every later task.

    `chat_store` and `structured_validator` are popped out of **cfg and passed
    straight to the constructor rather than validated as agent config;
    everything else in **cfg becomes the config dict. `fake_codex` is a
    required parameter (not read here) so every caller is guaranteed the
    `_CODEX_FACTORY` patch is live before the agent goes on to build a client
    from it.

    `structured_validator` defaults to the real, jsonschema-backed
    `JsonSchemaValidator` — the same class `create_agent` injects via
    `_default_structured_validator()` — not a fake, so schema tests exercise
    real BEP 12 validation rather than a stand-in that could silently drift
    from what production actually enforces.
    """
    from bos.core.defaults.structured_validator import JsonSchemaValidator
    from bos.extensions.runtimes.codex import CodexAgent

    chat_store = cfg.pop("chat_store", None)
    structured_validator = cfg.pop("structured_validator", None) or JsonSchemaValidator()
    cfg.setdefault("permission", "read-only")
    return CodexAgent(
        kind="george",
        cfg=cfg,
        chat_store=chat_store,
        workspace=tmp_path,
        mcp=lambda: None,
        structured_validator=structured_validator,
    )


def _arm_result(
    fake_codex,
    *,
    final_response: str | None,
    status: TurnStatus = TurnStatus.completed,
    error_message: str | None = None,
    usage_input: int | None = None,
    native_turn_id: str = "native-turn-1",
) -> TurnResult:
    """Build a REAL openai_codex TurnResult (Task 5) and arm it as the next
    turn's result — not a hand-rolled look-alike: every field below is the
    vendor's own TurnResult/TurnError/ThreadTokenUsage/TokenUsageBreakdown, so
    a shape the vendor renames or retypes breaks this helper, and every test
    using it, instead of silently drifting.

    `CodexAgent` builds its `AsyncCodex` lazily and caches it for the agent's
    whole lifetime (Task 4's `_ensure_client`), so a *second* turn in the same
    test reuses the client the *first* turn already built. Arm that existing
    instance directly; only when none exists yet (the first turn of a test)
    is there nothing to reach, so stage the value the same way `_Registry.arm`
    does, for the registry to apply when it builds the client.
    """
    usage = None
    if usage_input is not None:
        breakdown = TokenUsageBreakdown(
            cached_input_tokens=0,
            input_tokens=usage_input,
            output_tokens=0,
            reasoning_output_tokens=0,
            total_tokens=usage_input,
        )
        usage = ThreadTokenUsage(last=breakdown, total=breakdown)
    result = TurnResult(
        id=native_turn_id,
        status=status,
        error=TurnError(message=error_message) if error_message is not None else None,
        started_at=0,
        completed_at=1,
        duration_ms=1,
        final_response=final_response,
        items=[],
        usage=usage,
    )
    if fake_codex.instances:
        fake_codex.instances[-1].next_result = result
    else:
        fake_codex.arm(next_result=result)
    return result


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
        CodexAgent(
            kind="george", cfg={}, chat_store=None, workspace=tmp_path, mcp=lambda: None, structured_validator=None
        )
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
    assert fake_codex.instances[0].closed is True, "a client that fails preflight is closed, not leaked"


@pytest.mark.asyncio
async def test_a_new_chat_starts_a_thread_with_the_resolved_config(tmp_path, fake_codex, mem_store):
    agent = _agent(tmp_path, fake_codex, permission="workspace-write", cwd="services")
    thread, started = await agent._thread_for("chat-1", turn_id="t1")

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
    thread, started = await agent._thread_for("chat-1", turn_id="t1")

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
        await agent._thread_for("chat-1", turn_id="t1")
    message = str(excinfo.value)
    assert "thread_gone" in message and "codex" in message
    assert fake_codex.instances[0].thread_start_calls == [], "no silent replacement"


# ── Task 5: one turn — run() and ask() (BEP 19 §3.7, §3.9) ──────────────────


@pytest.mark.asyncio
async def test_a_turn_returns_the_final_response_and_commits_two_messages(tmp_path, fake_codex, mem_store):
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="done", usage_input=11)

    result = await agent.run("chat-1", "do it", turn_id="t1")

    assert result.output == "done"
    assert result.structured is False, "no schema was requested"
    assert result.iterations == 1
    assert result.usage  # non-empty
    assert result.finish_reason == "completed", "finish_reason carries TurnStatus.value verbatim"
    messages = await mem_store.get_messages("chat-1")
    assert [m.llm_message["role"] for m in messages] == ["user", "assistant"]
    assert messages[1].metadata["external_runtime"] == "codex"
    assert messages[1].metadata["native_session_id"] == "thread-1"
    assert messages[1].metadata["native_turn_id"] == "native-turn-1"


@pytest.mark.asyncio
async def test_a_failed_turn_raises_rather_than_returning_none(tmp_path, fake_codex, mem_store):
    """Review Focus 2: TurnResult.final_response is None on a failed turn."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response=None, status=TurnStatus.failed, error_message="model exploded")

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1")
    message = str(excinfo.value)
    assert "model exploded" in message
    # Fix round 2, Finding 2: the real SDK raises this *before* constructing a
    # TurnResult (openai_codex._run._raise_for_failed_turn), so the bare
    # vendor message alone would reach the caller with nothing to attribute it
    # to in a multi-chat process. codex.py wraps it with exactly this context.
    assert "codex" in message and "george" in message and "chat-1" in message and "t1" in message
    assert await mem_store.get_messages("chat-1") == [], "a failed turn commits nothing"


@pytest.mark.asyncio
async def test_a_failed_turn_with_no_error_detail_still_raises_with_bos_context(tmp_path, fake_codex, mem_store):
    """Mirrors openai_codex._run._raise_for_failed_turn's *other* branch: no
    `error`, or an `error` with a blank `message`, falls back to a generic
    "turn failed with status ..." rather than KeyError-ing or going silent.
    FakeTurnHandle.run() (fix round 2, Finding 2) reproduces this exactly."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response=None, status=TurnStatus.failed)  # no error_message

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1")
    message = str(excinfo.value)
    assert "turn failed with status" in message and "failed" in message
    assert "codex" in message and "george" in message and "chat-1" in message and "t1" in message
    assert await mem_store.get_messages("chat-1") == []


@pytest.mark.asyncio
async def test_a_completed_turn_with_no_text_is_an_empty_string_not_none(tmp_path, fake_codex, mem_store):
    """Review Focus 2, the other half: completed but silent must not hand a host None."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response=None, status=TurnStatus.completed)

    result = await agent.run("chat-1", "do it", turn_id="t1")
    assert result.output == ""


@pytest.mark.asyncio
async def test_run_generates_a_turn_id_when_none_is_given(tmp_path, fake_codex, mem_store):
    """Every other test in this file passes turn_id explicitly; this is the
    only one exercising the `turn_id or uuid.uuid4().hex` fallback itself."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="ok")

    result = await agent.run("chat-1", "do it")

    assert result.turn_id, "a turn_id must be generated, not left blank"
    messages = await mem_store.get_messages("chat-1")
    assert messages[0].turn_id == result.turn_id


@pytest.mark.asyncio
async def test_an_interrupted_turn_raises_and_commits_nothing(tmp_path, fake_codex, mem_store):
    """The failure mode the brief leaves open: an interrupted turn gets the
    same treatment as a failed one. Its `final_response` is a snapshot of a
    turn deliberately cut off before the model was done — not real turn
    history — so BEP 19 §7 criterion 20 ("timeout_seconds expiry ... raises")
    applies here too: raise, commit nothing. Pinned as its own test (distinct
    from the failed-turn test above) so a future change that special-cases
    `interrupted` into a silently-committed partial answer does not slip in
    unnoticed — Task 7 builds cancellation on top of this contract.

    Fix round 2, Finding 3: Task 7 will also reach `interrupted` from a
    cooperative `request_stop()`, where BOS's own contract (`Agent.run`) is to
    persist a handoff and return, not raise and discard the answer — the
    opposite of what this method does today. `turn_result` is attached to the
    exception precisely so that reopened decision has the full `TurnResult` to
    work with instead of a bare message; asserted here (on its meaningful
    fields, not by identity — see below) so a future edit that drops the
    attribute (leaving only the string) is caught.

    Task 6: `run()` now reaches this through `_emit_stream`, which *rebuilds*
    a `TurnResult` from the notification stream rather than handing back the
    vendor's own object unchanged (that is the point of streaming: BOS never
    gets one ready-made result to just pass through). So `turn_result` is no
    longer the exact object `_arm_result` returned — asserting identity would
    fail for a reason that has nothing to do with this test's actual contract.
    What must survive is the *content*: the status and the answer snapshot the
    turn was cut off with."""
    from bos.extensions.runtimes.codex import TurnNotCompletedError

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="partial", status=TurnStatus.interrupted)

    with pytest.raises(TurnNotCompletedError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1")
    assert "interrupted" in str(excinfo.value)
    assert excinfo.value.turn_result.status is TurnStatus.interrupted
    assert excinfo.value.turn_result.final_response == "partial"
    assert await mem_store.get_messages("chat-1") == [], "an interrupted turn commits nothing"


_OK_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


@pytest.mark.asyncio
async def test_schema_is_forwarded_and_the_validated_object_is_returned(tmp_path, fake_codex, mem_store):
    """Fix round 1: CodexAgent now receives the same injected StructuredValidator
    create_agent gives every Agent (BEP 19 §3.2 gained a sixth constructor
    kwarg for exactly this). `_agent()` wires a real JsonSchemaValidator, so
    this is real jsonschema validation, not a stand-in."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response='{"ok": true}')

    result = await agent.run("chat-1", "do it", turn_id="t1", schema=_OK_SCHEMA)

    assert result.structured is True
    assert result.output == {"ok": True}
    _thread_id, _input, kwargs = fake_codex.instances[0].turn_calls[0]
    assert kwargs["output_schema"] == _OK_SCHEMA, "the schema is also forwarded as a provider hint"
    messages = await mem_store.get_messages("chat-1")
    assert messages[1].llm_message["content"] == '{"ok": true}', "the raw text is stored, not the parsed object"


@pytest.mark.asyncio
async def test_schema_validation_failure_retries_with_a_correction_message_then_succeeds(
    tmp_path, fake_codex, mem_store
):
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await agent._ensure_client()  # build the client now so both queued results serve ONE run() call
    bad = _arm_result(fake_codex, final_response="not json at all")
    good = _arm_result(fake_codex, final_response='{"ok": true}')
    fake_codex.instances[0].next_results = [bad, good]

    result = await agent.run("chat-1", "do it", turn_id="t1", schema=_OK_SCHEMA, max_schema_retries=1)

    assert result.structured is True
    assert result.output == {"ok": True}
    calls = fake_codex.instances[0].turn_calls
    assert len(calls) == 2, "one initial attempt plus one retry"
    _thread_id, correction_input, _kwargs = calls[1]
    assert "schema validation" in correction_input and "not json at all" not in correction_input


@pytest.mark.asyncio
async def test_schema_validation_exhausting_retries_raises_and_commits_nothing(tmp_path, fake_codex, mem_store):
    from bos.core.agent import StructuredOutputError

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await agent._ensure_client()
    first = _arm_result(fake_codex, final_response="not json at all")
    second = _arm_result(fake_codex, final_response="still not json")
    fake_codex.instances[0].next_results = [first, second]

    with pytest.raises(StructuredOutputError):
        await agent.run("chat-1", "do it", turn_id="t1", schema=_OK_SCHEMA, max_schema_retries=1)

    assert len(fake_codex.instances[0].turn_calls) == 2, "exactly the initial attempt plus the one allowed retry"
    assert await mem_store.get_messages("chat-1") == [], "an unvalidated reply is not turn history"


@pytest.mark.asyncio
async def test_schema_validation_rejects_valid_json_of_the_wrong_type(tmp_path, fake_codex, mem_store):
    """Fix round 2, Finding 1: every other schema case here used unparseable
    text ("not json at all"), so a parse-only stand-in (json.loads with no
    jsonschema check) would pass every one of them — proving parsing works,
    not validation. '{"ok": "yes"}' is valid JSON that a bare json.loads
    accepts outright, but "ok" must be a boolean per _OK_SCHEMA, so real
    jsonschema validation must reject it. Confirmed by mutation: degrading the
    injected validator to parse_json makes this test fail with
    "DID NOT RAISE" while the rest of the suite stays green."""
    from bos.core.agent import StructuredOutputError

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await agent._ensure_client()
    first = _arm_result(fake_codex, final_response='{"ok": "yes"}')
    second = _arm_result(fake_codex, final_response='{"ok": "yes"}')
    fake_codex.instances[0].next_results = [first, second]

    with pytest.raises(StructuredOutputError):
        await agent.run("chat-1", "do it", turn_id="t1", schema=_OK_SCHEMA, max_schema_retries=1)

    assert len(fake_codex.instances[0].turn_calls) == 2, "exactly the initial attempt plus the one allowed retry"
    assert await mem_store.get_messages("chat-1") == []


@pytest.mark.asyncio
async def test_a_turn_without_a_chat_store_still_returns_a_result(tmp_path, fake_codex):
    """CodexAgent may be built with chat_store=None (ExternalRuntime allows it);
    run() still completes the turn and returns a normal AgentResult instead of
    crashing on the missing store when it reaches the commit step."""
    agent = _agent(tmp_path, fake_codex)  # chat_store defaults to None
    _arm_result(fake_codex, final_response="hi")

    result = await agent.run("chat-1", "do it", turn_id="t1")

    assert result.output == "hi"


@pytest.mark.asyncio
async def test_commit_observer_is_called_with_the_commit(tmp_path, fake_codex, mem_store):
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="ok")
    seen = []

    await agent.run("chat-1", "do it", turn_id="t1", commit_observer=seen.append)

    assert len(seen) == 1
    assert seen[0].chat_id == "chat-1"


@pytest.mark.asyncio
async def test_an_async_commit_observer_is_awaited(tmp_path, fake_codex, mem_store):
    """Agent.run's own commit_observer supports a sync OR an async callable
    (`if inspect.isawaitable(result): await result`); CodexAgent mirrors that
    exactly. The sync test above never reaches the `await observed` line, so
    it needs this test of its own."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="ok")
    seen = []

    async def observe(commit):
        seen.append(commit)

    await agent.run("chat-1", "do it", turn_id="t1", commit_observer=observe)

    assert len(seen) == 1
    assert seen[0].chat_id == "chat-1"


@pytest.mark.asyncio
async def test_ask_delegates_to_run_and_returns_the_text(tmp_path, fake_codex, mem_store):
    """None of the other tests here ever call ask() itself (only inspect its
    signature, in test_ask_and_run_accept_every_agent_port_keyword below) —
    this exercises the actual delegation body."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="hi")

    output = await agent.ask("chat-1", "do it", turn_id="t1")

    assert output == "hi"
    assert isinstance(output, str)


@pytest.mark.asyncio
async def test_llm_args_model_and_reasoning_effort_reach_the_turn_call(tmp_path, fake_codex, mem_store):
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="ok")

    await agent.run(
        "chat-1", "do it", turn_id="t1", llm_args={"model": "gpt-5.1-codex", "reasoning_effort": "high"}
    )

    _thread_id, _input, kwargs = fake_codex.instances[0].turn_calls[0]
    assert kwargs["model"] == "gpt-5.1-codex"
    assert kwargs["effort"] == "high"


@pytest.mark.asyncio
async def test_ask_and_run_accept_every_agent_port_keyword(tmp_path, fake_codex, mem_store):
    """AgentActor and _HarnessAgentRunner pass these by NAME; a rename is a TypeError
    that runtime_checkable isinstance cannot catch."""
    import inspect as inspect_mod

    from bos.extensions.runtimes.codex import CodexAgent

    for method, expected in (
        (CodexAgent.ask, {"chat_id", "content", "interrupt", "ctx_metadata", "llm_args",
                          "event_sink", "turn_id", "commit_observer"}),
        (CodexAgent.run, {"chat_id", "content", "interrupt", "ctx_metadata", "llm_args",
                          "event_sink", "turn_id", "commit_observer", "schema", "max_schema_retries"}),
    ):
        names = set(inspect_mod.signature(method).parameters) - {"self"}
        assert expected <= names, f"{method.__name__} is missing {expected - names}"


@pytest.mark.asyncio
async def test_a_second_turn_resumes_and_rewrites_the_session_id(tmp_path, fake_codex, mem_store):
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="one")
    await agent.run("chat-1", "a", turn_id="t1")
    _arm_result(fake_codex, final_response="two")
    await agent.run("chat-1", "b", turn_id="t2")

    assert fake_codex.instances[0].thread_resume_calls, "the second turn resumed"
    messages = await mem_store.get_messages("chat-1")
    assert messages[-1].metadata["native_session_id"] == "thread-1"
    assert messages[-1].llm_message["content"] == "two", "the second _arm_result must reach the built client"


# ── Task 5: content conversion (BEP 19 §3.9) ────────────────────────────────


def test_content_conversion_passes_a_plain_string_through_unchanged():
    from bos.extensions.runtimes.codex import _content_to_codex_input

    assert _content_to_codex_input("do it") == "do it"


@pytest.mark.parametrize(
    ("part", "expected_type", "expected_attrs"),
    [
        ({"type": "text", "text": "hi"}, TextInput, {"text": "hi"}),
        (
            {"type": "image", "source": {"kind": "url", "value": "https://x/y.png"}},
            ImageInput,
            {"url": "https://x/y.png"},
        ),
        (
            {"type": "image", "source": {"kind": "path", "value": "/tmp/y.png"}},
            LocalImageInput,
            {"path": "/tmp/y.png"},
        ),
        (
            {"type": "file", "mime_type": "text/plain", "source": {"kind": "path", "value": "/tmp/a/b.txt"}},
            MentionInput,
            {"name": "b.txt", "path": "/tmp/a/b.txt"},
        ),
    ],
)
def test_content_conversion_maps_each_bos_part_to_its_codex_input_type(part, expected_type, expected_attrs):
    from bos.extensions.runtimes.codex import _content_to_codex_input

    [item] = _content_to_codex_input([part])

    assert isinstance(item, expected_type)
    for attr, value in expected_attrs.items():
        assert getattr(item, attr) == value


def test_content_conversion_rejects_a_url_sourced_file_part():
    """Codex's MentionInput has no wire form for a remote file — BEP 19 §3.9
    defines FilePart -> MentionInput(name, path), a local-only mechanism."""
    from bos.extensions.runtimes.codex import _content_to_codex_input

    with pytest.raises(ValueError, match="local path"):
        _content_to_codex_input(
            [{"type": "file", "mime_type": "text/plain", "source": {"kind": "url", "value": "https://x/a.txt"}}]
        )


@pytest.mark.asyncio
async def test_a_multipart_turn_reaches_codex_as_converted_input_items(tmp_path, fake_codex, mem_store):
    """Wiring check: run() actually calls the converter, not just that the
    converter works in isolation above."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_result(fake_codex, final_response="ok")
    content = [{"type": "text", "text": "look at this"}, {"type": "image", "source": {"kind": "url", "value": "u"}}]

    await agent.run("chat-1", content, turn_id="t1")

    _thread_id, sent_input, _kwargs = fake_codex.instances[0].turn_calls[0]
    assert [type(item) for item in sent_input] == [TextInput, ImageInput]
    assert sent_input[0].text == "look at this"
    assert sent_input[1].url == "u"


# ── Task 6: streaming notifications to TurnEvent (BEP 19 §3.9) ─────────────
#
# Unlike Task 5's tests above, these arm a real Notification sequence directly
# (`_arm_notifications`) instead of a single TurnResult: the whole point of
# this task is what CodexAgent does with each notification as it streams by,
# which a canned final result can't exercise. None of these need a chat_store
# — they assert on the returned AgentResult and the sink's events, never on
# committed messages (Task 5's tests already cover persistence).
#
# Every notification below is stamped turn_id="turn-1" because none of these
# tests arm a TurnResult (_arm_result): with no result to take a native id
# from, FakeThread.turn() falls back to its own counter ("turn-{n}"), which
# is "turn-1" for the first turn of a fresh agent — see conftest.py.


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class RaisingSink:
    """Fails on every emit. `events` still records each attempt (appended
    before the raise), so a test can prove the turn keeps emitting past a
    failure instead of quietly giving up after the first one."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)
        raise RuntimeError("sink exploded")


def _arm_notifications(fake_codex, notifications: list[Any]) -> None:
    """Mirrors `_arm_result`'s staging: CodexAgent builds its client lazily, so
    arming has to reach whichever FakeAsyncCodex instance exists — or will
    exist — by the time `.turn()` is called."""
    if fake_codex.instances:
        fake_codex.instances[-1].next_notifications = notifications
    else:
        fake_codex.arm(next_notifications=notifications)


def _command_item(item_id: str, command: str, *, status: CommandExecutionStatus) -> ThreadItem:
    return ThreadItem(
        CommandExecutionThreadItem(
            id=item_id, command=command, command_actions=[], cwd="/tmp", status=status, type="commandExecution"
        )
    )


def _mcp_item(item_id: str, tool: str, server: str, *, status: McpToolCallStatus) -> ThreadItem:
    return ThreadItem(
        McpToolCallThreadItem(id=item_id, arguments={}, server=server, status=status, tool=tool, type="mcpToolCall")
    )


def _agent_message_item(item_id: str, text: str, *, phase: MessagePhase | None = MessagePhase.final_answer):
    return ThreadItem(AgentMessageThreadItem(id=item_id, text=text, phase=phase, type="agentMessage"))


def _item_started(item: ThreadItem, *, turn_id: str = "turn-1") -> Notification:
    return Notification(
        method="item/started",
        payload=ItemStartedNotification(item=item, started_at_ms=0, thread_id="thread-1", turn_id=turn_id),
    )


def _item_completed(item: ThreadItem, *, turn_id: str = "turn-1") -> Notification:
    return Notification(
        method="item/completed",
        payload=ItemCompletedNotification(item=item, completed_at_ms=1, thread_id="thread-1", turn_id=turn_id),
    )


def _turn_completed(turn_id: str = "turn-1", *, status: TurnStatus = TurnStatus.completed) -> Notification:
    return Notification(
        method="turn/completed",
        payload=TurnCompletedNotification(thread_id="thread-1", turn=Turn(id=turn_id, items=[], status=status)),
    )


@pytest.mark.asyncio
async def test_stream_emits_tool_and_response_events_in_order(tmp_path, fake_codex):
    """The brief's own sequence: a command execution started and completed, an
    agent message, a turn completed — asserted in order, by type/phase/name."""
    agent = _agent(tmp_path, fake_codex)
    running = _command_item("cmd-1", "ls -la", status=CommandExecutionStatus.in_progress)
    finished = _command_item("cmd-1", "ls -la", status=CommandExecutionStatus.completed)
    _arm_notifications(
        fake_codex,
        [
            _item_started(running),
            _item_completed(finished),
            _item_completed(_agent_message_item("msg-1", "done")),
            _turn_completed(),
        ],
    )
    sink = CaptureSink()

    result = await agent.run("chat-1", "do it", turn_id="t1", event_sink=sink)

    assert result.output == "done"
    assert [(e.event_type, e.phase) for e in sink.events] == [
        ("tool", "start"),
        ("tool", "finish"),
        ("response", "finish"),
        ("turn", "finish"),
    ]
    assert sink.events[0].tool_name == "ls -la"
    assert sink.events[1].tool_name == "ls -la"
    assert sink.events[2].content == "done"
    for event in sink.events:
        assert event.chat_id == "chat-1"
        assert event.turn_id == "t1", "the BOS turn_id, not the native one"
        assert event.agent_name == "george"


@pytest.mark.asyncio
async def test_stream_maps_an_mcp_tool_call_to_a_tool_event(tmp_path, fake_codex):
    """McpToolCallThreadItem is the mapping's other tool-bearing variant —
    untested by the command-execution sequence above."""
    agent = _agent(tmp_path, fake_codex)
    _arm_notifications(
        fake_codex,
        [
            _item_started(_mcp_item("mcp-1", "search", "web", status=McpToolCallStatus.in_progress)),
            _item_completed(_mcp_item("mcp-1", "search", "web", status=McpToolCallStatus.completed)),
            _turn_completed(),
        ],
    )
    sink = CaptureSink()

    await agent.run("chat-1", "do it", turn_id="t1", event_sink=sink)

    tool_events = [e for e in sink.events if e.event_type == "tool"]
    assert [e.phase for e in tool_events] == ["start", "finish"]
    assert all(e.tool_name == "search" for e in tool_events)


@pytest.mark.asyncio
async def test_stream_skips_item_types_with_no_tool_mapping(tmp_path, fake_codex):
    """A ThreadItem variant that is neither a command execution, an MCP tool
    call, nor an agent message (e.g. ReasoningThreadItem) has no BOS `tool`
    vocabulary: skipped at both item/started and item/completed, not raised
    and not force-fit into a tool event."""
    from openai_codex.generated.v2_all import ReasoningThreadItem

    agent = _agent(tmp_path, fake_codex)
    reasoning = ThreadItem(ReasoningThreadItem(id="r1", type="reasoning"))
    _arm_notifications(fake_codex, [_item_started(reasoning), _item_completed(reasoning), _turn_completed()])
    sink = CaptureSink()

    await agent.run("chat-1", "do it", turn_id="t1", event_sink=sink)

    assert [e.event_type for e in sink.events] == ["turn"]


@pytest.mark.asyncio
async def test_stream_skips_an_unrecognized_notification_method(tmp_path, fake_codex):
    """The vendor adds notification kinds between releases; UnknownNotification
    is the real shape a client falls back to for one this SDK doesn't parse.
    Must not raise, and must not stop the turn from completing normally."""
    agent = _agent(tmp_path, fake_codex)
    unknown = Notification(method="future/thing", payload=UnknownNotification(params={"whatever": True}))
    _arm_notifications(fake_codex, [unknown, _turn_completed()])
    sink = CaptureSink()

    result = await agent.run("chat-1", "do it", turn_id="t1", event_sink=sink)

    assert [e.event_type for e in sink.events] == ["turn"]
    assert result.finish_reason == "completed"


@pytest.mark.asyncio
async def test_stream_ignores_notifications_for_a_different_turn(tmp_path, fake_codex):
    """_collect_async_turn_result filters every accumulated notification by
    turn_id even though the real subscription is already scoped to one turn;
    mirrored here as the same defensive backstop, for all three accumulated
    kinds (item, usage, turn/completed).

    Every foreign notification below is placed *after* its genuine
    counterpart, specifically so that if a turn_id guard were ever deleted,
    the *foreign* value would be the one left standing: the reverse scan for
    final_response finds the last-appended item first, and a later write
    would otherwise overwrite usage/completed. A real stream() could never
    actually deliver anything after its own turn's completion — this ordering
    tests BOS's accumulation guards in isolation from that upstream guarantee,
    the same way _collect_async_turn_result's own filters apply unconditionally
    regardless of what the subscription is expected to already scope out."""
    agent = _agent(tmp_path, fake_codex)
    foreign_usage = ThreadTokenUsage(
        last=TokenUsageBreakdown(
            cached_input_tokens=0, input_tokens=1, output_tokens=0, reasoning_output_tokens=0, total_tokens=1
        ),
        total=TokenUsageBreakdown(
            cached_input_tokens=0, input_tokens=1, output_tokens=0, reasoning_output_tokens=0, total_tokens=1
        ),
    )
    _arm_notifications(
        fake_codex,
        [
            _item_completed(_agent_message_item("real-msg", "the real answer")),
            _turn_completed(),
            _item_completed(_agent_message_item("foreign-msg", "should not win"), turn_id="turn-999"),
            Notification(
                method="thread/tokenUsage/updated",
                payload=ThreadTokenUsageUpdatedNotification(
                    thread_id="thread-1", token_usage=foreign_usage, turn_id="turn-999"
                ),
            ),
            # A different status so a leaked overwrite of `completed` is
            # observable through finish_reason, not just silently harmless.
            _turn_completed("turn-999", status=TurnStatus.interrupted),
        ],
    )

    result = await agent.run("chat-1", "do it", turn_id="t1")

    assert result.output == "the real answer", "a foreign item must not win the reverse scan for the final answer"
    assert result.usage == {}, "the foreign-turn usage notification must not be accumulated"
    assert result.finish_reason == "completed", "a foreign turn/completed must not overwrite this turn's own"
    assert result.usage == {}, "the foreign-turn usage notification must not be accumulated"


@pytest.mark.asyncio
async def test_stream_with_no_sink_still_streams_and_returns_a_result(tmp_path, fake_codex):
    """sink may be None — the common case for _HarnessAgentRunner — and a turn
    with none must still stream, collect and return its result."""
    agent = _agent(tmp_path, fake_codex)
    _arm_notifications(fake_codex, [_item_completed(_agent_message_item("msg-1", "done")), _turn_completed()])

    result = await agent.run("chat-1", "do it", turn_id="t1")  # event_sink omitted -> None

    assert result.output == "done"


@pytest.mark.asyncio
async def test_stream_sink_that_raises_does_not_break_the_turn(tmp_path, fake_codex):
    """Emitting is best-effort: a sink that raises must not kill the turn, and
    must not stop later notifications from being attempted either — asserted
    via RaisingSink's own event count, not just that run() didn't raise."""
    agent = _agent(tmp_path, fake_codex)
    _arm_notifications(
        fake_codex,
        [
            _item_started(_command_item("cmd-1", "ls", status=CommandExecutionStatus.in_progress)),
            _item_completed(_command_item("cmd-1", "ls", status=CommandExecutionStatus.completed)),
            _item_completed(_agent_message_item("msg-1", "done")),
            _turn_completed(),
        ],
    )
    sink = RaisingSink()

    result = await agent.run("chat-1", "do it", turn_id="t1", event_sink=sink)

    assert result.output == "done"
    assert len(sink.events) == 4, "every notification's event was attempted despite each emit raising"


@pytest.mark.asyncio
async def test_stream_raises_if_the_stream_ends_without_a_turn_completed(tmp_path, fake_codex):
    """Mirrors _collect_async_turn_result's own guard: `completed is None`
    after the stream ends is a RuntimeError, not a silently empty result."""
    agent = _agent(tmp_path, fake_codex)
    _arm_notifications(fake_codex, [_item_completed(_agent_message_item("msg-1", "partial"))])

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1")
    message = str(excinfo.value)
    assert "turn completed event not received" in message
    assert "codex" in message and "george" in message


@pytest.mark.asyncio
async def test_final_response_falls_back_to_an_unphased_message_and_skips_commentary(tmp_path, fake_codex):
    """Mirrors _final_assistant_response_from_items exactly: with no
    final_answer-phase item, the last *unphased* one wins — but a
    commentary-phase item is neither the answer nor a fallback candidate, so
    it must be skipped rather than mistakenly picked up as one."""
    agent = _agent(tmp_path, fake_codex)
    _arm_notifications(
        fake_codex,
        [
            _item_completed(_agent_message_item("c1", "commentary text", phase=MessagePhase.commentary)),
            _item_completed(_agent_message_item("u1", "unphased text", phase=None)),
            _turn_completed(),
        ],
    )

    result = await agent.run("chat-1", "do it", turn_id="t1")

    assert result.output == "unphased text"


@pytest.mark.asyncio
async def test_final_response_skips_non_agent_message_items_while_scanning_backward(tmp_path, fake_codex):
    """_final_assistant_response_from_items scans `items` in reverse looking
    for an AgentMessageThreadItem. A different item type appended *after* the
    real answer (so it is visited *first* in the reverse scan) must be
    stepped over via `continue`, not mistaken for one — a
    CommandExecutionThreadItem has no `.phase` at all, so deleting that
    isinstance guard turns this into an AttributeError instead of a skip."""
    agent = _agent(tmp_path, fake_codex)
    _arm_notifications(
        fake_codex,
        [
            _item_completed(_agent_message_item("msg-1", "the answer")),
            _item_completed(_command_item("cmd-1", "ls", status=CommandExecutionStatus.completed)),
            _turn_completed(),
        ],
    )

    result = await agent.run("chat-1", "do it", turn_id="t1")

    assert result.output == "the answer"


@pytest.mark.asyncio
async def test_a_turn_with_nothing_armed_raises_rather_than_answering_silently(tmp_path, fake_codex):
    """Guards the fake itself: FakeThread.turn() falls back to synthesizing
    notifications from an armed TurnResult when nothing was explicitly armed
    via _arm_notifications, but if a test forgets to arm *anything* (no
    _arm_result either) there is nothing to synthesize from. That must
    surface as the same loud failure a real turn that never completes would
    — not a silent, bogus empty answer that would mask the mistake."""
    agent = _agent(tmp_path, fake_codex)

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1")
    assert "turn completed event not received" in str(excinfo.value)


# ── Task 7: interrupt, cooperative stop, timeout, concurrency (BEP 19 §3.10) ─


async def _poll_until(predicate, *, timeout: float = 2.0) -> None:
    """Wait for a synchronous predicate to go true, polling every event-loop
    tick rather than guessing how many turns the code under test needs."""

    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout=timeout)


async def _hanging_handle(fake_codex, *, expected_count: int = 1, timeout: float = 2.0):
    """Wait for the `expected_count`-th turn handle (in creation order) to
    exist and reach its armed HANG, then return it.

    Indexed by count rather than always grabbing turn_handles[-1] the moment
    the list is non-empty: once a test has two turns in flight, a handle
    already sitting in the list from an earlier, still-hanging turn must not
    be mistaken for the new one that hasn't been created yet.
    """

    def _handles() -> list:
        return fake_codex.instances[-1].turn_handles if fake_codex.instances else []

    await _poll_until(lambda: len(_handles()) >= expected_count, timeout=timeout)
    handle = _handles()[expected_count - 1]
    await asyncio.wait_for(handle.hang_reached.wait(), timeout=timeout)
    return handle


def _arm_handle(fake_codex, **attrs: Any) -> None:
    """Mirrors `_arm_notifications` for the FakeTurnHandle knobs (fix round 2):
    a handle is created inside production code's own `await thread.turn(...)`,
    so a slow or failing vendor RPC has to be armed before that call, not
    after — there is no seam in between."""
    if fake_codex.instances:
        fake_codex.instances[-1].next_handle_attrs.update(attrs)
    else:
        fake_codex.arm(next_handle_attrs=dict(attrs))


@pytest.mark.asyncio
async def test_interrupt_callback_steering_message_reaches_handle_steer(tmp_path, fake_codex, mem_store):
    """Fix round 1 (BEP 19 §3.9's `interrupt` row, corrected): AgentActor's
    poll-style callback is polled once per streamed notification — exactly as
    Agent._interrupt reads the same callback in agent.py — but a truthy
    return is a message to *deliver*, not a request to stop: Agent._interrupt
    merges it into the live context (`ctx.add_message(llm_message, merge=True)`),
    so the turn keeps going. Codex has no local context to merge into, so the
    equivalent is AsyncTurnHandle.steer() — the turn is not ended, interrupted,
    or even paused; it runs to its own normal completion."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(
        fake_codex,
        [
            _item_completed(_agent_message_item("msg-1", "working")),
            _item_completed(_agent_message_item("msg-2", "done")),
            _turn_completed(),  # status=completed — steering does not interrupt anything
        ],
    )
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        return {"role": "user", "content": "actually, do X instead"} if calls == 1 else None

    result = await agent.run("chat-1", "do it", turn_id="t1", interrupt=interrupt)

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert handle.interrupted is False, "steering is not interrupting"
    # The LLM-message dict's "content" (a BOS MessageContent, per
    # TurnContext.add_message's merge branch) converted through the same
    # _content_to_codex_input a new turn's own content goes through — a
    # plain string content passes through unchanged.
    assert handle.steered == ["actually, do X instead"]
    # Two, not three: polled on every notification *except* the terminal
    # turn/completed (fix round 2, I3 — see the dedicated test below).
    # Steering itself does not stop the poll.
    assert calls == 2, "polled on every non-terminal notification, steering does not stop the poll"
    assert result.output == "done"
    assert result.finish_reason == "completed"
    messages = await mem_store.get_messages("chat-1")
    assert messages[-1].llm_message["content"] == "done"


@pytest.mark.asyncio
async def test_interrupt_callback_falsy_return_changes_nothing(tmp_path, fake_codex, mem_store):
    """The other half of the callback contract: no steer, no interrupt, no
    state change at all — the turn cannot even tell the callback was there."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(
        fake_codex,
        [_item_completed(_agent_message_item("msg-1", "done")), _turn_completed()],
    )

    result = await agent.run("chat-1", "do it", turn_id="t1", interrupt=lambda: None)

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert handle.steered == []
    assert handle.interrupted is False
    assert result.output == "done"


@pytest.mark.asyncio
async def test_interrupt_callback_is_not_polled_after_the_terminal_notification(tmp_path, fake_codex, mem_store):
    """Fix round 2 (I3): polling is DESTRUCTIVE. AgentActor._make_interrupt
    pops the queued INTERRUPT_MESSAGE envelopes off the session as it reads
    them, so a poll is a take, not a peek. Polled on the terminal
    turn/completed the turn is already over: handle.steer()'s expectedTurnId
    no longer names an active turn, the vendor rejects it, and the user's
    mid-turn message is gone with nothing anywhere recording that it existed.

    Asserted on the callback's call count and on the pending message still
    being pending — not merely on steer() not being called, because the bug
    is the *consumption*, which happens before steer() is ever reached."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(
        fake_codex,
        [_item_completed(_agent_message_item("msg-1", "done")), _turn_completed()],
    )
    # Stands in for AgentActor's session.interrupts buffer: a follow-up that
    # arrives late — after the only non-terminal notification has gone by.
    pending = ["the user's follow-up"]
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        return {"role": "user", "content": pending.pop(0)} if calls == 2 else None

    result = await agent.run("chat-1", "do it", turn_id="t1", interrupt=interrupt)

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert calls == 1, "the terminal turn/completed must not be polled"
    # Left in AgentActor's buffer rather than consumed here. Not the same as
    # "delivered later": AgentActor clears session.interrupts when the next
    # plain MESSAGE arrives with no turn running (agent_actor.py:193), so a
    # follow-up that lands inside the terminal window is still lost — just not
    # lost *by this module*, which is all this test can speak to (N4).
    assert pending == ["the user's follow-up"], "not consumed by the terminal poll"
    assert handle.steered == []
    assert result.output == "done"


@pytest.mark.asyncio
async def test_interrupt_callback_abort_turn_returns_the_marker_and_commits_nothing(
    tmp_path, fake_codex, mem_store
):
    """Fix round 2 (I4): Agent._interrupt does not catch AbortTurn — `if
    interrupt and (llm_message := await _apply_async(interrupt, {})):` only
    inspects a *returned* value, so a raise skips that check and unwinds the
    turn — and _emit_stream's poll is the same. But `Agent.run` itself DOES
    catch it (agent.py:837-840): turn_status="aborted",
    ctx.final_content=ABORTED_TURN_CONTENT, and a normal AgentResult comes
    back. Round 1 made CodexAgent propagate instead, which turns the same
    signal into AgentActor's status="error" / "Turn failed: " with no text.
    Match Agent's caller-facing contract: catch, and return the marker.

    Nothing is committed, and that is not a contradiction: Agent persists the
    marker to shape the *model's* history, and CodexAgent never replays BOS
    history into Codex."""
    from bos.core.agent import ABORTED_TURN_CONTENT

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [_item_completed(_agent_message_item("msg-1", "unused"))])

    def interrupt():
        raise AbortTurn()

    result = await agent.run("chat-1", "do it", turn_id="t1", interrupt=interrupt)

    assert result.output == ABORTED_TURN_CONTENT
    assert result.finish_reason == "aborted"
    assert result.turn_id == "t1"
    assert await mem_store.get_messages("chat-1") == [], "an aborted turn commits nothing"
    # The `return` sits inside run()'s outer try, so the busy-guard entry is
    # still released by its `finally` — an abort must not wedge the chat.
    assert agent._in_flight == {}


@pytest.mark.asyncio
async def test_an_aborted_turn_also_tells_codex_to_stop(tmp_path, fake_codex, mem_store):
    """Fix round 3 (D): round 2 stopped propagating AbortTurn, but nothing
    told Codex. The exception unwound out of _emit_stream, run() caught it and
    returned a result — and the native turn kept running, spending tokens and
    writing to cwd, until request_stop(), aclose() or the next thread.turn().
    BEP 19 §3.10 says cancellation must not leave a child writing to the
    workspace, so _run_turn now interrupts on its way past."""
    from bos.core.agent import ABORTED_TURN_CONTENT

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [_item_completed(_agent_message_item("msg-1", "unused"))])

    def interrupt():
        raise AbortTurn()

    result = await agent.run("chat-1", "do it", turn_id="t1", interrupt=interrupt)

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert handle.interrupted is True, "the native turn is told to stop, not just abandoned"
    # Round 2's caller-facing contract is unchanged by the added interrupt.
    assert result.output == ABORTED_TURN_CONTENT
    assert result.finish_reason == "aborted"
    assert await mem_store.get_messages("chat-1") == []


@pytest.mark.asyncio
async def test_any_callback_exception_also_tells_codex_to_stop(tmp_path, fake_codex, mem_store):
    """The same hole swallowed every *other* exception the interrupt callback
    raises — `await _apply_async(interrupt, {})` is deliberately unwrapped, so
    anything it throws unwinds the same way AbortTurn does. Only AbortTurn is
    special to run(); the native turn has to be stopped either way."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [_item_completed(_agent_message_item("msg-1", "unused"))])

    def interrupt():
        raise ValueError("callback blew up")

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1", interrupt=interrupt)

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert handle.interrupted is True
    # Asserted as it actually is, not as assumed: a non-AbortTurn exception is
    # not special-cased, so run()'s generic wrap applies and the caller gets a
    # RuntimeError naming the turn, with the original kept as __cause__.
    assert "callback blew up" in str(excinfo.value)
    assert "t1" in str(excinfo.value) and "chat-1" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert await mem_store.get_messages("chat-1") == []


@pytest.mark.asyncio
async def test_an_aborted_turn_does_not_hang_on_a_wedged_interrupt(tmp_path, fake_codex, mem_store, monkeypatch):
    """The interrupt D adds is on the abort path, which is a path a caller is
    waiting on — so it gets the same bound as every other interrupt in this
    module rather than becoming a new way for a wedged child to hang run()."""
    import bos.extensions.runtimes.codex as codex_mod
    from bos.core.agent import ABORTED_TURN_CONTENT

    monkeypatch.setattr(codex_mod, "_INTERRUPT_GRACE_SECONDS", 0.05)
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [_item_completed(_agent_message_item("msg-1", "unused"))])
    _arm_handle(fake_codex, interrupt_hang=asyncio.Event())  # never set: the child never answers

    def interrupt():
        raise AbortTurn()

    started = time.perf_counter()
    result = await asyncio.wait_for(agent.run("chat-1", "do it", turn_id="t1", interrupt=interrupt), timeout=5)
    elapsed = time.perf_counter() - started

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert handle.interrupted is True
    assert elapsed < 1, f"the abort path is bounded by the interrupt grace: {elapsed:.2f}s"
    assert result.output == ABORTED_TURN_CONTENT


@pytest.mark.asyncio
async def test_a_failed_steer_request_is_logged_and_does_not_abort_the_turn(tmp_path, fake_codex, mem_store, caplog):
    """Best-effort, but not silent (fix round 2, I3). The turn survives — a
    network blip delivering the steering message must not be mistaken for a
    real turn failure while the stream is still perfectly capable of
    finishing — but unlike a failed handle.interrupt() (a courtesy BOS sends
    on its own behalf), what is lost here is a message a *user* typed, and
    the poll that produced it already drained it out of AgentActor's session.
    Swallowing it leaves no record anywhere that it existed, so it is logged
    at WARNING with the chat and turn id."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_handle(fake_codex, steer_error=RuntimeError("app-server hung up"))
    _arm_notifications(
        fake_codex,
        [_item_completed(_agent_message_item("msg-1", "still going")), _turn_completed()],
    )

    with caplog.at_level(logging.WARNING, logger="bos.extensions.runtimes.codex"):
        result = await agent.run(
            "chat-1", "do it", turn_id="t1", interrupt=lambda: {"role": "user", "content": "stop"}
        )

    assert result.output == "still going", "a failed steer is not a turn failure"
    # Filtered by logger name as well as level: caplog.at_level(..., logger=…)
    # sets the level on that logger but collects from the root handler, so an
    # unrelated WARNING from elsewhere in the session (the LLMConsolidator
    # registry line, whichever test first triggers registration) lands in
    # caplog.records too — which made this assertion order-dependent and made
    # it fail under CLAUDE.md's own single-test command (fix round 3, N2).
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "bos.extensions.runtimes.codex"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "chat-1" in message and "t1" in message and "dropped" in message
    assert warnings[0].exc_info is not None, "the underlying RPC failure is kept, not just summarized"


@pytest.mark.asyncio
async def test_request_stop_interrupts_the_turn_and_keeps_the_partial_answer(tmp_path, fake_codex, mem_store):
    """The decision Task 5 left open (BEP 19 §3.10.2): a cooperative stop is
    BOS taking the turn away, not the caller's own deadline — Agent's own
    contract for the same situation (agent.py:648-657, :831-836) is to keep
    what the turn produced rather than raise and discard it. Mirrored here for
    a stop landing mid-stream, on a *different* status (interrupted) than the
    unexplained-interrupted test above raises on (Task 5/6) — the difference
    is entirely in *why* this call itself asked for the interrupt."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(
        fake_codex,
        [
            _item_completed(_agent_message_item("msg-1", "partial answer")),
            HANG,
            _turn_completed(status=TurnStatus.interrupted),
        ],
    )

    task = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    handle = await _hanging_handle(fake_codex)
    agent.request_stop()
    await _poll_until(lambda: handle.interrupted)
    handle.release.set()  # the vendor confirming the interrupt, as a real turn eventually would
    result = await asyncio.wait_for(task, timeout=2)

    assert result.output == "partial answer"
    assert result.finish_reason == "interrupted"
    messages = await mem_store.get_messages("chat-1")
    assert messages[-1].llm_message["content"] == "partial answer", "the partial answer is kept, not discarded"


@pytest.mark.asyncio
async def test_a_failed_interrupt_request_does_not_abort_the_stop(tmp_path, fake_codex, mem_store):
    """handle.interrupt() is a best-effort courtesy to the vendor, not
    something the outcome of a stop depends on: _settle_interrupted's own
    call is wrapped in contextlib.suppress so a transient failure sending it
    (a network blip talking to the app-server) is not mistaken for a real
    turn failure — the stream may still finish on its own regardless, and
    that is what must decide the outcome, not the interrupt request."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(
        fake_codex,
        [
            _item_completed(_agent_message_item("msg-1", "partial answer")),
            HANG,
            _turn_completed(status=TurnStatus.interrupted),
        ],
    )

    task = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    handle = await _hanging_handle(fake_codex)
    handle.interrupt_error = RuntimeError("app-server hung up")
    agent.request_stop()
    await _poll_until(lambda: handle.interrupted)
    handle.release.set()  # the stream finishes on its own despite the interrupt request failing
    result = await asyncio.wait_for(task, timeout=2)

    assert result.output == "partial answer"


@pytest.mark.asyncio
async def test_timeout_seconds_expiry_interrupts_then_raises(tmp_path, fake_codex, mem_store, monkeypatch):
    """BEP 19 §3.10.2 / §7 criterion 20: on expiry the native turn is
    interrupted, then the error is raised. Unlike a cooperative stop, a
    timeout is the caller's own deadline: nothing is kept, and this holds even
    when the native turn never confirms the interrupt at all (it doesn't,
    here) — a timeout must not be left waiting on that confirmation."""
    import bos.extensions.runtimes.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_INTERRUPT_GRACE_SECONDS", 0.05)
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=0.02)
    _arm_notifications(fake_codex, [HANG])  # never yields on its own, and ignores interrupt() too

    with pytest.raises(TimeoutError) as excinfo:
        await agent.run("chat-1", "do it", turn_id="t1")
    message = str(excinfo.value)
    assert "codex" in message and "george" in message and "chat-1" in message

    handle = fake_codex.instances[-1].turn_handles[-1]
    assert handle.interrupted is True, "the native turn is asked to stop even though nothing confirms it"
    assert await mem_store.get_messages("chat-1") == [], "a timed-out turn commits nothing"


@pytest.mark.asyncio
async def test_timeout_seconds_still_raises_when_the_interrupt_rpc_never_returns(
    tmp_path, fake_codex, mem_store, monkeypatch
):
    """Fix round 2 (I2/C1b): the timeout branch asks the native turn to stop
    before raising, and that request used to be guarded by
    contextlib.suppress alone — which catches errors, not slowness. The
    vendor's interrupt is an RPC over a blocking queue on asyncio.to_thread
    with no timeout of its own: it wakes when the child answers or dies. A
    wedged child therefore hung the await *below* which the `raise
    TimeoutError` lives, and timeout_seconds silently stopped being a
    timeout. Bounding the interrupt is what makes the deadline real."""
    import bos.extensions.runtimes.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_INTERRUPT_GRACE_SECONDS", 0.05)
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=0.02)
    _arm_notifications(fake_codex, [HANG])
    _arm_handle(fake_codex, interrupt_hang=asyncio.Event())  # never set: the child never answers

    started = time.perf_counter()
    task = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    with pytest.raises(TimeoutError) as excinfo:
        await asyncio.wait_for(task, timeout=5)
    elapsed = time.perf_counter() - started

    # The outer wait_for raises a *bare* TimeoutError, so checking the message
    # is what tells "timeout_seconds fired" apart from "the test gave up".
    assert "exceeded timeout_seconds" in str(excinfo.value), "run() hung; the outer wait_for fired instead"
    assert elapsed < 1, f"bounded by the interrupt grace, not by the child: {elapsed:.2f}s"
    assert fake_codex.instances[-1].turn_handles[-1].interrupted is True
    assert await mem_store.get_messages("chat-1") == [], "a timed-out turn commits nothing"


@pytest.mark.asyncio
async def test_timeout_seconds_bounds_thread_start(tmp_path, fake_codex, mem_store):
    """Fix round 4: thread_start / thread_resume / thread.turn are awaited
    before _run_turn exists to wrap them in asyncio.timeout, so until now a
    child that wedged during setup hung the turn forever with the caller's
    own deadline never firing. Each now carries its own wait_for.

    Per *attempt*, not a whole-call deadline: that is already what
    timeout_seconds means here, since the schema-retry loop gives every
    attempt a fresh asyncio.timeout and a retried turn can already take a
    multiple of it."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=0.05)
    fake_codex.arm(thread_start_hang=asyncio.Event())  # never set: the child never answers

    started = time.perf_counter()
    with pytest.raises(TimeoutError) as excinfo:
        await asyncio.wait_for(agent.run("chat-1", "do it", turn_id="t1"), timeout=5)
    elapsed = time.perf_counter() - started

    message = str(excinfo.value)
    assert "thread setup (thread_start)" in message, "the phase is named, not just 'timed out'"
    assert "t1" in message and "chat-1" in message and "george" in message
    assert elapsed < 1, f"{elapsed:.2f}s"
    assert await mem_store.get_messages("chat-1") == []


@pytest.mark.asyncio
async def test_timeout_seconds_bounds_thread_resume_without_the_session_continuity_error(
    tmp_path, fake_codex, mem_store
):
    """The resume path's timeout must NOT come out wearing Task 4's
    "this thread could not be resumed, and BOS does not silently start a
    fresh session" message. A child that never answers is not a corrupt or
    expired session, and that wording would send an operator hunting for the
    wrong thing — so the TimeoutError is re-raised ahead of that wrap."""
    from bos.extensions.runtimes._shared import commit_external_turn

    await commit_external_turn(mem_store, "chat-1", turn_id="t0", user_content="a", response="b",
                               runtime="codex", native_session_id="thread_abc")
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=0.05)
    fake_codex.arm(thread_resume_hang=asyncio.Event())  # never set

    started = time.perf_counter()
    with pytest.raises(TimeoutError) as excinfo:
        await asyncio.wait_for(agent.run("chat-1", "do it", turn_id="t1"), timeout=5)
    elapsed = time.perf_counter() - started

    message = str(excinfo.value)
    assert "thread setup (thread_resume)" in message
    assert "could not be resumed" not in message, "a wedged child is not an unresumable session"
    assert "thread_abc" not in message, "nor is the session id the thing to go looking at"
    assert elapsed < 1, f"{elapsed:.2f}s"


@pytest.mark.asyncio
async def test_timeout_seconds_bounds_the_turn_request(tmp_path, fake_codex, mem_store):
    """The third setup RPC: thread.turn() returns the handle _run_turn needs,
    so it too runs before any asyncio.timeout window exists."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=0.05)
    fake_codex.arm(turn_hang=asyncio.Event())  # never set; thread setup itself answers fine

    started = time.perf_counter()
    with pytest.raises(TimeoutError) as excinfo:
        await asyncio.wait_for(agent.run("chat-1", "do it", turn_id="t1"), timeout=5)
    elapsed = time.perf_counter() - started

    message = str(excinfo.value)
    assert "the turn request (thread.turn)" in message
    # Distinguishable from _run_turn's own streaming deadline, which is the
    # whole point of naming the phase.
    assert "and was interrupted" not in message
    assert elapsed < 1, f"{elapsed:.2f}s"
    assert fake_codex.instances[-1].thread_start_calls, "setup itself got through"


@pytest.mark.asyncio
async def test_slow_setup_still_completes_when_no_timeout_seconds_is_set(tmp_path, fake_codex, mem_store):
    """timeout_seconds=None means the caller declined a deadline, and setup
    declines one too rather than inventing a fallback. Asserted as a call that
    *completes* after a slow setup — asserting a hang would only prove the
    test can wait."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=None)
    gate = asyncio.Event()
    fake_codex.arm(thread_start_hang=gate)
    _arm_notifications(
        fake_codex,
        [_item_completed(_agent_message_item("msg-1", "done")), _turn_completed()],
    )

    task = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    # Deterministic rather than timed: thread_start records its call before it
    # blocks, so this waits for the RPC to actually be in flight.
    await _poll_until(lambda: bool(fake_codex.instances) and bool(fake_codex.instances[-1].thread_start_calls))
    gate.set()  # slow, but it does answer

    result = await asyncio.wait_for(task, timeout=2)
    assert result.output == "done"


@pytest.mark.asyncio
async def test_busy_rejects_a_second_turn_on_the_same_chat_but_not_a_different_one(tmp_path, fake_codex, mem_store):
    """Review Focus 3 / BEP 19 §3.10.1: a Codex thread is single-threaded, so a
    second run() on a chat_id already running one is refused rather than
    queued. Tested in both directions: a *different* chat_id must not be
    blocked by it either, or a guard that simply rejects everything would
    pass the first half for free."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    # Each FakeTurnHandle only recognizes a turn/completed notification whose
    # turn_id matches its own handle.id (mirroring the real subscription's
    # scoping, guarded again defensively in _emit_stream); FakeThread.turn()
    # names an unarmed handle "turn-<call count>", so chat-1's turn (the first
    # .turn() call) is "turn-1" and chat-2's (the second — the rejected retry
    # on chat-1 never reaches .turn() at all) is "turn-2". Armed explicitly,
    # per chat, rather than relying on _default_turn_notifications, since HANG
    # has no synthesized form.
    _arm_notifications(fake_codex, [HANG, _turn_completed(turn_id="turn-1")])

    first = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    await _hanging_handle(fake_codex, expected_count=1)

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run("chat-1", "do it again", turn_id="t2")
    assert "chat-1" in str(excinfo.value)

    _arm_notifications(fake_codex, [HANG, _turn_completed(turn_id="turn-2")])
    second = asyncio.ensure_future(agent.run("chat-2", "do it too", turn_id="t3"))
    handle2 = await _hanging_handle(fake_codex, expected_count=2)
    handle1 = fake_codex.instances[-1].turn_handles[0]
    assert handle2 is not handle1, "a different chat_id must run against its own turn, not reuse chat-1's"

    handle1.release.set()
    handle2.release.set()
    await asyncio.wait_for(first, timeout=2)
    await asyncio.wait_for(second, timeout=2)


@pytest.mark.asyncio
async def test_aclose_mid_turn_returns_promptly_and_closes_the_client(tmp_path, fake_codex, mem_store, monkeypatch):
    """BEP 19 §3.10.2 / Review Focus 4: aclose() interrupts an in-flight turn
    and must not hang on one that ignores it — a bounded wait, then close
    regardless, so harness teardown cannot block on a model still thinking."""
    import bos.extensions.runtimes.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_INTERRUPT_GRACE_SECONDS", 0.05)
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [HANG])  # never confirms — aclose() must not wait it out

    turn = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    handle = await _hanging_handle(fake_codex)

    await asyncio.wait_for(agent.aclose(), timeout=1)

    assert handle.interrupted is True
    assert fake_codex.instances[0].closed is True
    turn.cancel()
    with contextlib.suppress(BaseException):
        await turn


@pytest.mark.asyncio
async def test_aclose_is_bounded_when_the_turn_swallows_its_cancel(
    tmp_path, fake_codex, mem_store, monkeypatch, caplog
):
    """Fix round 2 (C1a): _settle_interrupted does NOT guarantee the task is
    finished. When the turn ignores both the interrupt and the cancel it
    *abandons* it and raises — the same doctrine as Agent._abandon: what is
    still unwinding is left to the loop. An abandoned task outlives the turn
    that owned it, and aclose()'s wait then never returns, so client.close()
    — the only thing that reaps the codex child process — is never reached.

    The double had to learn to swallow a cancel before this was reachable at
    all: round 1 deleted this very bound because a mutation test showed it
    never fired, and it never fired because the fake always died on cancel."""
    import bos.extensions.runtimes.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_INTERRUPT_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(codex_mod, "_ACLOSE_GRACE_SECONDS", 0.3)
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [HANG])
    _arm_handle(fake_codex, swallow_cancel=True)

    turn = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    handle = await _hanging_handle(fake_codex)
    stream_task = agent._in_flight["chat-1"]  # the task aclose() will have to give up on

    started = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger="bos.extensions.runtimes.codex"):
        await asyncio.wait_for(agent.aclose(), timeout=5)
    elapsed = time.perf_counter() - started

    assert handle.cancels_swallowed >= 1, "the abandon path was never reached; the test proves nothing"
    assert 0.25 <= elapsed < 1.5, f"aclose() is bounded by _ACLOSE_GRACE_SECONDS, not by the turn: {elapsed:.2f}s"
    assert fake_codex.instances[0].closed is True, "the child is reaped even though the turn never let go"
    assert any("still running after" in r.getMessage() for r in caplog.records), "giving up is reported, not silent"

    # Retire the abandoned task: it is the loop's now, and would otherwise
    # outlive the test with an unretrieved exception.
    handle.swallow_cancel = False
    stream_task.cancel()
    for pending in (turn, stream_task):
        with contextlib.suppress(BaseException):
            await pending


@pytest.mark.asyncio
async def test_aclose_is_bounded_when_the_interrupt_rpc_never_returns(tmp_path, fake_codex, mem_store, monkeypatch):
    """The end-to-end shutdown scenario for the other unbounded path: aclose()
    reaches client.close() only after every in-flight turn's
    _settle_interrupted has returned, and that call awaited handle.interrupt()
    under contextlib.suppress — which catches errors, not slowness. A child
    that never answers the interrupt RPC held aclose() open forever and was
    never terminated, so one wedged agent blocked the whole harness shutdown
    (core/_utils._aclose catches exceptions but sets no timeout of its own).

    Does NOT pin C1b's bound on its own — either C1b's wait_for or C1a's
    _ACLOSE_GRACE_SECONDS is enough to make it pass, and it fails only with
    both reverted. The test that discriminates C1b is
    test_timeout_seconds_still_raises_when_the_interrupt_rpc_never_returns
    (fix round 3, N6)."""
    import bos.extensions.runtimes.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_INTERRUPT_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(codex_mod, "_ACLOSE_GRACE_SECONDS", 0.5)
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    _arm_notifications(fake_codex, [HANG])
    _arm_handle(fake_codex, interrupt_hang=asyncio.Event())  # never set: the child never answers

    turn = asyncio.ensure_future(agent.run("chat-1", "do it", turn_id="t1"))
    handle = await _hanging_handle(fake_codex)

    started = time.perf_counter()
    await asyncio.wait_for(agent.aclose(), timeout=5)
    elapsed = time.perf_counter() - started

    assert handle.interrupted is True, "the interrupt was still attempted, just not waited out"
    assert elapsed < 1.5, f"the wedged interrupt RPC must not hold aclose() open: {elapsed:.2f}s"
    assert fake_codex.instances[0].closed is True, "the child is reaped regardless"

    turn.cancel()
    with contextlib.suppress(BaseException):
        await turn


@pytest.mark.asyncio
async def test_aclose_is_bounded_when_the_auth_preflight_never_answers(tmp_path, fake_codex, monkeypatch):
    """Fix round 3 (A/N3): the third unbounded path, and the one round 2's new
    prose denied could exist. _preflight_auth awaits client.account() — the
    same unbounded queue-backed RPC as interrupt() — while holding
    _client_lock, which is the lock aclose() needs before it can reach
    client.close(). A login check the child never answers therefore held the
    whole harness shutdown open and left the child unreaped: C1's exact
    symptom, on a path C1's own fix did not cover.

    Bounded at the RPC, not at aclose()'s lock acquisition: the latter would
    free teardown while leaving _ensure_client() hung forever for the turn
    that asked for the client. The bound is its own constant, not a teardown
    grace — this is a credential check against a live service.

    Note test_aclose_racing_client_construction_does_not_leak_a_client below,
    which *sets* account_hang and so only ever exercised the answered case."""
    import bos.extensions.runtimes.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_PREFLIGHT_AUTH_SECONDS", 0.1)
    fake_codex.arm(account_hang=asyncio.Event())  # never set: the child never answers
    agent = _agent(tmp_path, fake_codex)  # auth defaults to "subscription"

    build = asyncio.ensure_future(agent._ensure_client())
    await _poll_until(lambda: bool(fake_codex.instances))

    started = time.perf_counter()
    await asyncio.wait_for(agent.aclose(), timeout=5)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.5, f"aclose() must not wait out an unanswered preflight: {elapsed:.2f}s"
    assert fake_codex.instances[0].closed is True, "the half-built client is closed, not leaked"

    # The waiting turn gets a real, actionable error rather than hanging with it.
    with pytest.raises(RuntimeError) as excinfo:
        await build
    assert "did not answer within" in str(excinfo.value)


@pytest.mark.asyncio
async def test_aclose_racing_client_construction_does_not_leak_a_client(tmp_path, fake_codex):
    """Task 4's hole, closed by Task 7: aclose() did not take _client_lock, so
    it could decide there was no client to close while _ensure_client() was
    still building one — which would then finish, unclosed, after aclose()
    had already returned. Both now take the lock, so aclose() either finds no
    client yet or waits for the one being built and closes that."""
    fake_codex.arm(account_hang=asyncio.Event())
    agent = _agent(tmp_path, fake_codex)  # auth defaults to "subscription" -> _ensure_client awaits account()

    build = asyncio.ensure_future(agent._ensure_client())
    await _poll_until(lambda: bool(fake_codex.instances))
    instance = fake_codex.instances[0]

    closer = asyncio.ensure_future(agent.aclose())
    await asyncio.sleep(0.01)  # let aclose() start waiting on _client_lock, still held by _ensure_client
    assert instance.account_hang is not None
    instance.account_hang.set()  # let _ensure_client's preflight finish and release the lock

    client = await asyncio.wait_for(build, timeout=2)
    await asyncio.wait_for(closer, timeout=2)

    assert client.closed is True, "the client built while aclose() was waiting must still get closed"


def test_unwrap_thread_item_falls_back_when_root_is_absent():
    """The `hasattr(item, "root") else item` half of the vendor's own unwrap
    (openai_codex/_run.py:36-40) — every item built through ThreadItem(...) in
    the tests above always has `.root`, so only a direct unit test exercises
    the fallback for an item that arrives already unwrapped."""
    from bos.extensions.runtimes.codex import _unwrap_thread_item

    class _NoRoot:
        pass

    bare = _NoRoot()
    assert _unwrap_thread_item(bare) is bare


# --- Task 8: approvals are decided by policy, never awaited (BEP 19 §3.5.4) ---


def test_codex_approval_handler_attribute_exists():
    """The tripwire that makes `_ensure_client`'s private reach-in acceptable,
    run against the REAL openai_codex rather than the double — the double
    mirrors this shape, so on its own it would only prove the double.

    Both halves matter. The first pins the attribute path and the hazard: the
    vendor's default is installed there and it auto-accepts, so an
    openai-codex that renames or moves the attribute must break CI rather than
    quietly restore auto-accept. The second fires in the good direction — if a
    future release adds a supported `approval_handler` parameter, this fails
    and tells us to stop reaching in.

    Offline: AsyncCodex.__init__ only builds the sync client (api.py:316-320),
    which only assigns fields; the `codex app-server` child is spawned by
    start(), so nothing here needs a process or a login.
    """
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.client import CodexClient

    codex = AsyncCodex(CodexConfig())
    handler = codex._client._sync._approval_handler

    assert handler.__func__ is CodexClient._default_approval_handler
    assert handler.__self__ is codex._client._sync
    assert "approval_handler" not in inspect.signature(AsyncCodexClient.__init__).parameters, (
        "openai-codex grew a supported way to pass a handler; use it instead of reaching into _sync"
    )


@pytest.mark.parametrize(
    "method, expected",
    [
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("item/permissions/requestApproval", {"permissions": {}}),
        ("execCommandApproval", {"decision": {"denied": {"rejection": _LEGACY_REJECTION}}}),
        ("applyPatchApproval", {"decision": {"denied": {"rejection": _LEGACY_REJECTION}}}),
    ],
)
def test_every_codex_approval_request_is_refused_on_the_wire(tmp_path, fake_codex, method, expected):
    """All five approval methods in the ServerRequest union, and the exact dict
    the reader thread writes back as the JSON-RPC `result` for each.

    The expected values are literals here on purpose, not `_APPROVAL_DENIALS`
    imported from the source: a test that imports the mapping it checks cannot
    catch the mapping being wrong, and being wrong here is a protocol
    violation on the wire — there is no `"deny"` decision in this protocol,
    which is exactly the plausible guess these literals exist to catch.

    The legacy pair references `_LEGACY_REJECTION` rather than repeating its
    prose, which keeps the literal pinning the part that can violate the
    protocol — the `decision` / `denied` / `rejection` nesting — while leaving
    the wording free to be improved. What that string has to say is asserted
    in test_the_two_legacy_methods_refuse_with_a_rejection_the_agent_can_act_on.
    """
    agent = _agent(tmp_path, fake_codex)

    assert agent._deny_approval(method, {"anything": "at all"}) == expected


def test_a_non_approval_server_request_falls_through_to_an_empty_answer(tmp_path, fake_codex, caplog):
    """The handler is handed EVERY server-to-client request, not just the five
    approvals. The other five (item/tool/call here) keep the vendor default's
    `{}`, and keep it silently: none of them is an escalation being refused —
    item/tool/call asks the client to run a tool it never registered — so
    there is nothing to report. Widening those answers is out of Task 8's
    scope; this test pins that Task 8 did not narrow them either."""
    agent = _agent(tmp_path, fake_codex)

    with caplog.at_level(logging.WARNING, logger="bos.extensions.runtimes.codex"):
        assert agent._deny_approval("item/tool/call", {"name": "grep"}) == {}

    assert not [r for r in caplog.records if r.name == "bos.extensions.runtimes.codex"]


@pytest.mark.asyncio
async def test_the_approval_handler_is_installed_before_the_auth_preflight(tmp_path, fake_codex):
    """Installed on the client the agent actually builds, and installed before
    account() — the first call that can spawn the child and so the first
    moment the server can ask for anything. Arriving one await late would mean
    the vendor's auto-accepting default answered it."""
    agent = _agent(tmp_path, fake_codex)  # auth defaults to "subscription" -> _ensure_client awaits account()

    client = await agent._ensure_client()

    assert client.approval_handler_at_account == agent._deny_approval, (
        "installed too late: the first RPC went out before the handler was in place"
    )
    assert client._client._sync._approval_handler == agent._deny_approval


def test_a_refused_escalation_is_logged_at_warning(tmp_path, fake_codex, caplog):
    """A silently refused escalation looks exactly like a model that decided
    not to try, and only one of those explains why the agent could not finish.
    The method, the runtime and the agent kind are all in the line."""
    agent = _agent(tmp_path, fake_codex)

    with caplog.at_level(logging.WARNING, logger="bos.extensions.runtimes.codex"):
        agent._deny_approval("item/commandExecution/requestApproval", {"command": ["sudo", "rm", "-rf", "/"]})

    # Filtered by logger name as well as level, for the same reason as
    # test_a_failed_steer_request_is_logged_and_does_not_abort_the_turn: an
    # unrelated WARNING elsewhere in the session lands in caplog.records too.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "bos.extensions.runtimes.codex"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "item/commandExecution/requestApproval" in message
    assert "codex" in message and "george" in message


def test_the_two_legacy_methods_refuse_with_a_rejection_the_agent_can_act_on(tmp_path, fake_codex):
    """`denied` over `abort` is only worth choosing because of this string.

    `abort` tells the agent to stop until the user's next command; `denied`
    tells it to try something else, and is the one refusal in the protocol
    that carries text back to the model. That text is therefore the part that
    makes adapting possible, so it is asserted here rather than left to the
    mapping: one shared constant, the same on both methods, and the full
    nesting the schema requires — `decision` (required on the response),
    `denied` (the only key DeniedReviewDecision allows), `rejection` (required
    on it, a string).

    The prose itself is asserted on substance, not word for word, so it can be
    improved without a test edit — but it cannot be gutted into a bare "no",
    and it cannot leak BOS's own vocabulary at a model that has never heard of
    a BEP.
    """
    agent = _agent(tmp_path, fake_codex)

    refused_exec = agent._deny_approval("execCommandApproval", {"command": ["sudo", "true"]})
    refused_patch = agent._deny_approval("applyPatchApproval", {"patch": "..."})

    assert refused_exec == {"decision": {"denied": {"rejection": _LEGACY_REJECTION}}}
    assert refused_patch == refused_exec, "both legacy methods send the same refusal"

    assert "unattended" in _LEGACY_REJECTION, "it says why no one can be asked"
    assert "sandbox" in _LEGACY_REJECTION, "it says what the agent can still work within"
    assert "BEP" not in _LEGACY_REJECTION and "§" not in _LEGACY_REJECTION, "model-facing text, not BOS jargon"


# --- Task 9: reading a Codex transcript back (BEP 19 §3.7) ------------------


def _wire_thread(*turns: Turn, thread_id: str = "thread-1") -> Any:
    """A REAL `ThreadReadResponse` wrapping *turns*, validated the way the
    vendor validates one off the wire.

    `Thread` has twelve required fields, none of which this feature reads
    except `turns`, so they are supplied as the wire payload pydantic actually
    receives (camelCase aliases, `source` as a bare string, `status` as its
    RootModel's inner shape) rather than by hand-constructing four more nested
    models. `model_validate` is the same entry point `AsyncCodexClient` uses,
    so a field the vendor renames or retypes breaks these tests here.

    The `Turn` objects pass through untouched — pydantic's default
    `revalidate_instances="never"` — which is what lets a test choose whether
    a turn's `items_view` is the enum or the raw string default. See
    test_a_turn_that_omits_items_view_is_projected_not_marked for why that
    distinction is the point.
    """
    from openai_codex.generated.v2_all import ThreadReadResponse

    return ThreadReadResponse.model_validate(
        {
            "thread": {
                "cliVersion": "0.0.0",
                "createdAt": 0,
                "cwd": "/tmp",
                "ephemeral": False,
                "id": thread_id,
                "modelProvider": "openai",
                "preview": "",
                "sessionId": "session-1",
                "source": "appServer",
                "status": {"type": "idle"},
                "turns": list(turns),
                "updatedAt": 0,
            }
        }
    )


def _user_turn(turn_id: str, *content: Any, started_at: int | None = None, **turn_kwargs: Any) -> Turn:
    """One completed turn holding a single `UserMessageThreadItem` built from
    real `UserInput` members."""
    from openai_codex.generated.v2_all import UserInput, UserMessageThreadItem

    item = UserMessageThreadItem(
        id=f"{turn_id}-user", type="userMessage", content=[UserInput(c) for c in content]
    )
    return Turn(
        id=turn_id,
        items=[ThreadItem(item)],
        status=TurnStatus.completed,
        started_at=started_at,
        **turn_kwargs,
    )


def _agent_item(item_id: str, text: str, phase: MessagePhase | None) -> ThreadItem:
    return ThreadItem(AgentMessageThreadItem(id=item_id, type="agentMessage", text=text, phase=phase))


async def _bound_chat(agent, mem_store, *, chat_id: str = "chat-1", session_id: str = "thread-1") -> None:
    """Bind *chat_id* to *session_id* the only way BOS ever does — a committed
    external turn (BEP 19 §3.6) — so `native_messages` recovers it through the
    same `read_native_session_id` scan production uses."""
    from bos.extensions.runtimes._shared import commit_external_turn

    await commit_external_turn(
        mem_store, chat_id, turn_id="bos-turn-1", user_content="q", response="a",
        runtime="codex", native_session_id=session_id,
    )


@pytest.mark.asyncio
async def test_a_chat_with_no_native_session_reads_as_an_empty_transcript(tmp_path, fake_codex, mem_store, caplog):
    """An empty transcript, not a missing one: nothing ever ran here. It must
    not raise, and — the part worth pinning — it must not build a client or
    send a read either, since there is no thread id to send."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)

    with caplog.at_level(logging.DEBUG, logger="bos.extensions.runtimes.codex"):
        assert await agent.native_messages("never-used") == []

    assert fake_codex.instances == [], "no native session means nothing to ask, so no client either"
    debug = [
        r for r in caplog.records
        if r.name == "bos.extensions.runtimes.codex" and r.levelno == logging.DEBUG
    ]
    assert any("never-used" in r.getMessage() for r in debug)


@pytest.mark.asyncio
async def test_an_agent_with_no_chat_store_reads_as_an_empty_transcript(tmp_path, fake_codex):
    """`chat_store=None` is allowed (ExternalRuntime), and with no store there
    is nowhere a session id could have been recorded."""
    agent = _agent(tmp_path, fake_codex)

    assert await agent.native_messages("chat-1") == []


@pytest.mark.asyncio
async def test_a_deleted_thread_surfaces_as_an_error_not_an_empty_list(tmp_path, fake_codex, mem_store):
    """The plan's own requirement, and the whole reason Ruling 4's `[]` is
    scoped to "no session id at all": a session id that exists and cannot be
    read is a different answer. Driven with a real `CodexError` subclass —
    `InvalidParamsError` is what `map_jsonrpc_error` returns for -32602
    (errors.py) — because the vendor raises for an archived or deleted thread
    rather than returning an empty one."""
    from openai_codex.errors import CodexError, InvalidParamsError

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store, session_id="thread-gone")
    fake_codex.arm(thread_read_error=InvalidParamsError(-32602, "unknown thread: thread-gone"))

    with pytest.raises(RuntimeError) as excinfo:
        await agent.native_messages("chat-1")

    message = str(excinfo.value)
    assert "thread-gone" in message and "chat-1" in message and "george" in message
    assert isinstance(excinfo.value.__cause__, CodexError), "the vendor error is kept as the cause"


@pytest.mark.asyncio
async def test_the_read_does_not_resume_the_session(tmp_path, fake_codex, mem_store):
    """Ruling 1, and the reason it is a ruling: `_thread_for` would call
    `thread_resume` with this agent's sandbox, approval mode and cwd, which is
    a write to a live session on what the caller asked to be a read. The read
    goes out on a bare `AsyncThread` built over the client instead — the
    vendor's own `AsyncThread.read()`, landing on `_client.thread_read`."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store, session_id="thread-7")
    fake_codex.arm(next_thread_read=_wire_thread())

    assert await agent.native_messages("chat-1") == []

    client = fake_codex.instances[-1]
    assert client.thread_resume_calls == [], "a read must not resume the session"
    assert client.thread_start_calls == [], "a read must not start one either"
    assert client.thread_read_calls == [("thread-7", True)], "include_turns is what populates Turn.turns"


@pytest.mark.asyncio
async def test_it_projects_user_and_final_answers_and_drops_commentary(tmp_path, fake_codex, mem_store):
    """Ruling 3: two of the nineteen ThreadItem variants are messages, and a
    `commentary`-phase agent message is not the answer — the same rule
    `_final_assistant_response_from_items` applies. Order is the thread's."""
    from openai_codex.generated.v2_all import TextUserInput, UserInput, UserMessageThreadItem

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    turn = Turn(
        id="native-1",
        items=[
            ThreadItem(
                UserMessageThreadItem(
                    id="i-user", type="userMessage",
                    content=[UserInput(TextUserInput(type="text", text="what is 2+2?"))],
                )
            ),
            _agent_item("i-commentary", "let me think about that", MessagePhase.commentary),
            _agent_item("i-answer", "4", MessagePhase.final_answer),
        ],
        status=TurnStatus.completed,
    )
    fake_codex.arm(next_thread_read=_wire_thread(turn))

    messages = await agent.native_messages("chat-1")

    assert [(m.llm_message["role"], m.llm_message["content"]) for m in messages] == [
        ("user", "what is 2+2?"),
        ("assistant", "4"),
    ]
    assert [m.metadata["source"] for m in messages] == ["codex", "codex"]
    assert [m.metadata["native_turn_id"] for m in messages] == ["native-1", "native-1"]
    assert [m.metadata["native_item_id"] for m in messages] == ["i-user", "i-answer"]
    assert [m.turn_id for m in messages] == [None, None], "a BOS turn id would be invented, not read"


@pytest.mark.asyncio
async def test_an_agent_message_with_no_phase_is_kept(tmp_path, fake_codex, mem_store):
    """`phase` is optional, and only `commentary` is excluded. Dropping a
    no-phase message would lose the answer on any turn the vendor does not
    label — the same reason `_final_assistant_response_from_items` falls back
    to it."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    turn = Turn(id="native-1", items=[_agent_item("i-1", "unlabelled", None)], status=TurnStatus.completed)
    fake_codex.arm(next_thread_read=_wire_thread(turn))

    messages = await agent.native_messages("chat-1")

    assert [m.llm_message["content"] for m in messages] == ["unlabelled"]


@pytest.mark.asyncio
async def test_non_message_items_including_a_compaction_do_not_break_the_projection(
    tmp_path, fake_codex, mem_store
):
    """The other seventeen variants are the turn's internal work. A
    `ContextCompactionThreadItem` is the interesting one: it is inline in the
    transcript, marking where the vendor compacted, and it sits between two
    real messages here so a skip that took the rest of the turn with it would
    show."""
    from openai_codex.generated.v2_all import (
        ContextCompactionThreadItem,
        LegacyAppPathString,
        ReasoningThreadItem,
        TextUserInput,
    )

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    turn = _user_turn("native-1", TextUserInput(type="text", text="hello"))
    turn.items.extend(
        [
            ThreadItem(ReasoningThreadItem(id="i-reason", type="reasoning")),
            ThreadItem(ContextCompactionThreadItem(id="i-compact", type="contextCompaction")),
            ThreadItem(
                CommandExecutionThreadItem(
                    id="i-cmd", type="commandExecution", command="ls", command_actions=[],
                    cwd=LegacyAppPathString("/tmp"), status=CommandExecutionStatus.completed,
                )
            ),
            _agent_item("i-answer", "hi", MessagePhase.final_answer),
        ]
    )
    # Guard against the fixture silently rotting into "one user item": the
    # point of this test is what happens with the other four present.
    assert len(turn.items) == 5
    fake_codex.arm(next_thread_read=_wire_thread(turn))

    messages = await agent.native_messages("chat-1")

    assert [(m.llm_message["role"], m.llm_message["content"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["notLoaded", "summary"])
async def test_a_turn_the_vendor_did_not_load_becomes_a_visible_gap(tmp_path, fake_codex, mem_store, caplog, view):
    """Ruling 2. A turn whose `items` are absent or summarized must not be
    projected as if it were the transcript, and must not vanish either: a
    silently shorter list is the exact lie §3.7 exists to prevent. One marker
    per turn, carrying the value, plus a WARNING — and the surrounding turns
    still project."""
    from openai_codex.generated.v2_all import TextUserInput, TurnItemsView

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    before = _user_turn("native-1", TextUserInput(type="text", text="first"))
    hidden = Turn(id="native-2", items=[], status=TurnStatus.completed, items_view=TurnItemsView(view))
    after = _user_turn("native-3", TextUserInput(type="text", text="third"))
    fake_codex.arm(next_thread_read=_wire_thread(before, hidden, after))

    with caplog.at_level(logging.WARNING, logger="bos.extensions.runtimes.codex"):
        messages = await agent.native_messages("chat-1")

    assert [m.llm_message["role"] for m in messages] == ["user", "system", "user"]
    marker = messages[1]
    assert marker.metadata == {"source": "codex", "native_turn_id": "native-2", "items_view": view}
    assert view in marker.llm_message["content"], "the marker says which state the vendor reported"

    warnings = [
        r for r in caplog.records
        if r.name == "bos.extensions.runtimes.codex" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "native-2" in warnings[0].getMessage() and view in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_turn_that_omits_items_view_is_projected_not_marked(tmp_path, fake_codex, mem_store, caplog):
    """The divergence `_LOADED_ITEMS_VIEWS` exists for, and it is a production
    path, not a test artifact: `Turn.items_view` defaults to the *string*
    `"full"`, pydantic does not validate defaults, and `TurnItemsView` is a
    plain Enum — so a payload that omits `itemsView` arrives as `'full'`,
    which is neither `is` nor `==` `TurnItemsView.full`. The first assert
    pins that against the real vendor model; an identity check in the
    projection would bury every such turn under a gap marker.

    The enum form is asserted beside it so the rule covers both, and the
    WARNING count is what tells a false marker from a real one.
    """
    from openai_codex.generated.v2_all import TextUserInput, TurnItemsView

    assert Turn.model_validate({"id": "x", "items": [], "status": "completed"}).items_view == "full", (
        "vendor check: an omitted itemsView is the raw default string, not the enum"
    )
    assert TurnItemsView.full != "full", "…and a plain Enum is not equal to its own value"

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    omitted = _user_turn("native-1", TextUserInput(type="text", text="defaulted"))
    explicit = _user_turn(
        "native-2", TextUserInput(type="text", text="validated"), items_view=TurnItemsView.full
    )
    assert omitted.items_view == "full" and explicit.items_view is TurnItemsView.full
    fake_codex.arm(next_thread_read=_wire_thread(omitted, explicit))

    with caplog.at_level(logging.WARNING, logger="bos.extensions.runtimes.codex"):
        messages = await agent.native_messages("chat-1")

    assert [m.llm_message["content"] for m in messages] == ["defaulted", "validated"]
    assert not [r for r in caplog.records if r.name == "bos.extensions.runtimes.codex"]


@pytest.mark.asyncio
async def test_user_content_round_trips_through_the_inverse_mapping(tmp_path, fake_codex, mem_store):
    """`_codex_input_to_content` mirrors `_content_to_codex_input` member by
    member, so a message BOS sent reads back as the same message. Driven
    through the real forward mapping rather than hand-written wire items, so
    the two cannot drift apart silently.

    `mime_type` is the one field that cannot survive: `MentionInput` has no
    slot for it outbound, so the inverse guesses from the path — `.md` here,
    which `mimetypes` knows.
    """
    from openai_codex.generated.v2_all import (
        ImageUserInput,
        LocalImageUserInput,
        MentionUserInput,
        TextUserInput,
        UserInput,
    )

    from bos.extensions.runtimes.codex import _codex_input_to_content, _content_to_codex_input

    sent: Any = [
        {"type": "text", "text": "look at this"},
        {"type": "image", "source": {"kind": "url", "value": "https://example.com/a.png"}},
        {"type": "image", "source": {"kind": "path", "value": "/tmp/b.png"}},
        {"type": "file", "mime_type": "text/markdown", "source": {"kind": "path", "value": "/tmp/notes.md"}},
    ]
    outbound = _content_to_codex_input(sent)
    assert isinstance(outbound, list)  # a list of parts in, a list of InputItems out
    assert [type(i).__name__ for i in outbound] == ["TextInput", "ImageInput", "LocalImageInput", "MentionInput"]

    # What the vendor stores for that input: the same four kinds, as the
    # UserInput members the wire round trip produces (`_to_wire_item`).
    stored = [
        UserInput(TextUserInput(type="text", text="look at this")),
        UserInput(ImageUserInput(type="image", url="https://example.com/a.png")),
        UserInput(LocalImageUserInput(type="localImage", path="/tmp/b.png")),
        UserInput(MentionUserInput(type="mention", name="notes.md", path="/tmp/notes.md")),
    ]

    assert _codex_input_to_content(stored) == sent

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    fake_codex.arm(next_thread_read=_wire_thread(_user_turn("native-1", *[u.root for u in stored])))
    messages = await agent.native_messages("chat-1")
    assert messages[0].llm_message["content"] == sent


def test_a_lone_text_part_reads_back_as_a_plain_string():
    """`Agent.ask("hi")` stores a plain string in the BOS record and sends a
    single TextInput to Codex. Without this normalization the same message
    would read as a one-item list from the native side and a string from the
    BOS side."""
    from openai_codex.generated.v2_all import TextUserInput, UserInput

    from bos.extensions.runtimes.codex import _codex_input_to_content

    assert _codex_input_to_content([UserInput(TextUserInput(type="text", text="hi"))]) == "hi"
    assert _codex_input_to_content([]) == [], "…but an empty content list is not a string"


def test_a_user_input_with_no_bos_counterpart_becomes_text_rather_than_vanishing():
    """The four `UserInput` members `_content_to_codex_input` never sends can
    still arrive from a thread the `codex` CLI authored. BOS has no part for
    any of them; dropping one would make the message read as though the user
    never sent it."""
    from openai_codex.generated.v2_all import AudioUserInput, SkillUserInput, TextUserInput, UserInput

    from bos.core.agent import content_to_plain_text
    from bos.extensions.runtimes.codex import _codex_input_to_content

    content = _codex_input_to_content(
        [
            UserInput(TextUserInput(type="text", text="run it")),
            UserInput(SkillUserInput(type="skill", name="review", path="/skills/review")),
            UserInput(AudioUserInput(type="audio", url="https://example.com/a.wav")),
        ]
    )

    assert isinstance(content, list)
    assert [part["type"] for part in content] == ["text", "text", "text"], "three parts in, three out"
    # Rendered rather than indexed, so the placeholder wording stays free to
    # improve while the thing that matters — the kind is named, not dropped —
    # is still asserted.
    rendered = content_to_plain_text(content)
    assert "run it" in rendered and "SkillUserInput" in rendered and "AudioUserInput" in rendered


@pytest.mark.asyncio
async def test_created_at_follows_the_turn_rather_than_defaulting_to_now(tmp_path, fake_codex, mem_store):
    """Per-turn is the finest granularity Codex offers. Without it every
    message in a year-old thread would carry today's date, which a host
    renders as a timestamp and a reader believes."""
    from openai_codex.generated.v2_all import TextUserInput

    agent = _agent(tmp_path, fake_codex, chat_store=mem_store)
    await _bound_chat(agent, mem_store)
    dated = _user_turn("native-1", TextUserInput(type="text", text="old"), started_at=1_600_000_000)
    undated = _user_turn("native-2", TextUserInput(type="text", text="new"))
    fake_codex.arm(next_thread_read=_wire_thread(dated, undated))

    messages = await agent.native_messages("chat-1")

    assert messages[0].created_at == datetime.fromtimestamp(1_600_000_000)
    assert messages[1].created_at > messages[0].created_at, "no started_at falls back to now"


@pytest.mark.asyncio
async def test_the_read_is_bounded_by_timeout_seconds(tmp_path, fake_codex, mem_store):
    """`thread_read` is the same unbounded request path as every other vendor
    RPC here (`_call_sync` -> `asyncio.to_thread` -> a queue read with no
    timeout), so a wedged child would otherwise leave `get_messages` with no
    answer and no deadline. The message names the read, so it is not mistaken
    for a turn that timed out."""
    agent = _agent(tmp_path, fake_codex, chat_store=mem_store, timeout_seconds=0.05)
    await _bound_chat(agent, mem_store, session_id="thread-wedged")
    fake_codex.arm(thread_read_hang=asyncio.Event())

    with pytest.raises(TimeoutError) as excinfo:
        await agent.native_messages("chat-1")

    message = str(excinfo.value)
    assert "transcript" in message and "thread-wedged" in message and "timeout_seconds=0.05" in message


@pytest.mark.asyncio
async def test_it_carries_the_two_surfaces_bosapp_routes_on(tmp_path, fake_codex):
    """`BosApp.get_messages(source="native")` finds this class by two names and
    nothing else: `resolved_config["external_runtime"]`, matched against the
    runtime in the chat's stored metadata, and a duck-typed `native_messages`.
    Neither is on `AgentPort` or `ExternalRuntime`, so nothing but this test
    fails if one is renamed — test_sdk.py routes against a stub, deliberately,
    and a stub cannot notice.
    """
    agent = _agent(tmp_path, fake_codex)

    assert agent.resolved_config["external_runtime"] == "codex"
    assert inspect.iscoroutinefunction(agent.native_messages)
    assert list(inspect.signature(agent.native_messages).parameters) == ["chat_id"]
