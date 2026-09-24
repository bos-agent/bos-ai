"""BEP 19 Layer 4a: the Codex runtime.

Stage 4 of 4 over CodexAgent's turn path: construction, config resolution and
lifecycle (Task 3), the client and thread lifecycle (Task 4), a turn that
runs and persists itself (Task 5), and here — that turn streamed as
TurnEvents (Task 6). Tasks 7-8 add interrupt/cooperative-stop/timeout and the
approval handler on top of this.
"""

from __future__ import annotations

from typing import Any

import pytest
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

from bos.extensions.chat_stores.in_memory import InMemChatStore


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
