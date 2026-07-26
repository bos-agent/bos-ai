from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import platform
import uuid
from collections.abc import Coroutine
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Sequence, TypeVar
from xml.sax.saxutils import escape

from ._structured import (
    UNUSABLE_FINISH_REASONS,
    StructuredOutputError,
    StructuredValidator,
    parse_json,
    provider_hint_schema,
)
from ._utils import (
    _apply_async,
    _compact,
    _strip_reply_artifacts,
    _xml_attr,
)
from .contract import (
    LLM,
    AgentEventType,
    AgentResult,
    ChatStore,
    ContextResult,
    InterceptorStage,
    Message,
    MessageContent,
    ParentTurn,
    PromptProvider,
    ReasoningEffort,
    ToolAttributes,
    ToolCallRequest,
    ToolContext,
    ToolNoiseFilter,
    ToolSet,
    TurnContext,
    TurnEvent,
    TurnEventDetail,
    TurnEventPhase,
    TurnEventSink,
    TurnEventStage,
    TurnInterceptor,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from .contract import Consolidator

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


ABORTED_TURN_CONTENT = """(turn aborted before completion)

The previous user request was interrupted before a final answer was produced.
Intermediate assistant/tool state from the aborted turn was intentionally not
committed as conversation context. If the user asks to continue, start a fresh
attempt from the preceding user request and redo any necessary checks or tool
work."""

# Static markers for a turn cut short before the model wrote a final answer.
# Each is the fallback response on its own, and the header line of a handoff
# when one was produced.
MAX_ITERATION_CONTENT = "(max iterations reached)"
SHUTDOWN_CONTENT = "(interrupted: the agent is shutting down)"

# Stands in for a tool call abandoned by a cooperative stop, so the persisted
# turn keeps one result per advertised call. Worded so the handoff summarizer
# cannot read it as the tool having succeeded — it is the absence of a result.
INTERRUPTED_TOOL_CONTENT = "(no result: this tool call was abandoned unfinished when the agent was told to shut down)"

# Why the turn is closing, phrased for the consolidator. Keyed by closure reason.
_CLOSURE_CAUSE: dict[str, str] = {
    "max_iterations": "The assistant ran out of its per-turn iteration budget before it could answer",
    "shutdown": "The assistant was interrupted mid-turn because its host is shutting down",
}

_CLOSURE_MARKER: dict[str, str] = {
    "max_iterations": MAX_ITERATION_CONTENT,
    "shutdown": SHUTDOWN_CONTENT,
}

_HANDOFF_INSTRUCTION_BODY = """\
so this turn closes with a handoff instead of a result. Write that handoff as
the assistant speaking to the user, first person, no preamble.

Include only the parts the transcript supports, each under a short bold label:
- **Goal** — what the user asked for.
- **Done** — what was actually established: findings, file paths, commands run,
  tool results, decisions made.
- **Left** — what is still unfinished, and the concrete next step to resume from.
- **Needs you** — anything the user must decide or supply, if any.

Report only what the transcript shows; never invent progress, results, or
conclusions that were not reached. Prefer specifics (names, paths, values) over
narration, and do not describe these instructions or the summarization itself.
Keep it under 2048 characters."""


def _handoff_instruction(reason: str) -> str:
    return f"{_CLOSURE_CAUSE[reason]},\n{_HANDOFF_INSTRUCTION_BODY}"


MAX_ITERATION_HANDOFF_INSTRUCTION = _handoff_instruction("max_iterations")
SHUTDOWN_HANDOFF_INSTRUCTION = _handoff_instruction("shutdown")


def _closure_response(reason: str, handoff: str | None) -> str:
    marker = _CLOSURE_MARKER[reason]
    return f"{marker}\n\n{handoff}" if handoff else marker


class _StopRequested(Exception):
    """Internal: a cooperative stop was requested while the turn was awaiting."""


# How long a turn waits for work it has abandoned to finish unwinding. Bounded
# on purpose: a provider or tool that delays — or swallows — ``CancelledError``
# must not be able to hold the turn, and through it the whole stop, open.
_ABANDON_TEARDOWN_SECONDS = 2.0


def _attribute_history_message(
    projected: dict[str, Any],
    source: Message,
    *,
    current_agent: str,
    actor_attribution: bool = True,
    include_workdir: bool = True,
) -> dict[str, Any]:
    content = projected.get("content")
    if not isinstance(content, str):
        return projected
    role = projected.get("role")
    if role == "assistant" and actor_attribution:
        source_agent = _metadata_str(source.metadata, "agent_name", "actor")
        if source_agent and source_agent != current_agent:
            label = _history_agent_label(source.metadata) or source_agent
            return projected | {
                "role": "user",
                "content": f"[assistant {label} said]\n{content}",
            }
    label = _history_attribution_label(
        role, source.metadata, actor_attribution=actor_attribution, include_workdir=include_workdir
    )
    if not label:
        return projected
    return projected | {"content": f"{label}\n{content}"}


def _history_attribution_label(
    role: Any,
    metadata: dict[str, Any],
    *,
    actor_attribution: bool = True,
    include_workdir: bool = True,
) -> str | None:
    if role == "assistant":
        if actor_attribution:
            label = _history_agent_label(metadata)
            if label:
                return f"[assistant: {label}]"
        return None
    if role == "user":
        parts: list[str] = []
        if actor_attribution:
            target = _metadata_str(metadata, "target_display") or _metadata_str(
                metadata, "target_agent", "target_actor"
            )
            if target:
                parts.append(f"user -> {target}")
        if include_workdir:
            workdir = _metadata_str(metadata, "workdir")
            if workdir:
                parts.append(f"workdir: {workdir}")
        if parts:
            return f"[{' | '.join(parts)}]"
    return None


def _history_agent_label(metadata: dict[str, Any]) -> str | None:
    return _metadata_str(metadata, "agent_display", "actor_display") or _metadata_str(metadata, "agent_name", "actor")


def _metadata_str(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_thinking(response: Any) -> str | None:
    """Extract the best available thinking content from an LLM response."""
    if getattr(response, "reasoning_content", None):
        return response.reasoning_content
    blocks = getattr(response, "thinking_blocks", None)
    if blocks:
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("thinking"):
                parts.append(block["thinking"])
        if parts:
            return "\n".join(parts)
    if getattr(response, "content", None) and getattr(response, "tool_calls", None):
        return response.content
    return None


class AbortTurn(Exception):
    pass


class _NoopInterceptor:
    """Does nothing. The default when an Agent is constructed without an
    injected interceptor — combining plugin/configured interceptors into a
    single TurnInterceptor is the outer layer's job."""

    async def intercept(self, stage: InterceptorStage, context: TurnContext) -> None:
        return None


class _NoopPromptProvider:
    """Contributes no extra prompt sections. The default when an Agent is
    constructed without an injected provider."""

    async def sections(self, context: TurnContext) -> list[str]:
        return []


class _EmptyToolSet:
    """A ToolSet with no tools. Default when an Agent is constructed without
    an injected, resolved tool collection."""

    def has(self, name: str) -> bool:
        return False

    def to_openai_schema(self) -> dict[str, dict[str, Any]]:
        return {}

    def describe_usage(self) -> dict[str, str]:
        return {}

    def attributes(self, name: str) -> ToolAttributes:
        return ToolAttributes()

    async def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> str:
        raise Exception(f"Tool {name} not found")


class Agent:
    def __init__(
        self,
        *,
        kind: str,
        chat_store: ChatStore,
        consolidator: Consolidator,
        system_prompt: str | None = None,
        prompt_provider: PromptProvider | None = None,
        tools: ToolSet | None = None,
        tools_usage: dict[str, str] | None = None,
        model: str | None = None,
        agent_name: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        llm: LLM,
        interceptor: TurnInterceptor | None = None,
        max_tokens: int = 128 * 1024,
        max_iterations: int = 80,
        max_iteration_handoff: bool = True,
        shutdown_handoff: bool = True,
        tool_noise_filter: ToolNoiseFilter | None = None,
        chat_compaction_lock: Callable[[str], AbstractAsyncContextManager] | None = None,
        history_attribution: bool = False,
        workspace: str | os.PathLike[str] | None = None,
        structured_validator: StructuredValidator | None = None,
    ):
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string or None")
        self._system_prompt = system_prompt or ""
        # The resolved set of tools this agent may call. Resolution (merge of
        # global + plugin-local tools, plus include/exclude filtering) is done
        # by the outer layer; the entity only consumes the result.
        self._tools: ToolSet = tools or _EmptyToolSet()
        self._tools_usage = tools_usage or {}
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._max_iteration_handoff = max_iteration_handoff
        self._shutdown_handoff = shutdown_handoff
        # Cooperative stop. Set by ``request_stop`` when the host is shutting
        # down: in-flight turns abandon what they are awaiting and close with a
        # handoff instead of being cancelled mid-flight.
        self._stop_requested = asyncio.Event()
        self._llm = llm
        self._chat_store = chat_store
        self._consolidator = consolidator
        self._kind = kind
        self._name = agent_name or kind
        self._prompt_provider: PromptProvider = prompt_provider or _NoopPromptProvider()
        self._tool_noise_filter: ToolNoiseFilter | None = tool_noise_filter
        self._compaction_lock = chat_compaction_lock
        self._history_attribution = history_attribution
        self._workspace = str(workspace) if workspace else None
        self._structured_validator = structured_validator

        # The agent runs a single interceptor; assembling plugin + configured
        # interceptors into one is the outer layer's job.
        self._interceptor: TurnInterceptor = interceptor or _NoopInterceptor()

    @property
    def name(self) -> str:
        return self._name

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        """Ask in-flight turns to close early, cooperatively.

        The host calls this when it is shutting down. Each running turn
        abandons whatever it is awaiting (an LLM call, a tool batch), closes
        with a handoff summarizing what it established, persists it, and
        returns normally — so the caller still receives a reply instead of a
        cancellation. Idempotent; a turn started after this returns closes
        immediately with the static marker."""
        self._stop_requested.set()

    @staticmethod
    async def _abandon(*futures: asyncio.Future[Any]) -> None:
        """Cancel *futures* and wait a bounded moment for them to unwind.

        Two things must not happen here. The wait must not be open-ended — a
        tool that delays or swallows ``CancelledError`` would otherwise hold the
        turn past the host's deadline. And *our own* cancellation must not be
        read as the abandoned work finishing: suppressing it would let a turn
        the host has already given up on carry on into a full handoff.
        ``asyncio.wait`` gives both properties — it reports a child's outcome
        through the future rather than raising it, so a ``CancelledError``
        escaping it can only be ours. Whatever is still unwinding when the
        budget expires is left to the loop; the turn does not wait for it."""
        pending = [future for future in futures if not future.done()]
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.wait(set(pending), timeout=_ABANDON_TEARDOWN_SECONDS)
        for future in futures:
            if future.done() and not future.cancelled():
                future.exception()  # consumed, so the loop does not log it as unretrieved

    async def _await_or_stop(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Await *coro*, abandoning it if a cooperative stop is requested.

        Raises :class:`_StopRequested` instead of returning, so the turn loop
        can close with a handoff rather than waiting out a long tool call or
        model response. The abandoned work is cancelled, not left running."""
        if self._stop_requested.is_set():
            coro.close()
            raise _StopRequested
        work = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self._stop_requested.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # The whole turn is being cancelled (the hard path) — take the
            # in-flight work down with it rather than orphaning the task. No
            # wait for its unwind: the host has already stopped waiting for us.
            work.cancel()
            raise
        finally:
            stop.cancel()
        if work.done():
            return work.result()
        await self._abandon(work)
        raise _StopRequested

    async def _await_tasks_or_stop(self, tasks: Sequence[asyncio.Future[str]]) -> bool:
        """Wait for *tasks*, abandoning the unfinished ones on a cooperative stop.

        Returns whether a stop cut the wait short. Unlike ``_await_or_stop`` the
        caller keeps the individual futures, so results that landed before the
        stop are still readable — work that completed must not be reported as
        never having run."""
        if self._stop_requested.is_set():
            await self._abandon(*tasks)
            return True
        work = asyncio.ensure_future(asyncio.gather(*tasks))
        stop = asyncio.ensure_future(self._stop_requested.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            work.cancel()
            raise
        finally:
            stop.cancel()
        if work.done():
            await work  # re-raises a tool failure, as the plain gather did
            return False
        await self._abandon(work, *tasks)
        return True

    async def ask(
        self,
        chat_id: str,
        content: MessageContent,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: TurnEventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
    ) -> str:
        """Run a turn and return the final response text.

        Thin wrapper over :meth:`run`; the full result (token usage, iteration
        count, structured output) is available via ``run`` directly."""
        result = await self.run(
            chat_id,
            content,
            interrupt=interrupt,
            ctx_metadata=ctx_metadata,
            llm_args=llm_args,
            event_sink=event_sink,
            turn_id=turn_id,
            commit_observer=commit_observer,
        )
        return result.output

    async def run(
        self,
        chat_id: str,
        content: MessageContent,
        *,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
        ctx_metadata: dict[str, Any] | None = None,
        llm_args: dict[str, Any] | None = None,
        event_sink: TurnEventSink | None = None,
        turn_id: str | None = None,
        commit_observer: Callable[[Any], Any | Awaitable[Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_schema_retries: int = 1,
    ) -> AgentResult:
        llm_params = {
            "model": self._model,
            "reasoning_effort": self._reasoning_effort,
        } | (llm_args or {})
        if schema is not None:
            # Provider-native structured-output hint; local validation below is
            # authoritative (BEP 12). Sanitized so non-OpenAI providers accept it.
            llm_params["response_schema"] = provider_hint_schema(schema)
        budget_model = llm_params.get("model")

        ctx = TurnContext(
            agent_name=self._name,
            chat_id=chat_id,
            turn_id=turn_id or uuid.uuid4().hex,
            history=await self._load_and_compact_history(chat_id, budget_model=budget_model),
            tool_defs=self._get_tool_defs(),
            event_sink=event_sink,
            metadata=(ctx_metadata or {}).copy(),
            current_message_projector=self._project_current_message,
        )
        user_message_metadata = ctx.metadata.get("user_message_metadata")
        ctx.add_message(
            {"role": "user", "content": content or ""},
            **(user_message_metadata if isinstance(user_message_metadata, dict) else {}),
        )
        turn_status: Literal["running", "completed", "aborted", "error"] = "running"
        abort_reason: str | None = None

        cache_index = 0
        iteration = 0
        usage_total: dict[str, int] = {}
        structured_output: Any = None
        structured_ok = False
        schema_retries = 0

        def _message_metadata(message: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
            if metadata is not None:
                return metadata
            if message.get("role") == "assistant" and isinstance(ctx.metadata.get("assistant_message_metadata"), dict):
                return ctx.metadata["assistant_message_metadata"]
            return None

        def _add_message(message: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
            nonlocal cache_index
            ctx.add_message(_compact(message), **(_message_metadata(message, metadata) or {}))
            cache_index -= 1

        def _cache_control_injection_points() -> list[dict[str, Any]]:
            hints: list[dict[str, Any]] = [{"location": "message", "role": "system"}]
            if cache_index == 0:
                return hints
            # ctx.ephemeral is appended after persisted/current messages in the
            # LLM input, so negative indexes that target current/history messages
            # must be shifted left by the ephemeral tail length.
            hints.append({"location": "message", "index": cache_index - len(ctx.ephemeral)})
            return hints

        def _aborted_turn_messages(reason: str | None) -> list[Message]:
            user_messages = [message for message in ctx.current if message.llm_message.get("role") == "user"]
            if not user_messages:
                return []
            return [
                *user_messages,
                Message(
                    llm_message={"role": "assistant", "content": ABORTED_TURN_CONTENT},
                    turn_id=ctx.turn_id,
                    metadata={
                        "turn_status": "aborted",
                        "aborted": True,
                        "abort_reason": reason or "unknown",
                    },
                ),
            ]

        def _has_final_assistant_response() -> bool:
            if ctx.final_content is None or not ctx.current:
                return False
            last_message = ctx.current[-1].llm_message
            return (
                last_message.get("role") == "assistant"
                and not last_message.get("tool_calls")
                and last_message.get("content") == ctx.final_content
            )

        async def _persist_turn() -> None:
            if turn_status == "aborted" and not _has_final_assistant_response():
                messages = _aborted_turn_messages(abort_reason)
            else:
                messages = ctx.current
            if messages:
                commit = await self._chat_store.commit_turn(chat_id, messages, turn_id=ctx.turn_id)
                if commit_observer is not None:
                    result = commit_observer(commit)
                    if inspect.isawaitable(result):
                        await result

        async def _run_interceptor(stage: InterceptorStage):
            try:
                await self._interceptor.intercept(stage, ctx)
            except AbortTurn:
                raise
            except Exception as e:
                logger.error(
                    "Error in interceptor: [chat_id: %s, turn_id: %s, stage: %s] %s",
                    ctx.chat_id,
                    ctx.turn_id,
                    stage,
                    e,
                    exc_info=True,
                )

        async def _emit_event(
            event_type: str,
            phase: str,
            *,
            stage: str | None = None,
            detail: str | None = None,
            tool_name: str | None = None,
            content: str | None = None,
            summary: str | None = None,
            tool_calls: list[dict[str, Any]] | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            if event_sink is None:
                return
            try:
                await event_sink.emit(
                    TurnEvent(
                        event_type=event_type,
                        phase=phase,
                        chat_id=ctx.chat_id,
                        turn_id=ctx.turn_id,
                        agent_name=self._name,
                        stage=stage,
                        detail=detail,
                        tool_name=tool_name,
                        content=content,
                        summary=summary,
                        tool_calls=tool_calls,
                        metadata=metadata or {},
                    )
                )
            except Exception:
                logger.debug("Event sink emit error", exc_info=True)

        async def _interrupt():
            if interrupt and (llm_message := await _apply_async(interrupt, {})):
                ctx.add_message(llm_message, merge=True)

        async def _close_with_handoff(reason: Literal["max_iterations", "shutdown"]) -> None:
            """End the turn early with a handoff instead of a bare marker.

            The model never got to write a final answer, so closing on the
            static marker alone would throw away everything the turn
            established. Consolidate that context into an actionable handoff
            and answer with it; the marker stays as the header and as the
            fallback when consolidation is disabled, fails, or has nothing to
            summarize."""
            handoff = await self._consolidate_closure_handoff(ctx, reason=reason)
            final_content = _closure_response(reason, handoff)
            _add_message({"role": "assistant", "content": final_content})
            ctx.final_content = final_content
            stage = TurnEventStage.shutdown if reason == "shutdown" else TurnEventStage.max_iteration
            detail = TurnEventDetail.shutdown if reason == "shutdown" else TurnEventDetail.max_iteration
            await _run_interceptor("shutdown" if reason == "shutdown" else "max_iteration")
            await _emit_event(
                AgentEventType.turn,
                TurnEventPhase.fail,
                stage=stage,
                detail=detail,
                content=final_content,
                metadata={
                    "iteration": iteration,
                    "max_iterations": self._max_iterations,
                    "handoff": handoff is not None,
                    "closure_reason": reason,
                },
            )

        try:
            await _run_interceptor("prepare")
            # Build the system prompt once per turn (after prepare). It is not
            # rebuilt per iteration: intra-turn state the agent produces (e.g. a
            # memory write) is already present in the turn's message context, so
            # re-rendering it into the system prompt mid-turn is redundant and
            # would needlessly churn the prompt-cache prefix.
            ctx.set_system_prompt(await self._build_system_prompt(ctx))
            await _emit_event(
                AgentEventType.turn,
                TurnEventPhase.start,
                stage=TurnEventStage.prepare,
                detail=TurnEventDetail.start,
            )
            while True:
                if iteration >= self._max_iterations:
                    await _close_with_handoff("max_iterations")
                    break
                if self._stop_requested.is_set():
                    # The host is shutting down between iterations. Close on
                    # what this turn already established rather than being
                    # cancelled and losing it.
                    await _close_with_handoff("shutdown")
                    break
                iteration += 1
                await _interrupt()
                await _run_interceptor("before_llm")
                await _emit_event(
                    AgentEventType.llm,
                    TurnEventPhase.start,
                    stage=TurnEventStage.before_llm,
                    detail=TurnEventDetail.thinking,
                    metadata={"iteration": iteration, "max_iterations": self._max_iterations},
                )

                # Interruptible: a stop mid-completion abandons the call rather
                # than making shutdown wait out a long response. Nothing has
                # been added to the context yet, so there is nothing to repair.
                ctx.current_llm_response = response = await self._await_or_stop(
                    self._llm.complete(
                        ctx.get_llm_messages(),
                        tools=ctx.tool_defs,
                        cache_control_injection_points=_cache_control_injection_points(),
                        **llm_params,
                    )
                )
                cache_index = -1
                for _k, _v in (response.usage or {}).items():
                    if isinstance(_v, int):
                        usage_total[_k] = usage_total.get(_k, 0) + _v
                await _run_interceptor("after_llm")

                # Emit one unified LLM response event carrying both the
                # model's thinking content and any tool calls.
                thinking = _resolve_thinking(response)
                await _emit_event(
                    AgentEventType.llm,
                    TurnEventPhase.finish,
                    stage=TurnEventStage.after_llm,
                    detail=TurnEventDetail.tool_calls if response.tool_calls else TurnEventDetail.response_ready,
                    content=thinking,
                    tool_calls=(
                        [{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls]
                        if response.tool_calls
                        else None
                    ),
                    metadata=(
                        {"finish_reason": response.finish_reason, "usage": response.usage}
                        if response.usage
                        else {"finish_reason": response.finish_reason}
                    ),
                )
                if not response.tool_calls:
                    # finalize the response if there is no tool call required
                    final_content = _strip_reply_artifacts(response.content, strip_labels=self._history_attribution)
                    if response.finish_reason == "error":
                        logger.error("Error in LLM response: %s", response.finish_reason)
                        final_content = final_content or "(LLM responds error)"
                    else:
                        final_content = final_content or "(empty model response)"
                    _add_message(
                        {
                            "role": "assistant",
                            "content": final_content,
                            "reasoning_content": response.reasoning_content,
                            "thinking_blocks": response.thinking_blocks,
                        },
                        metadata=(
                            ctx.metadata.get("assistant_message_metadata")
                            if isinstance(ctx.metadata.get("assistant_message_metadata"), dict)
                            else None
                        ),
                    )
                    if schema is not None and not structured_ok:
                        if response.finish_reason in UNUSABLE_FINISH_REASONS:
                            # Truncation / content filter / provider error — a
                            # "reply in JSON" retry cannot repair it; surface.
                            raise StructuredOutputError(
                                f"no usable completion for structured output "
                                f"(finish_reason={response.finish_reason!r})"
                            )
                        try:
                            if self._structured_validator is not None:
                                structured_output = self._structured_validator.validate(final_content, schema)
                            else:
                                structured_output = parse_json(final_content)
                            structured_ok = True
                        except StructuredOutputError as e:
                            if schema_retries < max_schema_retries:
                                schema_retries += 1
                                _add_message({
                                    "role": "user",
                                    "content": (
                                        f"Your previous response failed schema validation: {e}. "
                                        "Reply ONLY with JSON matching the schema."
                                    ),
                                })
                                continue
                            raise
                    ctx.final_content = final_content
                    await _run_interceptor("final_response")
                    await _emit_event(
                        AgentEventType.response,
                        TurnEventPhase.finish,
                        stage=TurnEventStage.final_response,
                        detail=TurnEventDetail.final,
                        content=final_content,
                    )
                    break

                # call tools and send bake to the llm
                tool_call_dicts = [tc.to_openai_call() for tc in response.tool_calls]
                _add_message({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": tool_call_dicts,
                    "reasoning_content": response.reasoning_content,
                    "thinking_blocks": response.thinking_blocks,
                })

                answered: set[str] = set()
                stopped_in_tools = False
                for batch in self._tool_call_batches(response.tool_calls):
                    for tc in batch:
                        await _emit_event(
                            AgentEventType.tool,
                            TurnEventPhase.start,
                            detail=TurnEventDetail.tool_call,
                            tool_name=tc.name,
                            content=json.dumps(tc.arguments, default=str),
                        )
                    # Interruptible: a stop abandons whatever is still running
                    # instead of waiting out a long tool call.
                    tool_tasks: list[asyncio.Future[str]] = [
                        asyncio.ensure_future(self._call_tool(tc, ctx, event_sink=event_sink)) for tc in batch
                    ]
                    stopped_in_tools = await self._await_tasks_or_stop(tool_tasks)
                    # Record every call that produced a result, stop or no stop.
                    # A parallel batch is abandoned as a unit, but a call that
                    # already returned has already had its effect — calling it
                    # unfinished invites the next turn to repeat it.
                    for tc, task in zip(batch, tool_tasks):
                        if not task.done() or task.cancelled() or task.exception() is not None:
                            continue
                        tool_result = task.result()
                        answered.add(tc.id)
                        _add_message({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": tool_result,
                        })
                        await _run_interceptor("after_tool")
                        await _emit_event(
                            AgentEventType.tool,
                            TurnEventPhase.finish,
                            stage=TurnEventStage.after_tool,
                            detail=TurnEventDetail.tool_result,
                            tool_name=tc.name,
                            content=tool_result,
                        )
                    if stopped_in_tools:
                        break
                if stopped_in_tools:
                    # The assistant message already advertises every tool call,
                    # so each one needs a result message or the persisted turn
                    # is an invalid sequence the next turn cannot replay.
                    for tc in response.tool_calls:
                        if tc.id in answered:
                            continue
                        _add_message({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": INTERRUPTED_TOOL_CONTENT,
                        })
                    await _close_with_handoff("shutdown")
                    break
            turn_status = "completed"
        except _StopRequested:
            # Raised by the LLM call, where the context is still clean.
            await _close_with_handoff("shutdown")
            turn_status = "completed"
        except AbortTurn:
            turn_status = "aborted"
            abort_reason = "abort_turn"
            ctx.final_content = ABORTED_TURN_CONTENT
        except asyncio.CancelledError:
            turn_status = "aborted"
            abort_reason = "cancelled"
            raise
        except StructuredOutputError:
            # Structured-output failure is the caller's to handle (BEP 12):
            # persist what we have, then propagate rather than masking it as a
            # normal "(error: …)" assistant turn.
            turn_status = "error"
            raise
        except Exception as e:
            turn_status = "error"
            logger.error("Error in agent: %s", e, exc_info=True)
            ctx.add_message({"role": "assistant", "content": f"(error: {e})"})
            await _run_interceptor("error")
            await _emit_event(AgentEventType.turn, TurnEventPhase.fail, detail=TurnEventDetail.error, content=str(e))
        finally:
            await _persist_turn()

        finish_reason = ctx.current_llm_response.finish_reason if ctx.current_llm_response else None
        if schema is not None and structured_ok:
            return AgentResult(
                output=structured_output,
                structured=True,
                iterations=iteration,
                usage=usage_total,
                turn_id=ctx.turn_id,
                finish_reason=finish_reason,
            )
        return AgentResult(
            output=ctx.final_response,
            structured=False,
            iterations=iteration,
            usage=usage_total,
            turn_id=ctx.turn_id,
            finish_reason=finish_reason,
        )

    def _get_tool_defs(self) -> list[dict[str, Any]]:
        return list(self._tools.to_openai_schema().values())

    def _tool_parallel_safe(self, tool_name: str) -> bool:
        return self._tools.attributes(tool_name).parallel_safe

    def _tool_call_batches(self, tool_calls: Sequence[ToolCallRequest]) -> list[list[ToolCallRequest]]:
        batches: list[list[ToolCallRequest]] = []
        pending_safe: list[ToolCallRequest] = []
        for tc in tool_calls:
            if self._tool_parallel_safe(tc.name):
                pending_safe.append(tc)
                continue
            if pending_safe:
                batches.append(pending_safe)
                pending_safe = []
            batches.append([tc])
        if pending_safe:
            batches.append(pending_safe)
        return batches

    async def _call_tool(self, tc: ToolCallRequest, ctx: TurnContext, event_sink: TurnEventSink | None = None) -> str:
        try:
            return await self._invoke_tool(
                tc.name,
                **tc.arguments,
                chat_id=ctx.chat_id,
                turn_id=ctx.turn_id,
                event_sink=event_sink,
            )
        except Exception as e:
            logger.error("Error in tool call [%s]: %s", tc.name, e)
            return str(e)

    async def _invoke_tool(self, tool_name: str, **params: Any) -> str:
        """Invoke a tool by name, folding the runtime turn linkage into a single
        ``ToolContext``. The ``chat_id``/``turn_id``/``event_sink`` carried in
        ``params`` are the runtime injection from ``_call_tool``; they are lifted
        into ``context`` and never exposed to tools as flat parameters — a tool
        reads them via ``context.parent.*``."""
        context = ToolContext(
            parent=ParentTurn(
                chat_id=params.pop("chat_id", ""),
                turn_id=params.pop("turn_id", ""),
                agent_name=self._name,
                event_sink=params.pop("event_sink", None),
            ),
        )
        if self._tools.has(tool_name):
            return await self._tools.invoke(tool_name, params | {"context": context})
        raise Exception(f"Tool {tool_name} not found")

    async def _consolidate_closure_handoff(
        self, ctx: TurnContext, *, reason: Literal["max_iterations", "shutdown"]
    ) -> str | None:
        """Summarize the turn's whole context into a handoff message.

        Runs when a turn is cut short — the iteration budget ran out, or the
        host asked it to stop — so the model never got to write a final answer
        and the consolidator writes one from what the turn actually established
        (history plus this turn's user/assistant/tool messages).

        Best-effort — a disabled flag, a turn that established nothing yet, a
        consolidator failure, or an empty summary returns ``None`` and the
        caller falls back to the static marker rather than failing the turn.
        """
        if not (self._max_iteration_handoff if reason == "max_iterations" else self._shutdown_handoff):
            return None
        # A turn stopped before it produced anything has nothing to hand off;
        # spending a consolidator call on the user message alone would only
        # paraphrase the request back. The marker is the honest answer.
        if not any(m.llm_message.get("role") in ("assistant", "tool") for m in ctx.current):
            return None
        # ctx.history is already provider-projected; re-wrap it as Message so the
        # consolidator sees one uniform sequence (it reads llm_message only).
        messages = [Message(llm_message=m, turn_id=ctx.turn_id) for m in ctx.history] + list(ctx.current)
        if not messages:
            return None
        try:
            handoff = await self._consolidator.consolidate(messages, instruction=_handoff_instruction(reason))
        except Exception as e:
            logger.error(
                "Closure handoff consolidation failed [chat_id: %s, turn_id: %s, reason: %s]: %s",
                ctx.chat_id,
                ctx.turn_id,
                reason,
                e,
                exc_info=True,
            )
            return None
        return handoff.strip() or None if isinstance(handoff, str) else None

    async def _load_and_compact_history(self, chat_id: str, *, budget_model: str | None) -> list[dict[str, Any]]:
        result = await self._chat_store.get_context(
            chat_id, tokenizer_model=budget_model, filter_mode=self._tool_noise_filter
        )
        if result.estimated_tokens > self._max_tokens and self._compaction_lock is not None:
            async with self._compaction_lock(chat_id):
                # Re-check after acquiring lock (race guard)
                result = await self._chat_store.get_context(
                    chat_id, tokenizer_model=budget_model, filter_mode=self._tool_noise_filter
                )
                if result.estimated_tokens > self._max_tokens:
                    compaction_msgs = await self._chat_store.get_compaction_messages(
                        chat_id, filter_mode=self._tool_noise_filter
                    )
                    summary = await self._consolidator.consolidate(compaction_msgs)
                    await self._chat_store.save_summary(chat_id, summary)
                    result = await self._chat_store.get_context(
                        chat_id, tokenizer_model=budget_model, filter_mode=self._tool_noise_filter
                    )
        return self._format_history(result)

    def _project_current_message(self, message: Message) -> dict[str, Any]:
        """Render attribution labels on current-turn user messages sent to the LLM.

        Mirrors the history projection, except the workdir label always
        renders here so the latest message carries the active workdir even
        when history suppresses repeats (:meth:`_format_history`). Stored
        content stays raw; the label is re-derived from metadata on every
        projection.
        """
        llm_message = message.llm_message
        if llm_message.get("role") != "user" or not isinstance(llm_message.get("content"), str):
            return llm_message
        label = _history_attribution_label("user", message.metadata, actor_attribution=self._history_attribution)
        if not label:
            return llm_message
        return llm_message | {"content": f"{label}\n{llm_message['content']}"}

    def _format_history(self, result: ContextResult) -> list[dict[str, Any]]:
        """Hook for subclasses to customise history projection (e.g. attribution labels).

        Actor labels (``[user -> X]``, ``[assistant: X]``) render only when
        ``history_attribution`` is enabled. Workdir labels render only when the
        workdir changes between user messages — repeating an unchanged workdir
        on every message is noise; the current-turn projection always carries
        the active workdir (see :meth:`_project_current_message`).
        """
        if len(result.messages) != len(result.source_messages):
            return result.messages
        formatted: list[dict[str, Any]] = []
        last_workdir: str | None = None
        for projected, source in zip(result.messages, result.source_messages, strict=True):
            include_workdir = False
            if projected.get("role") == "user":
                workdir = _metadata_str(source.metadata, "workdir")
                include_workdir = workdir is not None and workdir != last_workdir
                last_workdir = workdir
            formatted.append(
                _attribute_history_message(
                    projected,
                    source,
                    current_agent=self._name,
                    actor_attribution=self._history_attribution,
                    include_workdir=include_workdir,
                )
            )
        return formatted

    async def _build_system_prompt(self, ctx: TurnContext) -> str:
        system_sections = [self._system_prompt, *await self._prompt_provider.sections(ctx)]
        sections = [
            self._prompt_section_base(system_sections),
            await self._prompt_section_tools(),
            self._prompt_section_system_info(),
        ]
        return "\n\n".join(s for s in sections if s)

    def _prompt_section_base(self, sections: Sequence[str] | None = None) -> str:
        content = "\n\n".join(s for s in (sections or [self._system_prompt]) if s)
        return f"<system_prompt>\n{content}\n</system_prompt>"

    async def _prompt_section_tools(self) -> str:
        available_tools = self._limit_prompt_collection(self._tools.describe_usage(), "tools")
        available_tools = {k: (self._tools_usage.get(k, v) or "").strip() for k, v in available_tools.items()}

        if not available_tools:
            return "<available_tools>\n\n</available_tools>"
        section = "<available_tools>\n"
        section += "\n\n".join(
            f'<tool name="{_xml_attr(name)}">\n{escape(str(content)).strip()}\n</tool>'
            for name, content in available_tools.items()
        )
        section += "\n</available_tools>"
        return section

    def _prompt_section_system_info(self) -> str:
        workspace_line = f"<workspace>{escape(self._workspace)}</workspace>\n" if self._workspace else ""
        return (
            "<system_info>\n"
            f"<platform>{platform.system()}</platform>\n"
            f"<date>{datetime.now().strftime('%A, %B %d, %Y')}</date>\n"
            f"{workspace_line}"
            "</system_info>"
        )

    @staticmethod
    def _limit_prompt_collection(collection: dict[str, Any], kind: str) -> dict[str, Any]:
        try:
            limit = int(os.environ.get("BOS_CAPABILITY_LIMIT", 50))
        except Exception:
            limit = 50
            logger.warning("Env variable BOS_CAPABILITY_LIMIT should be a valid integer number.")

        if len(collection) <= limit:
            return collection
        logger.warning(
            "Rendering only the first %d %s in the system prompt; %d are available.",
            limit,
            kind,
            len(collection),
        )
        return dict(list(collection.items())[:limit])
