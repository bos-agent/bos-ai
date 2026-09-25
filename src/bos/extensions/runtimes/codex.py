"""The Codex vendor runtime (BEP 19 Layer 4a).

``CodexAgent`` is BOS's ``ExternalRuntime`` adapter over ``openai_codex.AsyncCodex``,
the vendor SDK that talks to a lazily-spawned ``codex app-server`` child process.
It implements ``AgentPort`` without being a ``bos.core.agent.Agent``: it holds no
``LLM``, runs none of BOS's turn loop, and its permission enforcement is the
vendor's own OS-level sandbox rather than anything BOS assembles — BEP 19 §3.5.2:
"This is real confinement, and it is the one BOS does not have to build."

This module imports ``openai_codex`` at module scope. That is safe here, and only
here: it is reached exclusively through ``importlib``, from
``bos.core.harness._load_external_runtime``. Nothing under ``bos/core/`` or
``bos/sdk/`` may import this module or the vendor package directly, so a base
install with neither extra never touches either.

Stage 4 of 4 over ``CodexAgent``'s turn path: construction (Task 3), the
client and thread lifecycle (Task 4), a turn that runs and persists itself
including schema-validated structured output (BEP 12 semantics, via the
injected ``StructuredValidator`` — BEP 19 §3.2, §3.9) (Task 5), that turn made
observable via ``_emit_stream``, which streams ``AsyncTurnHandle.stream()``
into BOS ``TurnEvent``s as they arrive rather than awaiting one final result
(BEP 19 §3.9) (Task 6), and here — the control surface: ``_run_turn`` races
each native turn against a cooperative stop and ``timeout_seconds``, telling
apart a caller's deadline (raised), BOS taking the turn away (kept, partial),
and an unexplained vendor-side interruption (raised, as before), even though
all three reach here as the same ``TurnStatus.interrupted``; ``run()``'s
``self._in_flight`` busy guard rejects a second turn on a chat_id already
running one; and ``aclose()`` interrupts every in-flight turn with a bounded
wait before closing the client regardless (BEP 19 §3.10.2) (Task 7); and
finally ``_deny_approval``, installed over the vendor's auto-accepting
default so an escalation past the sandbox is refused rather than granted
(BEP 19 §3.5.4) (Task 8).

Beside that turn path — not a stage of it — sits ``native_messages`` (Task 9,
BEP 19 §3.7): the read that projects Codex's *own* thread back into BOS
``Message``s, so a host can render a session BOS did not author. It is the
only method here that reads state BOS does not own, and it runs no turn and
resumes no session. It is not side-effect-free, though, and the docstring
there says so: it goes through ``_ensure_client`` like everything else, so on
a cold agent the read is what spawns the ``codex app-server`` child, and under
the default ``auth="subscription"`` it can fail on the ``account()`` preflight
— a failure about the login, not about the transcript.

The other thing hanging off ``_ensure_client`` is ``_mcp_egress_config`` (Task
10, BEP 19 §3.8), which hands the host's selected ``ep_tool``s to Codex: it
starts BOS's one loopback MCP server and tells every thread of this agent to
connect to it, carrying a per-agent bearer token. Everything else this module
sends Codex is a setting or a prompt; this is the one thing the model can
*call*. It stays lazy the whole way down: an agent with nothing servable —
no ``mcp_tools``, or none the host has an ``ep_tool`` for — never so much as
asks the harness for a server, so no port is bound on its account.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import mimetypes
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, TypeVar, cast

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    ImageInput,
    Input,
    InputItem,
    LocalImageInput,
    MentionInput,
    Sandbox,
    TextInput,
    TurnResult,
)
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CommandExecutionThreadItem,
    ImageUserInput,
    LocalImageUserInput,
    McpToolCallThreadItem,
    MentionUserInput,
    MessagePhase,
    TextUserInput,
    TurnItemsView,
    UserInput,
    UserMessageThreadItem,
)
from openai_codex.models import ItemCompletedNotification, ItemStartedNotification, JsonObject, Notification
from openai_codex.types import (
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)

from bos.core.agent import (
    ABORTED_TURN_CONTENT,
    AbortTurn,
    AgentEventType,
    AgentResult,
    ChatStore,
    Message,
    MessageContent,
    MessageContentPart,
    StructuredOutputError,
    StructuredValidator,
    TurnEvent,
    TurnEventPhase,
    TurnEventSink,
    _apply_async,
    _compact,
    content_as_parts,
)
from bos.extensions.runtimes._shared import (
    commit_external_turn,
    external_agent_result,
    parse_external_config,
    read_native_session_id,
)

# Safe at module scope: `mcp_egress` defers every third-party import into the
# method that needs it, so naming it here costs nothing and requires no extra.
from bos.extensions.runtimes.mcp_egress import unregistered_tools

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Patched in tests to inject FakeAsyncCodex — the only seam the double needs.
_CODEX_FACTORY: Callable[..., Any] = AsyncCodex

# BEP 19 §3.5: Codex's confinement is an OS sandbox the vendor enforces, so the
# permission story is these two enums plus `_APPROVAL_DENIALS` below — no
# BOS-side tool interception like Claude Code needs (§3.5.2 vs §3.5.3).
# `full-access` still uses `auto_review`, not because approvals matter once the
# sandbox is open, but because `deny_all` would block turns from proceeding at
# all.
_SANDBOX_AND_APPROVAL: dict[str, tuple[Sandbox, ApprovalMode]] = {
    "read-only": (Sandbox.read_only, ApprovalMode.deny_all),
    "workspace-write": (Sandbox.workspace_write, ApprovalMode.auto_review),
    "full-access": (Sandbox.full_access, ApprovalMode.auto_review),
}

# The `rejection` text the two legacy approval methods send back, shared so
# both carry the same answer. Model-facing: the agent reads it mid-turn, so it
# says why the request was refused and what the agent can still do, in the
# agent's own terms — no BOS vocabulary, no section numbers.
_LEGACY_REJECTION = (
    "Denied: this agent runs unattended and has no way to ask a person to approve anything "
    "beyond its sandbox, so every such request will be refused. Continue with what the "
    "sandbox already allows."
)

# BEP 19 §3.5.4: the refusal for every approval request the protocol defines,
# and the whole of BOS's Codex approval policy. Keyed only by method, with no
# `permission` branch, because there is no level at which BOS can say yes:
#
# - The sandbox above is the real boundary and is already set per turn from
#   `permission`. An approval request only ever arrives to escalate *past* it,
#   and BOS has no channel that carries the question to a human and an answer
#   back (§2.2.2), so "no" is the only answer it can honestly give — under
#   `full-access` too, where full access is what the *sandbox* grants and a
#   request to go beyond it is still unanswerable.
# - Refusing them all does not disarm a working agent: `auto_review` maps to
#   `AskForApproval(on_request)` (`openai_codex/_approval_mode.py:29-33`), so
#   the server asks only when the agent asks to escalate; `deny_all` maps to
#   `AskForApproval(never)` (:34-35), where it does not ask at all and these
#   are a backstop that should never fire.
#
# There is no `"deny"` decision in this protocol — each method spells refusal
# its own way, and returning one would be a protocol violation, since
# `CodexClient._reader_loop` writes whatever the handler returns straight back
# as the JSON-RPC `result`. Values below read out of the schema the shipped
# binary generates (`codex app-server generate-json-schema`):
#
# A refused escalation is not a turn failure: the agent is told no and left to
# finish with what the sandbox already allows — the same reasoning as
# `Agent._call_tool` (agent.py:900-911) returning the error string rather than
# raising. Four of the five vocabularies put that as a choice, offering a
# refusal that stops the turn and a refusal that lets the agent carry on, and
# each of those four entries picks the second. The fifth,
# `item/permissions/requestApproval`, offers no decision at all — its response
# is a granted-permission profile, not a verdict — so refusing it means
# granting nothing, and the agent carries on by construction. Method by method:
#
# - The two `requestApproval` methods share a vocabulary where `decline` is
#   "refused, the agent continues the turn" and `cancel` is "refused, and the
#   turn is immediately interrupted". Hence `decline`.
# - `item/permissions/requestApproval` answers with a granted-permission
#   profile rather than a decision. `GrantedPermissionProfile` has only the
#   optional, nullable `fileSystem` and `network`, so `{}` is a valid profile
#   that grants nothing.
# - The two legacy methods use the older `ReviewDecision`, whose two refusals
#   are `abort` ("the agent should not do anything until the user's next
#   command") and `DeniedReviewDecision` ("should not execute it, but it
#   should continue the session and try something else"). `denied` is the
#   legacy analogue of `decline` and `abort` the analogue of `cancel`, so
#   `denied` — and it is the one refusal in this protocol that carries text
#   back to the model, which is the point of choosing it: an agent told why
#   can adapt, where one told only "no" cannot. The `decision` wrapper is
#   required and `DeniedReviewDecision` is `additionalProperties: false`, so
#   the whole response is exactly the three nested keys below.
_APPROVAL_DENIALS: dict[str, JsonObject] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}},
    "execCommandApproval": {"decision": {"denied": {"rejection": _LEGACY_REJECTION}}},
    "applyPatchApproval": {"decision": {"denied": {"rejection": _LEGACY_REJECTION}}},
}

# How long a turn (or aclose(), across all of them) waits for the native side
# to confirm an interrupt before giving up on it — mirrors
# Agent._ABANDON_TEARDOWN_SECONDS (agent.py) and the same reasoning: a codex
# app-server that delays or ignores the request must not be able to hold a
# stop, a timeout, or aclose() open (BEP 19 §3.10.2).
_INTERRUPT_GRACE_SECONDS = 2.0

# The outer bound aclose() puts on draining every in-flight turn. Must stay
# above _settle_interrupted's worst case (3 x the grace: the interrupt RPC,
# the drain wait, the post-cancel wait), or aclose() reports turns that are
# winding down normally as stuck.
_ACLOSE_GRACE_SECONDS = 10.0

# The bound on the auth preflight's account() RPC. Deliberately NOT one of the
# two above: those are teardown graces, measured against a turn that is already
# being given up on, while this is a startup credential check against a live
# service — a slow but working login must not trip it. Bounded at all because
# account() is the same unbounded request path as interrupt(): account() ->
# account_read -> _call_sync -> asyncio.to_thread -> CodexClient._request_raw
# -> `waiter.get()` (client.py:382), a queue read with no timeout. And
# _preflight_auth holds _client_lock while it waits, which is the lock
# aclose() needs before it can reach client.close() — so an unanswered
# credential check would otherwise hold the whole harness shutdown open and
# leave the child unreaped. What the bound frees is the event loop and the
# lock; the to_thread worker stays parked on that queue until the process
# ends, exactly as it does for a wedged interrupt(). That is a thread, not a
# turn, and it is the vendor's to fix.
_PREFLIGHT_AUTH_SECONDS = 30.0

# The `Turn.items_view` values under which `Turn.items` is the turn's real,
# complete content and `native_messages` may project it (BEP 19 §3.7). Three
# entries for two states, and the middle one is the whole reason this is a
# membership test rather than `is TurnItemsView.full`:
#
# - `TurnItemsView.full` — what the field holds when the server sends
#   `"itemsView": "full"` and pydantic validates it into the enum.
# - the bare string `"full"` — `Turn.items_view`'s own *default*, and pydantic
#   does not validate defaults, so a payload that omits `itemsView` (which
#   `Turn` permits, the field having a default) leaves the raw `str` in place.
#   Verified against the installed package: `Turn.model_validate({...})` with
#   no `itemsView` really does come back as `'full'`, not `TurnItemsView.full`,
#   and `TurnItemsView` is a plain `Enum`, so the two are not even `==`. An
#   identity check against the enum would call every such turn partial and
#   bury a complete transcript under gap markers.
# - `None` — the field is typed `TurnItemsView | None`. An explicit null
#   states nothing about loading, and the vendor's own answer for "not stated"
#   is the `"full"` default above, so this follows it rather than inventing a
#   third verdict.
#
# The complement is `notLoaded` and `summary`, both of which mean the items are
# absent or summarized — see `native_messages` for what is emitted for them.
# That complement is exhaustive *today*, not by construction, and the exception
# is worth naming: `TurnItemsView` is a closed Enum, so a value a future
# `openai-codex` adds never reaches the marker branch at all. It fails
# `ThreadReadResponse` validation inside the vendor's own `thread_read`, which
# kills the whole read rather than degrading one turn (BEP 19 §8.2).
_LOADED_ITEMS_VIEWS: tuple[TurnItemsView | str | None, ...] = (TurnItemsView.full, TurnItemsView.full.value, None)

# The mime type a `MentionUserInput` read back out of a Codex thread becomes
# when `mimetypes` cannot guess one from the path. BOS's `FilePart` requires a
# non-empty `mime_type` and Codex's mention carries none, so something has to
# be supplied; this is the value that says "unknown" rather than claiming a
# type Codex never reported.
_UNKNOWN_FILE_MIME_TYPE = "application/octet-stream"

# The rest of the audit those three constants are half of, stated once so the
# next reader does not have to redo it — and stated as it is, not as "every
# wait is bounded", which was the round-2 prose that made an unbounded
# account() a finding rather than a known gap:
#
# - `client.close()` bounds itself: CodexClient.close is stdin.close() ->
#   proc.terminate() -> proc.wait(timeout=2) -> kill() -> two
#   join(timeout=0.5), ~3s worst case (measured 2.003s against a child that
#   ignores SIGTERM). Two ways the wall clock can still exceed that, both
#   remote enough to leave alone: stdin.close() flushes and can block on a
#   full pipe with nothing draining it, and AsyncCodexClient.close runs on
#   the DEFAULT to_thread executor, in which every wedged RPC above parks a
#   worker (~32 to starve it).
# - The stream iteration and `handle.steer()` sit inside _run_turn's
#   `asyncio.timeout(timeout_seconds)` AND are raced against _stop_requested,
#   so _settle_interrupted's cancel bounds them even when timeout_seconds is
#   None.
# - `thread_start` / `thread_resume` / `thread.turn` are awaited *outside*
#   that timeout window — they run before _run_turn exists — so each carries
#   its own `wait_for(..., timeout_seconds)` instead (_bounded_setup, fix
#   round 4). Per *attempt*, which is what timeout_seconds already means
#   here: the schema-retry loop gives every attempt a fresh asyncio.timeout,
#   so a turn with retries can already take a multiple of it. It is not a
#   whole-call deadline, and round 4 did not make it one.
# - With `timeout_seconds = None` nothing above is bounded by it, because the
#   caller declined a deadline. A wedged setup is still recoverable: the
#   busy-guard slot is still None while setup runs, so aclose() waits on no
#   task, takes the uncontended _client_lock and closes the client — which
#   fails the pending RPC.
# - `native_messages`' `thread.read()` (Task 9) is not a turn and is not in
#   the list above, but it is the same unbounded request path
#   (`_call_sync` -> `asyncio.to_thread` -> `waiter.get()`), so it carries its
#   own `wait_for(..., timeout_seconds)`. Unlike the three setup RPCs it holds
#   no lock and no in-flight slot while it waits — `_ensure_client` releases
#   `_client_lock` before returning it — so a wedged read blocks only its own
#   caller; bounded anyway because "the host asked for a chat's history and
#   never got an answer" is its own failure.
# - `_deny_approval` (Task 8) adds no await to any of the above — it is
#   synchronous and returns a dict lookup. It does add the one place BOS code
#   runs on the vendor's stdout reader thread rather than the event loop, and
#   nothing above can bound *that*: the reader thread is the sole consumer of
#   the child's stdout, so anything slow there stalls every notification and
#   every response for every turn, with no deadline in reach. The bound is
#   that it cannot be slow — see the constraint recorded on the method.


# The name BOS's loopback tool server takes in Codex's `[mcp_servers.<name>]`
# namespace (BEP 19 §3.8). Not cosmetic: `config=` is an override *merged over*
# the operator's own `~/.codex/config.toml`, not a replacement, so this name is
# shared with whatever they already have there, and a collision is a real
# failure. Measured against codex-cli 0.145.0 with a throwaway CODEX_HOME, via
# the CLI's own `-c` flag (see `_mcp_egress_config` for why that is the
# stand-in):
#
# - the merge is per key even for a whole-table override: overriding
#   `mcp_servers.<name>` wholesale with a `{url, http_headers}` table left a
#   `bearer_token_env_var` from config.toml in place on the merged entry. So a
#   collision with an HTTP server of the same name leaves Codex holding the
#   operator's credential key next to the header below, and BOS cannot say
#   which Authorization it then sends. If it sends theirs, `_gate` answers 401
#   and the agent simply has no BOS tools — silently, since `_gate` does not
#   log and the server runs with `access_log=False`.
# - a collision with a *stdio* server of the same name is louder and worse: the
#   whole config fails to load ("url is not supported for stdio in
#   `mcp_servers.<name>`"), which fails the turn rather than degrading it.
#
# Hence "bos-tools" rather than the bare project name: it is the name the
# server already reports for itself over MCP (`Server("bos-tools", …)` in
# mcp_egress.py), so one server has one name on both sides of the wire, and it
# is a good deal less likely than "bos" to be a table an operator already has.
# Hyphens are fine — `-c mcp_servers.bos-tools={…}` parses as a dotted override.
_MCP_SERVER_NAME = "bos-tools"


def _content_to_codex_input(content: MessageContent) -> Input | str:
    """BOS ``MessageContent`` -> a Codex ``Input`` (BEP 19 §3.9).

    Typed ``Input | str`` rather than the wider ``RunInput`` (``Input | str |
    ExternalMessage``) that ``thread.turn()`` accepts: this never produces an
    ``ExternalMessage``, and the narrower type is what ``AsyncTurnHandle.steer()``
    (Task 7 fix round 1) requires, so the same conversion serves both a new
    turn's content and a steering message's, honestly — not by widening
    ``steer()``'s accepted type with a ``cast``.

    A plain string is already valid input (the SDK wraps it in a
    ``TextInput`` itself) and passes straight through unchanged. A list of BOS
    parts is mapped item by item:

    - ``TextPart`` -> ``TextInput``.
    - ``ImagePart`` -> ``ImageInput`` for a url/data source, or
      ``LocalImageInput`` for a path — Codex reads that path itself, since it
      runs against this same filesystem; there is no reason to base64-encode
      it the way a remote-only provider would.
    - ``FilePart`` -> ``MentionInput``, named after the file since a BOS
      ``FilePart`` carries no separate display name. Codex's mention mechanism
      has no wire form for a remote file, so a url-sourced ``FilePart`` is
      rejected rather than silently dropped or mis-sent as a local path.

    ``content_as_parts`` validates as well as normalizes, so every part
    reaching the loop below is already one of exactly ``text``/``image``/
    ``file`` — the three kinds ``_content.py`` currently defines.
    """
    if isinstance(content, str):
        return content
    items: list[InputItem] = []
    for part in content_as_parts(content):
        part_type = part.get("type")
        if part_type == "text":
            items.append(TextInput(text=part["text"]))
        elif part_type == "image":
            source = part["source"]
            if source["kind"] == "url":
                items.append(ImageInput(url=source["value"]))
            else:
                items.append(LocalImageInput(path=source["value"]))
        elif part_type == "file":
            source = part["source"]
            if source["kind"] != "path":
                raise ValueError(
                    f"codex runtime: a FilePart sent to Codex must be a local path, not a "
                    f"{source['kind']!r} source ({source['value']!r}); Codex has no wire form "
                    "for mentioning a remote file."
                )
            items.append(MentionInput(name=Path(source["value"]).name, path=source["value"]))
    return items


def _codex_input_to_content(content: list[UserInput]) -> MessageContent:
    """A Codex ``UserMessageThreadItem.content`` -> BOS ``MessageContent`` (BEP 19 §3.7).

    The inverse of :func:`_content_to_codex_input`, mirrored member by member
    so a message BOS sent and later reads back is recognizably the same one:

    - ``TextUserInput`` -> ``TextPart``.
    - ``ImageUserInput`` -> ``ImagePart`` with a ``url`` source.
    - ``LocalImageUserInput`` -> ``ImagePart`` with a ``path`` source.
    - ``MentionUserInput`` -> ``FilePart`` with a ``path`` source. BOS's
      ``FilePart`` requires a non-empty ``mime_type`` and a Codex mention
      carries none, so it is guessed from the path and falls back to
      ``_UNKNOWN_FILE_MIME_TYPE``. That guess is made on the way back and is
      not something Codex reported — the outbound direction drops the mime
      type entirely, so no round trip can preserve it.

    ``UserInput`` is an eight-member union, and the four above are the four
    :func:`_content_to_codex_input` can produce. The other four —
    ``FileIdUserInput``, ``AudioUserInput``, ``LocalAudioUserInput``,
    ``SkillUserInput`` — can still appear in a thread someone else authored
    (the `codex` CLI, another client), and BOS has no content part for any of
    them. Each becomes a text placeholder naming its kind *and carrying its own
    fields*, rather than being dropped: a dropped part makes the message read
    as though the user never sent it, and a placeholder keeping only the kind
    loses the skill that was invoked or the audio that was attached — a
    partial drop by another name.

    A lone text part comes back as a plain ``str``, not a one-item list —
    the same normalization ``content_as_parts`` applies on the way out, so a
    message sent as a plain string round-trips to a plain string instead of
    rendering differently from the copy in BOS's own record.
    """
    parts: list[MessageContentPart] = []
    for wrapped in content:
        # `.root` directly, without :func:`_unwrap_thread_item`'s ``hasattr``
        # hedge. That hedge exists because the vendor writes it for
        # ``ThreadItem`` in its own code (``_run.py:36-40``) and it is mirrored
        # verbatim; there is no such vendor precedent for ``UserInput``, and
        # every element here was validated into the ``RootModel`` when pydantic
        # built the enclosing ``UserMessageThreadItem``.
        item = wrapped.root
        if isinstance(item, TextUserInput):
            parts.append({"type": "text", "text": item.text})
        elif isinstance(item, ImageUserInput):
            parts.append({"type": "image", "source": {"kind": "url", "value": item.url}})
        elif isinstance(item, LocalImageUserInput):
            parts.append({"type": "image", "source": {"kind": "path", "value": item.path}})
        elif isinstance(item, MentionUserInput):
            parts.append(
                {
                    "type": "file",
                    "mime_type": mimetypes.guess_type(item.path)[0] or _UNKNOWN_FILE_MIME_TYPE,
                    "source": {"kind": "path", "value": item.path},
                }
            )
        else:
            # Named AND carrying its payload: `SkillUserInput.name`,
            # `AudioUserInput.url` and the rest are plain renderable text, and
            # losing them is a partial drop dressed up as a placeholder. Which
            # fields exist varies by member, so the model's own dump carries
            # them — `by_alias` so the keys read as the wire spells them, and
            # `mode="json"` so an enum field (`ImageDetail` on
            # `FileIdUserInput`) renders as its value and not as a repr.
            payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
            parts.append({"type": "text", "text": f"[{type(item).__name__}: no BOS content part — {payload}]"})
    if len(parts) == 1:
        only = parts[0]
        if only["type"] == "text":
            return only["text"]
    return parts


def _unwrap_thread_item(item: ThreadItem) -> Any:
    """``ThreadItem`` is a pydantic ``RootModel`` union; unwrap it to the concrete
    variant before an ``isinstance`` check, exactly as ``openai_codex/_run.py``
    (lines 36-40) does ahead of its own ``isinstance`` checks. The ``hasattr``
    guard — rather than assuming ``.root`` is always present — is the vendor's
    own hedge against a future item that arrives unwrapped; mirrored verbatim
    rather than simplified to a bare ``.root``.
    """
    return item.root if hasattr(item, "root") else item


def _tool_name_for(item: Any) -> str | None:
    """The two ``ThreadItem`` variants BEP 19 §3.9 maps to a ``tool`` event.

    ``CommandExecutionThreadItem`` has no separate "tool name" field — the
    command being run *is* the tool identity — so its own ``command`` string
    is what a host has to show. ``McpToolCallThreadItem`` names its tool
    directly. Every other variant (``FileChangeThreadItem``,
    ``ReasoningThreadItem``, a future addition, ...) returns ``None``: BOS has
    no ``tool`` vocabulary for it, so the caller skips the notification
    instead of emitting a half-populated event.
    """
    if isinstance(item, CommandExecutionThreadItem):
        return item.command
    if isinstance(item, McpToolCallThreadItem):
        return item.tool
    return None


def _raise_for_failed_turn(turn: Turn) -> None:
    """Mirrors ``openai_codex._run._raise_for_failed_turn`` exactly.

    ``_collect_async_turn_result`` calls this on the completed ``Turn`` before
    ever building a ``TurnResult``, so a failed turn never reaches its caller
    as a normal result on the streaming path either — the same place
    ``AsyncThread.run()`` raises for Task 5's plain (non-streaming) call.
    Reimplemented rather than imported: this is vendor-internal (leading
    underscore) code, so a citation in a comment is the stable reference,
    not a runtime dependency on a module that owes us no compatibility.
    """
    if turn.status is not TurnStatus.failed:
        return
    if turn.error is not None and turn.error.message:
        raise RuntimeError(turn.error.message)
    raise RuntimeError(f"turn failed with status {turn.status.value}")


def _final_assistant_response_from_items(items: list[ThreadItem]) -> str | None:
    """Mirrors ``openai_codex._run._final_assistant_response_from_items`` exactly:
    the *last* ``AgentMessageThreadItem`` whose ``phase`` is ``final_answer``,
    or — only when none exists — the last one with no phase at all. A
    ``commentary``-phase message is neither: it is not the answer, and it is
    not a fallback candidate either, so it is silently skipped either way.
    """
    last_unknown_phase_response: str | None = None
    for item in reversed(items):
        thread_item = _unwrap_thread_item(item)
        if not isinstance(thread_item, AgentMessageThreadItem):
            continue
        if thread_item.phase is MessagePhase.final_answer:
            return thread_item.text
        if thread_item.phase is None and last_unknown_phase_response is None:
            last_unknown_phase_response = thread_item.text
    return last_unknown_phase_response


class TurnNotCompletedError(RuntimeError):
    """A Codex turn ended without producing a usable answer, on a status Codex
    itself hands back as a normal ``TurnResult`` rather than raising for.

    Today that is only ``interrupted`` — ``failed`` is raised by the vendor
    SDK before it ever constructs a ``TurnResult`` (see the comment in
    ``run()``), so it never reaches this exception. Carries ``turn_result`` so
    a caller has more than a message to work with.

    Not every ``interrupted`` turn raises this since Task 7 (BEP 19 §3.10.2). A
    cooperative stop — ``request_stop()`` racing the turn — returns the kept
    partial answer instead, mirroring ``Agent``'s own contract for the same
    situation; a ``timeout_seconds`` expiry raises a plain ``TimeoutError``
    instead, so a caller can catch a deadline it imposed itself without also
    catching this. What is left for this exception is an ``interrupted``
    status this runtime never itself asked for — see ``run()``'s own
    docstring for the full three-way split.

    The actor's own per-chat ``interrupt`` callback (BEP 19 §3.9's
    ``interrupt`` row) is not one of the three: since fix round 1, a truthy
    return steers the running turn (``AsyncTurnHandle.steer()``) rather than
    ending it, so it never produces ``interrupted`` at all — see
    ``_emit_stream``'s docstring.
    """

    def __init__(self, message: str, *, turn_result: TurnResult) -> None:
        super().__init__(message)
        self.turn_result = turn_result


class CodexAgent:
    """``ExternalRuntime`` adapter over the Codex vendor SDK (BEP 19 §3.4, §3.5.1).

    One instance per ``create_agent`` call. It owns one ``AsyncCodex`` client,
    built lazily on first turn (§3.1) rather than here — constructing an agent
    must never spawn the ``codex app-server`` child.
    """

    def __init__(
        self,
        *,
        kind: str,
        cfg: Mapping[str, Any],
        chat_store: ChatStore | None,
        workspace: Path,
        mcp: Callable[[], Any],
        structured_validator: StructuredValidator,
    ) -> None:
        self._kind = kind
        self._chat_store = chat_store
        self._mcp = mcp
        self._structured_validator = structured_validator
        self._config = parse_external_config(dict(cfg), runtime="codex", workspace=Path(workspace))
        self._client: AsyncCodex | None = None
        self._client_lock = asyncio.Lock()
        # The `[mcp_servers.<name>]` override every thread of this agent is
        # started/resumed with, or None when `mcp_tools` is empty and no server
        # was ever asked for. Built once by _ensure_client, under the same lock.
        self._mcp_config: JsonObject | None = None
        self._stop_requested = asyncio.Event()
        # chat_id -> the task consuming that chat's in-flight turn (BEP
        # §3.10.1's busy guard). Reserved (value None) the instant run() is
        # entered, before any await can interleave a second call for the same
        # chat_id; filled in once _run_turn creates the real task, so aclose()
        # has something to wait on and a concurrent run() has something to see.
        self._in_flight: dict[str, asyncio.Task[TurnResult] | None] = {}

    @property
    def name(self) -> str:
        return self._kind

    @property
    def resolved_config(self) -> Mapping[str, Any]:
        """The *resolved* config ``boscli inspect`` reads (BEP 19 §8.2): an
        absolute ``cwd`` and a validated ``permission``, not the raw input the
        constructor was handed."""
        return MappingProxyType(
            {
                "external_runtime": self._config.runtime,
                "cwd": str(self._config.cwd),
                "permission": self._config.permission,
                "system_prompt": self._config.system_prompt,
                "base_instructions": self._config.base_instructions,
                "model": self._config.model,
                "auth": self._config.auth,
                "timeout_seconds": self._config.timeout_seconds,
                "mcp_tools": list(self._config.mcp_tools),
                # The subset of `mcp_tools` the host has no `ep_tool` for, from
                # the same predicate the MCP server's own warn-and-skip uses.
                # Computed here rather than read back off the server because
                # there may be no server: `_ensure_client` is lazy, so `boscli
                # inspect` — which builds an agent and runs no turn — would
                # otherwise never see the mismatch it exists to surface.
                "mcp_tools_unavailable": list(unregistered_tools(self._config.mcp_tools)),
                "native_options": dict(self._config.native_options),
            }
        )

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def _ensure_client(self) -> AsyncCodex:
        """Build the one ``AsyncCodex`` this agent owns, lazily (BEP 19 §3.1, §3.10.1).

        Guarded by a lock so two concurrent turns on different ``chat_id``s
        under the same agent cannot each build and orphan their own client —
        the SDK's own ``AsyncCodex._ensure_initialized`` guards itself the
        same way, for the same reason.
        """
        async with self._client_lock:
            if self._client is None:
                # Ahead of the client, not after it: the egress can fail (no
                # `mcp` installed, a harness already torn down), and a client
                # built first would be an orphan that _preflight_auth below may
                # already have spawned a child for. Inside the lock so two
                # concurrent turns register one grant, not two.
                self._mcp_config = await self._mcp_egress_config()
                client: AsyncCodex = _CODEX_FACTORY(CodexConfig())
                # BEP 19 §3.5.4, and before _preflight_auth below, because that
                # is the first call that can spawn the child and so the first
                # moment a server request can arrive. The SDK's default handler
                # auto-accepts escalations and AsyncCodex offers no way to pass
                # a replacement down (AsyncCodexClient.__init__ takes only a
                # config), so reach the sync client that owns the transport.
                #
                # No getattr guard and no try/except on purpose: if a future
                # openai-codex moves this attribute, construction must fail
                # loudly rather than silently leave the auto-accepting default
                # installed — that silent restoration is the whole hazard.
                # test_codex_approval_handler_attribute_exists pins both the
                # attribute path and the absence of a supported alternative.
                client._client._sync._approval_handler = self._deny_approval
                if self._config.auth == "subscription":
                    await self._preflight_auth(client)
                self._client = client
            return self._client

    async def _mcp_egress_config(self) -> JsonObject | None:
        """Start BOS's loopback MCP server and return the Codex config override
        that points this agent's threads at it (BEP 19 §3.8) — or None when
        there is nothing for that server to serve this agent.

        "Nothing to serve" is two cases, and neither one touches the ``mcp``
        accessor: an empty ``mcp_tools``, and an ``mcp_tools`` whose every name
        the host has no ``ep_tool`` for. Not touching it is the point (§3.1,
        the lazy half of §7.7) — ``AgentHarness._ensure_tool_mcp_server``
        *builds* the server as a side effect of being asked, so asking and then
        discarding the answer binds a loopback port to serve an empty tool
        list. §7.5 asks for both halves of the second case at once: that
        warning, and no server.

        Both halves are reachable only because the skip rule is a predicate
        rather than something the server decides. Asking
        ``mcp_egress.unregistered_tools`` here needs no server, so this method
        owns the warning — one line per missing name, naming the agent kind,
        which is knowledge the registry does not have — and hands
        ``register_agent`` only names that already resolve. That is why there
        is exactly *one* warning per typo and not two: ``register_agent``'s own
        warn-and-skip is left in place as the safety net for any other caller,
        and this path never trips it. The same predicate feeds
        :attr:`resolved_config`, so ``boscli inspect`` reports the same names on
        an agent that has never run a turn and so never reached this method.

        The bearer token is minted per agent and travels only as a request
        header. Three vendor notes, all measured against codex-cli 0.145.0
        with a throwaway ``CODEX_HOME`` — through the CLI's own ``-c`` flag,
        which is the observable stand-in for this ``config=`` argument (the
        app-server schema documents ``ThreadStartParams.config`` as nothing but
        a free-form object, so that the two are one channel is inference, not
        measurement; Task 11's manual checklist carries the live check):

        - ``http_headers`` is what carries it. ``codex mcp list`` reports a
          server configured this way as ``Auth: Bearer token``, and an invented
          key on the same server as ``Auth: Unsupported``.
        - **Not** ``bearer_token``. It is not a literal-token key at all, and
          it is not ignored either: ``-c mcp_servers.probe.bearer_token="x"``
          fails the whole config load with "bearer_token is not supported for
          streamable_http".
        - **Not** ``bearer_token_env_var``, though it is the only auth option
          ``codex mcp add`` offers. It names an environment variable, and the
          only env BOS controls here is ``CodexConfig(env=…)``, fixed when the
          client is constructed — which, since this runs just before that,
          is before the token exists.
        """
        unavailable = unregistered_tools(self._config.mcp_tools)
        for name in unavailable:
            logger.warning(
                "%s runtime %r: mcp_tools names %r, which is not a registered tool; it is not exposed.",
                self._config.runtime,
                self._kind,
                name,
            )
        available = [name for name in self._config.mcp_tools if name not in unavailable]
        if not available:
            return None
        server = self._mcp()
        await server.start()
        # Only resolvable names, so register_agent has nothing to warn about.
        token = server.register_agent(self._kind, available)
        # Annotated, not inferred: `JsonObject` is `dict[str, JsonValue]` and
        # `dict` is invariant in its value type, so an unannotated nested literal
        # infers as `dict[str, dict[str, ...]]` and will not assign to it.
        config: JsonObject = {
            "mcp_servers": {
                _MCP_SERVER_NAME: {"url": server.url, "http_headers": {"Authorization": f"Bearer {token}"}}
            }
        }
        return config

    def _deny_approval(self, method: str, params: JsonObject | None) -> JsonObject:
        """Refuse every escalation ``codex app-server`` asks for (BEP 19 §3.5.4).

        Installed over ``CodexClient._default_approval_handler``, which accepts
        command-execution and file-change escalations outright. The policy, and
        why it needs no ``permission`` argument, is on ``_APPROVAL_DENIALS``.

        Synchronous and non-blocking by contract, not by preference: the vendor
        calls this from ``CodexClient._handle_server_request``, on the single
        stdout reader thread (``_reader_loop``, client.py:863-871), and writes
        what it returns straight back as the JSON-RPC ``result``. Blocking here
        stalls the whole transport — every notification and every response, for
        every turn — and there is no event loop on that thread, so nothing here
        may touch asyncio. A dict lookup and ``logger.warning`` is all it does.

        It is handed *every* server-to-client request, not only approvals. The
        other five in the ``ServerRequest`` union (``item/tool/call``,
        ``item/tool/requestUserInput``, ``mcpServer/elicitation/request``,
        ``attestation/generate``, ``account/chatgptAuthTokens/refresh``) fall
        through to ``{}``, which is exactly what the vendor default answers
        them with. Changing that is out of scope here.
        """
        denial = _APPROVAL_DENIALS.get(method)
        if denial is None:
            return {}
        # Never silent: a denied escalation and a model that simply chose not
        # to try look identical from the outside, and only one of them is a
        # reason the agent could not finish the job.
        logger.warning(
            "%s runtime %r: denied Codex escalation request %r — BOS decides approvals by policy "
            "and has no channel to ask a human (BEP 19 §3.5.4)",
            self._config.runtime,
            self._kind,
            method,
        )
        return denial

    async def _preflight_auth(self, client: AsyncCodex) -> None:
        """BEP 19 §3.10.3: with ``auth="subscription"``, fail loudly here —
        once, before the client is cached, never per turn — rather than let a
        missing login silently fall back to billed API-key usage later.

        A client that fails this check is never handed to a caller: on either
        failure below we best-effort close it (it may already have spawned
        the ``codex app-server`` child to answer ``account()`` at all) so a
        later retry, once the operator has actually logged in, starts clean
        instead of piling up abandoned children.
        """
        runtime = self._config.runtime
        try:
            response = await asyncio.wait_for(client.account(), _PREFLIGHT_AUTH_SECONDS)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.close()
            # wait_for's TimeoutError carries no message, and "the account
            # check failed: " with nothing after it tells an operator nothing.
            detail = (
                f"did not answer within {_PREFLIGHT_AUTH_SECONDS}s"
                if isinstance(exc, TimeoutError)
                else f"failed: {exc}"
            )
            raise RuntimeError(
                f'{runtime} runtime {self._kind!r}: auth="subscription" but the account check {detail}'
            ) from exc
        if response.account is None:
            with contextlib.suppress(Exception):
                await client.close()
            raise RuntimeError(
                f'{runtime} runtime {self._kind!r}: auth="subscription" but no Codex account is '
                f'logged in. Run `codex login`, or set auth="api_key" to opt out of this check.'
            )

    async def _bounded_setup(self, awaitable: Awaitable[_T], *, phase: str, chat_id: str, turn_id: str) -> _T:
        """Bound one vendor *setup* RPC with ``timeout_seconds`` (fix round 4).

        ``thread_start`` / ``thread_resume`` / ``thread.turn`` are awaited
        before :meth:`_run_turn` exists to wrap them in ``asyncio.timeout``,
        so without this a child that wedges during setup hangs the turn with
        the caller's own deadline never firing.

        Bounded per *attempt*, which is what ``timeout_seconds`` already means
        here — the schema-retry loop gives every attempt its own fresh
        ``asyncio.timeout``, so a turn with retries can already take a
        multiple of it. This does **not** make it a whole-call deadline; that
        would be a real semantic change and is not what this is.

        Nothing is interrupted on expiry, unlike :meth:`_settle_interrupted`'s
        paths — but for one of the three that is a *limitation*, not a
        cheaper teardown:

        - ``thread_start`` / ``thread_resume``: nothing was started, so there
          is genuinely nothing to tell the vendor about. ``wait_for``
          cancelling the RPC is the whole teardown.
        - ``thread.turn``: **the native turn may already be running.** The
          vendor submits the start to a module-level executor and awaits it
          through ``asyncio.wrap_future``; on cancellation it only closes the
          orphaned *subscription* — its own docstring says "releasing an
          unclaimed result" — and never cancels the submitted work. So the
          child can go on working against ``cwd`` with no way for BOS to stop
          it, because ``turn_interrupt`` needs the turn id and the turn id is
          exactly what the cancelled call never returned. That turn ends when
          the client closes. Known and accepted (BEP 19 §8.2): recovering it
          would mean reading the thread back to find an in-flight turn, over
          an RPC that can wedge the same way. This is not a regression — the
          call used to hang *and* leave the turn running; bounding it traded
          a silent hang for a silent orphan.

        ``timeout_seconds=None`` means the caller declined a deadline, and
        this declines one too rather than inventing a fallback. A wedge is
        still recoverable in that case: no turn task is registered, so
        ``aclose()`` waits on none, takes the uncontended ``_client_lock``
        and closes the client — which fails the pending RPC, and is also what
        ends an orphaned ``thread.turn``.

        The message names the phase, so a setup timeout is never mistaken for
        a turn that timed out while streaming.
        """
        try:
            return await asyncio.wait_for(awaitable, self._config.timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                f"{self._config.runtime} runtime {self._kind!r}: {phase} for turn {turn_id!r} on "
                f"chat {chat_id!r} exceeded timeout_seconds={self._config.timeout_seconds!r}"
            ) from exc

    async def _thread_for(self, chat_id: str, *, turn_id: str) -> tuple[AsyncThread, bool]:
        """Map *chat_id* onto a Codex thread (BEP 19 §3.6): resume the thread on
        record for this chat, or start a fresh one when there is none.

        Returns ``(thread, started)`` — *started* is True only when a new
        thread was opened. A thread on record that Codex can no longer resume
        (archived, deleted, expired) is raised as an error naming the runtime
        and the thread id; it is never silently replaced with a new thread
        under the same ``chat_id``, which would drop the user's conversation
        mid-way with no signal.
        """
        client = await self._ensure_client()
        native_session_id = (
            await read_native_session_id(self._chat_store, chat_id, runtime=self._config.runtime)
            if self._chat_store is not None
            else None
        )
        sandbox, approval_mode = _SANDBOX_AND_APPROVAL[self._config.permission]
        thread_kwargs: dict[str, Any] = {
            "sandbox": sandbox,
            "approval_mode": approval_mode,
            "cwd": str(self._config.cwd),
            "model": self._config.model,
            "developer_instructions": self._config.system_prompt,
            "base_instructions": self._config.base_instructions,
            # BEP 19 §3.8. Both thread_start and thread_resume take it, so a
            # resumed chat reaches the same tools a fresh one does. None is the
            # vendor's own default for the parameter, and is what an agent with
            # nothing servable passes — see `_mcp_egress_config`.
            "config": self._mcp_config,
        }
        if native_session_id is None:
            thread = await self._bounded_setup(
                client.thread_start(**thread_kwargs),
                phase="thread setup (thread_start)",
                chat_id=chat_id,
                turn_id=turn_id,
            )
            return thread, True
        try:
            thread = await self._bounded_setup(
                client.thread_resume(native_session_id, **thread_kwargs),
                phase="thread setup (thread_resume)",
                chat_id=chat_id,
                turn_id=turn_id,
            )
        except TimeoutError:
            # Deliberately ahead of the wrap below: a child that never answers
            # is not a thread BOS can no longer resume. Re-labelling it as the
            # session-continuity error would send an operator hunting for a
            # corrupt or expired session when the real answer is a wedged
            # child.
            raise
        except Exception as exc:
            raise RuntimeError(
                f"{self._config.runtime} runtime {self._kind!r}: thread {native_session_id!r} for "
                f"chat {chat_id!r} could not be resumed, and BOS does not silently start a fresh "
                f"session under the same chat_id (BEP 19 §3.6): {exc}"
            ) from exc
        return thread, False

    def _event(
        self,
        *,
        chat_id: str,
        turn_id: str,
        event_type: str,
        phase: str,
        detail: str | None = None,
        tool_name: str | None = None,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnEvent:
        return TurnEvent(
            event_type=event_type,
            phase=phase,
            chat_id=chat_id,
            turn_id=turn_id,
            agent_name=self._kind,
            detail=detail,
            tool_name=tool_name,
            content=content,
            metadata=dict(metadata or {}),
        )

    def _event_for_notification(
        self,
        notification: Notification,
        *,
        chat_id: str,
        turn_id: str,
        metadata: dict[str, Any] | None,
    ) -> TurnEvent | None:
        """BEP 19 §3.9's mapping. ``Notification.method`` drives it, refined by
        the concrete ``ThreadItem`` variant for ``item/started``/``item/completed``:

        - ``item/started`` on a ``CommandExecutionThreadItem``/``McpToolCallThreadItem``
          -> ``tool``/``start``.
        - the matching ``item/completed`` -> ``tool``/``finish``.
        - ``item/completed`` on an ``AgentMessageThreadItem`` -> ``response``/``finish``.
        - ``turn/completed`` -> ``turn``/``finish``.

        Anything else — an unrecognised ``method``, or an item variant BOS has
        no ``tool`` vocabulary for (a ``FileChangeThreadItem``, a future
        addition, ...) — returns ``None``. The vendor adds notification and
        item kinds between releases; a turn must not die because BOS has not
        heard of one yet, so this is a lookup that misses cleanly, never a
        raise.
        """
        payload = notification.payload
        if notification.method == "item/started" and isinstance(payload, ItemStartedNotification):
            tool_name = _tool_name_for(_unwrap_thread_item(payload.item))
            if tool_name is None:
                return None
            return self._event(
                chat_id=chat_id,
                turn_id=turn_id,
                event_type=AgentEventType.tool,
                phase=TurnEventPhase.start,
                tool_name=tool_name,
                metadata=metadata,
            )
        if notification.method == "item/completed" and isinstance(payload, ItemCompletedNotification):
            item = _unwrap_thread_item(payload.item)
            if isinstance(item, AgentMessageThreadItem):
                return self._event(
                    chat_id=chat_id,
                    turn_id=turn_id,
                    event_type=AgentEventType.response,
                    phase=TurnEventPhase.finish,
                    content=item.text,
                    metadata=metadata,
                )
            tool_name = _tool_name_for(item)
            if tool_name is None:
                return None
            return self._event(
                chat_id=chat_id,
                turn_id=turn_id,
                event_type=AgentEventType.tool,
                phase=TurnEventPhase.finish,
                tool_name=tool_name,
                metadata=metadata,
            )
        if notification.method == "turn/completed" and isinstance(payload, TurnCompletedNotification):
            return self._event(
                chat_id=chat_id,
                turn_id=turn_id,
                event_type=AgentEventType.turn,
                phase=TurnEventPhase.finish,
                metadata=metadata,
            )
        return None

    async def _emit_stream(
        self,
        handle: AsyncTurnHandle,
        sink: TurnEventSink | None,
        *,
        chat_id: str,
        turn_id: str,
        ctx_metadata: dict[str, Any] | None,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None = None,
    ) -> TurnResult:
        """Consume ``handle.stream()``, translating each ``Notification`` into a
        ``TurnEvent`` for ``sink`` while accumulating exactly what
        ``openai_codex._run._collect_async_turn_result`` does, so the
        ``TurnResult`` this returns is what a plain ``await handle.run()``
        would have produced (BEP 19 §3.9) — the only difference is that the
        caller also got to watch it happen.

        ``sink`` may be ``None`` (the common case for ``_HarnessAgentRunner``):
        the turn still streams and collects, translation is just skipped.
        Emitting is best-effort — a sink that raises must not end the turn,
        mirroring how ``Agent._emit_event`` guards ``event_sink.emit``
        (``agent.py``).

        ``interrupt`` — ``AgentActor``'s poll-style callback (BEP 19 §3.9's
        `interrupt` row) — is polled once per notification, exactly as
        ``Agent._interrupt`` reads the same callback in ``agent.py``, with one
        exception: the terminal ``turn/completed`` notification. Polling is
        destructive (``AgentActor._make_interrupt`` *pops* the pending
        envelopes off the session), and once the turn has completed there is
        nothing left to steer into — so a poll there would take the user's
        message and have nowhere to put it (fix round 2, I3):

        - **A truthy return is a message to deliver, not a request to stop.**
          Fix round 1: Task 7's first cut read the brief's "`interrupt`
          callback -> ``AsyncTurnHandle.interrupt()``" table row (BEP 19
          §3.9) as "fires -> stop", but ``Agent._interrupt`` itself —
          ``if interrupt and (llm_message := await _apply_async(interrupt, {})):
          ctx.add_message(llm_message, merge=True)`` — merges the returned
          LLM message dict into the *running* turn's context; the turn
          continues. `AgentActor._make_interrupt` (`agent_actor.py:543`)
          returns exactly that shape for a queued ``INTERRUPT_MESSAGE`` (a
          user's follow-up sent while the turn is still going). Ending the
          turn instead — what this used to do — killed that follow-up rather
          than delivering it. Codex's own primitive for "deliver input to the
          turn that's already running" is ``AsyncTurnHandle.steer()``
          ("Send additional user input to this active turn"); only ``review``
          and ``compact`` turn kinds refuse it
          (``NonSteerableTurnKind``/``ActiveTurnNotSteerable``), an ordinary
          chat turn is always steerable, and it does not end or replace the
          turn — the same ``handle``/``turn_id`` keeps streaming afterward.
          So a truthy return's ``"content"`` value — a BOS ``MessageContent``,
          the same shape ``TurnContext.add_message``'s merge branch expects,
          not the whole ``dict`` — is converted with
          :func:`_content_to_codex_input` and handed to ``handle.steer()``.
          The steer call itself is best-effort — a failed steer RPC is a
          network blip, not a turn failure, and the turn may still finish
          normally without it — but it is **logged at WARNING**, not
          swallowed: unlike a failed ``handle.interrupt()`` (a courtesy BOS
          sends on its own behalf), what is lost here is a message a user
          typed, and nothing else in the system records that it existed.
        - **Raising unwinds the turn.** ``Agent._interrupt`` does not catch
          ``AbortTurn`` either — it is meant to unwind the turn, not be
          absorbed here. Nothing in *this* method catches it (or anything
          else the callback raises): it propagates out of the ``async for``
          (through the ``finally`` below, so the stream is still closed),
          out of this coroutine, and — via :meth:`_run_turn`'s
          ``stream_task.result()`` — up to ``run()``, which is where the
          unwinding stops: ``run()`` catches ``AbortTurn`` and returns the
          aborted-turn marker, matching what ``Agent`` hands its own caller
          (see ``run()``'s docstring; fix round 2, I4). Any *other* exception
          the callback raises is wrapped by ``run()`` as a turn failure.
          Either way :meth:`_run_turn` interrupts the native turn on its way
          past (fix round 3, D) — unwinding BOS-side alone would leave the
          child running against ``cwd`` with nothing left that knows about it.
        - **A falsy return does nothing.** No steer, no interrupt, no
          state change — the turn is not even aware the callback fired.

        None of this produces ``TurnStatus.interrupted`` (steering keeps the
        turn running; raising unwinds it some other way), so the callback
        plays no part in the three-way ``interrupted`` split ``run()``'s
        docstring and :class:`TurnNotCompletedError` describe — that split is
        ``request_stop()`` / `timeout_seconds` / unexplained, unchanged from
        before this fix.
        """
        items: list[ThreadItem] = []
        usage: ThreadTokenUsage | None = None
        completed: TurnCompletedNotification | None = None

        # `AsyncTurnHandle.stream()` is annotated `-> AsyncIterator[Notification]`,
        # but its body is an `async def ... yield ...` function, so calling it
        # always produces a real async generator — `AsyncIterator` just doesn't
        # declare the `aclose()` every async generator actually has. A vendor
        # stub gap, not a guess: cast to what it actually is rather than
        # suppress the check.
        stream = cast(AsyncGenerator[Notification, None], handle.stream())
        try:
            async for notification in stream:
                payload = notification.payload
                if isinstance(payload, ItemCompletedNotification) and payload.turn_id == handle.id:
                    items.append(payload.item)
                elif isinstance(payload, ThreadTokenUsageUpdatedNotification) and payload.turn_id == handle.id:
                    usage = payload.token_usage
                elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == handle.id:
                    completed = payload

                if sink is not None:
                    event = self._event_for_notification(
                        notification, chat_id=chat_id, turn_id=turn_id, metadata=ctx_metadata
                    )
                    if event is not None:
                        try:
                            await sink.emit(event)
                        except Exception:
                            logger.debug("Codex event sink emit error", exc_info=True)

                if interrupt is not None and completed is None:
                    # Not polled once the terminal turn/completed has landed:
                    # the callback drains destructively (AgentActor._make_interrupt
                    # pops the pending message), and a steer against a finished
                    # turn is rejected by the vendor — so polling here would
                    # consume the user's message and throw it away.
                    #
                    # The poll itself is not wrapped in try/except: a raise
                    # (AbortTurn or anything else) is meant to propagate, per
                    # this method's own docstring — only the steer RPC below
                    # is best-effort, and even that is logged, not silent.
                    steer_message = await _apply_async(interrupt, {})
                    if steer_message:
                        steer_input = _content_to_codex_input(steer_message.get("content", ""))
                        try:
                            await handle.steer(steer_input)
                        except Exception:
                            # Best-effort, but never silent: this message came
                            # from a user and is now lost.
                            logger.warning(
                                "%s runtime %r: steering turn %r on chat %r failed; "
                                "the mid-turn message was dropped",
                                self._config.runtime,
                                self._kind,
                                turn_id,
                                chat_id,
                                exc_info=True,
                            )
        finally:
            await stream.aclose()

        if completed is None:
            raise RuntimeError("turn completed event not received")
        turn = completed.turn
        _raise_for_failed_turn(turn)

        return TurnResult(
            id=turn.id,
            status=turn.status,
            error=turn.error,
            started_at=turn.started_at,
            completed_at=turn.completed_at,
            duration_ms=turn.duration_ms,
            final_response=_final_assistant_response_from_items(items),
            items=items,
            usage=usage,
        )

    async def _run_turn(
        self,
        handle: AsyncTurnHandle,
        sink: TurnEventSink | None,
        *,
        chat_id: str,
        turn_id: str,
        ctx_metadata: dict[str, Any] | None,
        interrupt: Callable[[], dict[str, Any] | Awaitable[dict[str, Any]] | None] | None,
    ) -> tuple[TurnResult, bool]:
        """Consume one native turn, racing it against a cooperative stop and
        ``timeout_seconds`` (BEP 19 §3.10.2) on top of ``_emit_stream``'s own
        per-notification interrupt-callback poll (which, since fix round 1,
        steers rather than stops — see ``_emit_stream``'s docstring; it never
        causes the branch below to be taken).

        Returns ``(result, interrupted_by_host)``. ``interrupted_by_host`` is
        True only when ``self._stop_requested`` is why the turn ended early —
        as opposed to a `timeout_seconds` expiry (raised, never returned) or a
        vendor-reported `interrupted` this method never asked for. ``run()``
        uses it to decide between keeping a partial answer and raising, since
        the vendor's own `TurnStatus.interrupted` cannot tell those apart by
        itself.

        Registers the streaming task in ``self._in_flight[chat_id]`` for the
        busy guard and for ``aclose()`` to find and wait on.

        Whichever way the turn ends early, the native side is told: a stop or
        a timeout through :meth:`_settle_interrupted`, and a stream task that
        ended in an *exception* — an ``AbortTurn`` or anything else the
        interrupt callback raised — through the bounded, best-effort
        interrupt in the ``done()`` branch below (fix round 3, D). The three
        are mutually exclusive, so no turn is interrupted twice.
        """
        stream_task: asyncio.Task[TurnResult] = asyncio.ensure_future(
            self._emit_stream(
                handle, sink, chat_id=chat_id, turn_id=turn_id, ctx_metadata=ctx_metadata, interrupt=interrupt
            )
        )
        self._in_flight[chat_id] = stream_task
        stop_task = asyncio.ensure_future(self._stop_requested.wait())
        try:
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    await asyncio.wait({stream_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            except TimeoutError as exc:
                # Discard whatever _settle_interrupted comes back with: a
                # timeout raises regardless (BEP 19 §3.10.2), so there is
                # nothing to gain from waiting for a clean TurnResult — only
                # from asking the native turn to stop before this returns.
                with contextlib.suppress(Exception):
                    await self._settle_interrupted(handle, stream_task)
                raise TimeoutError(
                    f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                    f"{chat_id!r} exceeded timeout_seconds={self._config.timeout_seconds!r} and was "
                    "interrupted"
                ) from exc
        finally:
            stop_task.cancel()

        if stream_task.done():
            # Either it finished on its own, or it raced stop_task to the
            # finish line and won — either way, nothing was abandoned. (A
            # raise from the callback — see _emit_stream's docstring —
            # surfaces here too: stream_task.result() re-raises it.)
            try:
                return stream_task.result(), False
            except Exception:
                # BOS is giving up on this turn — an AbortTurn from the
                # interrupt callback, or anything else it raised (deliberately
                # unwrapped, see _emit_stream). The native turn does not know
                # that and keeps running against cwd, which BEP 19 §3.10 says
                # must not happen. Best-effort and bounded like every other
                # teardown interrupt here; harmless when the turn has already
                # ended (a vendor `failed`), since the interrupt is suppressed
                # either way.
                #
                # `except Exception`, not BaseException: a cancelled
                # stream_task raises CancelledError, and awaiting inside a
                # cancellation unwind is its own hazard. Only
                # _settle_interrupted cancels this task, and neither of its
                # two callers routes through this branch, so that case cannot
                # arrive here — and this is not a double interrupt for the
                # same reason.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.interrupt(), _INTERRUPT_GRACE_SECONDS)
                raise

        # self._stop_requested fired before the stream finished on its own:
        # a cooperative stop landed mid-turn.
        result = await self._settle_interrupted(handle, stream_task)
        return result, True

    async def _settle_interrupted(
        self, handle: AsyncTurnHandle, stream_task: asyncio.Task[TurnResult]
    ) -> TurnResult:
        """Ask the vendor to stop, then give the stream a bounded window to
        drain into the ``TurnResult`` that produces — rather than waiting it
        out (BEP 19 §3.10.2, the brief's phrasing for `request_stop()`, applied
        equally to a timeout's own best-effort interrupt). Shared by both
        callers in :meth:`_run_turn` so "a native turn that ignores the
        interrupt" is one code path, not two that could drift.

        Raises if the native side never confirms: there is no ``TurnResult``
        to hand back in that case, and manufacturing one would misreport what
        happened. Worst case is three times ``_INTERRUPT_GRACE_SECONDS`` — the
        interrupt RPC, the drain wait, and the post-cancel wait — and on that
        path the task is **abandoned**, not killed: it goes on running, owned
        by the loop rather than by this turn (the same doctrine as
        ``Agent._abandon``). :meth:`aclose` bounds its own wait because of
        that. The vendor's own stream is not what survives the cancel — driven
        directly it dies at once — but the two *host-supplied* awaits inside
        :meth:`_emit_stream`'s loop, ``sink.emit`` and the ``interrupt``
        callback, are arbitrary caller code that can shield, block or swallow
        one (fix round 3, N1).
        """
        with contextlib.suppress(Exception):
            # Bounded, not merely guarded: the vendor's interrupt is an RPC
            # over a blocking queue (asyncio.to_thread) that only wakes when
            # the child answers or dies, and suppress() catches errors, not
            # slowness. wait_for's TimeoutError is an Exception, so a wedged
            # child falls through to the cancel path below like any other
            # failure to confirm.
            await asyncio.wait_for(handle.interrupt(), _INTERRUPT_GRACE_SECONDS)
        done, _ = await asyncio.wait({stream_task}, timeout=_INTERRUPT_GRACE_SECONDS)
        if stream_task in done:
            return stream_task.result()
        stream_task.cancel()
        await asyncio.wait({stream_task}, timeout=_INTERRUPT_GRACE_SECONDS)
        # Not independently mutation-tested: this only fires in the narrow
        # window where stream_task finishes on its own — with a result or an
        # exception — in the instant between the cancel() above landing and
        # this check running, so cancel() did not "win". It exists purely so
        # asyncio's default handler does not log an "exception was never
        # retrieved" warning for that straggler; it changes no observable
        # behavior (the RuntimeError below is raised regardless). Mirrors
        # Agent._abandon's identical line (agent.py:358-360) verbatim.
        if stream_task.done() and not stream_task.cancelled():
            stream_task.exception()  # consumed
        raise RuntimeError(
            f"{self._config.runtime} runtime {self._kind!r}: turn {handle.id!r} did not respond to "
            f"interrupt within {_INTERRUPT_GRACE_SECONDS}s"
        )

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
        """Thin wrapper over :meth:`run`, mirroring ``Agent.ask`` (BOS's own agent)."""
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
        return str(result.output)

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
        """Run one Codex turn to completion, streaming it, and persist it
        (BEP 19 §3.7, §3.9).

        Every native turn — including each schema-validation retry — is
        started with ``thread.turn()`` and consumed through
        :meth:`_run_turn`/:meth:`_emit_stream`, which stream ``handle.stream()``
        into ``TurnEvent``s for ``event_sink`` while racing the turn against a
        cooperative stop and ``timeout_seconds`` and polling the ``interrupt``
        callback, accumulating the same ``TurnResult`` a plain
        ``await thread.run(...)`` would have produced. Still deliberately
        plain otherwise (stage 4 of 4, see the module docstring): no approval
        handling yet (Task 8).

        ``schema`` maps to ``thread.turn(output_schema=...)`` as a provider
        hint, but that hint is never trusted on its own: the reply is always
        checked locally with ``self._structured_validator`` (the same object
        ``create_agent`` injects into every ``Agent``, so BEP 12 semantics are
        identical regardless of which kind of agent ran the turn — not a
        second, divergent validation path). A validation failure re-sends a
        plain-text correction message on the *same* thread, up to
        ``max_schema_retries`` times; exhausting retries raises
        ``StructuredOutputError`` and — like a native turn failure — commits
        nothing, so a half-validated exchange never looks like turn history
        the next resume can reason from.

        Ending early has three distinct causes that all surface from the
        vendor as the same ``TurnStatus.interrupted``, so this method — not
        the bare status — is what tells them apart, deciding as each happens
        rather than guessing afterwards from the result alone (BEP 19 §3.10.2,
        the decision Task 5 left open):

        - **`timeout_seconds` expiry** is a deadline the *caller* asked for.
          :meth:`_run_turn` interrupts the native turn and raises a fresh
          ``TimeoutError`` (BEP 19 §7 criterion 20: "... raises"). Nothing is
          committed: an answer cut off by the caller's own deadline is a
          failure, not turn history. The same key also bounds the setup RPCs
          that run before :meth:`_run_turn` exists to wrap them
          (:meth:`_bounded_setup`, fix round 4); that flavour names its phase
          in the message and is equally uncommitted. It interrupts nothing —
          for ``thread_start``/``thread_resume`` because nothing started, and
          for ``thread.turn`` because BOS never received the turn id it would
          need, even though the native turn may be running. See
          :meth:`_bounded_setup` for that limitation in full.
        - **A cooperative stop** — ``request_stop()`` racing the turn — is
          BOS taking the turn away, not the model or the caller failing.
          ``Agent``'s own contract for the same situation
          (``src/bos/core/agent/agent.py:648-657``, ``:831-836``) is to keep
          what the turn established rather than raise and discard it —
          persisting a handoff and returning. This mirrors that: it returns
          normally, persists whatever ``final_response`` the turn had
          produced, and reports ``finish_reason="interrupted"`` so the caller
          can tell. It stops short of ``Agent``'s handoff *summary* — nothing
          hands this runtime a ``Consolidator``, and BEP 19 defines none of
          its own for it — so the kept content is the turn's own last answer,
          not a paraphrase of what happened; Task 7's brief asks for exactly
          this much and no more.
        - **An unexplained `interrupted`** — the vendor reports it without
          this call itself ever having asked for it — keeps Task 5/6's
          original behaviour: raise :class:`TurnNotCompletedError` and commit
          nothing. Nothing today produces this (the native session is private
          to this one ``CodexAgent``), but a status this method did not ask
          for is not one it should silently reinterpret as a stop.

        The ``interrupt`` callback (BEP 19 §3.9's `interrupt` row) is not a
        fourth cause: since fix round 1, ``_emit_stream`` steers a truthy
        return into the running turn instead of ending it (see its own
        docstring), so it never produces ``interrupted``. It can still end a
        turn a different way — a raised ``AbortTurn`` — and that is
        **caught here and returned, not propagated** (fix round 2, I4), after
        :meth:`_run_turn` has told the native side to stop (fix round 3, D).
        ``Agent`` does the same with the identical signal
        (``agent.py:837-840``): it sets ``turn_status = "aborted"``, puts
        ``ABORTED_TURN_CONTENT`` in ``ctx.final_content``, and returns a
        normal ``AgentResult``. Propagating instead would make ``AgentActor``
        report ``status="error"`` and send ``"Turn failed: "`` with no text,
        where a BOS agent sends the marker as an ordinary reply — so this
        returns ``ABORTED_TURN_CONTENT`` with ``finish_reason="aborted"``.
        It commits **nothing**, and that is not an inconsistency with
        ``Agent``: ``Agent`` persists the marker to shape the *model's* own
        history so the next turn does not re-answer a dead request, and
        ``CodexAgent`` never replays BOS history into Codex — the native
        thread is the model's history — so that purpose does not transfer.

        Two concurrent turns on the same ``chat_id`` are rejected with a busy
        ``RuntimeError`` rather than queued — the native session is
        single-threaded (BEP 19 §3.10.1) — tracked via ``self._in_flight``,
        which also gives :meth:`aclose` something to interrupt and wait on.
        """
        turn_id = turn_id or uuid.uuid4().hex
        if chat_id in self._in_flight:
            raise RuntimeError(
                f"Agent {self._kind!r} already has a turn running on chat {chat_id!r}. "
                f"A Codex thread is single-threaded; wait for the turn to finish."
            )
        self._in_flight[chat_id] = None  # reserved synchronously — no await before this line
        try:
            thread, _ = await self._thread_for(chat_id, turn_id=turn_id)
            turn_kwargs = _compact(
                model=(llm_args or {}).get("model"),
                effort=(llm_args or {}).get("reasoning_effort"),
                output_schema=schema,
            )

            codex_input = _content_to_codex_input(content)
            structured_output: Any = None
            structured_ok = False
            retries = 0
            while True:
                try:
                    handle = await self._bounded_setup(
                        thread.turn(codex_input, **turn_kwargs),
                        phase="the turn request (thread.turn)",
                        chat_id=chat_id,
                        turn_id=turn_id,
                    )
                    result, interrupted_by_host = await self._run_turn(
                        handle,
                        event_sink,
                        chat_id=chat_id,
                        turn_id=turn_id,
                        ctx_metadata=ctx_metadata,
                        interrupt=interrupt,
                    )
                except TimeoutError:
                    # Both flavours pass straight through, unwrapped: the
                    # streaming deadline from _run_turn, and the setup
                    # deadline from _bounded_setup above. Each already carries
                    # its own runtime/kind/turn/chat message, and re-wrapping
                    # either as a generic turn failure would bury which one
                    # fired.
                    raise
                except AbortTurn:
                    # Agent CATCHES AbortTurn and returns (agent.py:837-840);
                    # propagating would make AgentActor report status="error"
                    # and send "Turn failed: " with no text, where a BOS agent
                    # sends the marker as an ordinary reply. Commits nothing:
                    # ABORTED_TURN_CONTENT shapes the MODEL's history, and
                    # CodexAgent never replays BOS history into Codex, so the
                    # marker is the caller's answer here, not stored context.
                    return external_agent_result(
                        output=ABORTED_TURN_CONTENT,
                        turn_id=turn_id,
                        usage=None,
                        finish_reason="aborted",
                    )
                except Exception as exc:
                    # A native TurnStatus.failed is raised before a TurnResult is
                    # ever built, on both the plain and the streaming path:
                    # openai_codex._run's own _raise_for_failed_turn runs inside
                    # _collect_async_turn_result ahead of the `TurnResult(...)`
                    # call, and `_emit_stream` mirrors that exact check (this
                    # module's own `_raise_for_failed_turn`) ahead of its own
                    # `TurnResult(...)` call — so neither `thread.turn()` nor
                    # `_emit_stream` ever hands back a `TurnResult` for a failed
                    # turn; this method's own status check below is unreachable
                    # for `failed`. Re-wrapped here so the runtime/agent/turn_id/
                    # chat_id context this method would attach to a
                    # `TurnResult`-shaped failure is not lost on the one path a
                    # real vendor call actually takes. Applies on a retry turn
                    # too: a native failure while sending a correction message is
                    # a new native failure, not one more validation attempt to
                    # retry.
                    raise RuntimeError(
                        f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                        f"{chat_id!r} failed: {exc}"
                    ) from exc

                if result.status is not TurnStatus.completed:
                    if not (interrupted_by_host and result.status is TurnStatus.interrupted):
                        # The only status left that reaches here as a normal
                        # TurnResult is `interrupted` (see TurnNotCompletedError
                        # and this method's own docstring); `failed` is handled
                        # above, and is never returned as a TurnResult by the
                        # real SDK to begin with. Commits nothing, same
                        # reasoning as the raise above: neither is a real
                        # answer BOS should treat as turn history.
                        raise TurnNotCompletedError(
                            f"{self._config.runtime} runtime {self._kind!r}: turn {turn_id!r} for chat "
                            f"{chat_id!r} ended with status {result.status.value!r}",
                            turn_result=result,
                        )
                    # BOS itself ended this turn early (see the docstring's
                    # "cooperative stop" case) — keep what it produced instead
                    # of raising. Never schema-checked below: the turn never
                    # finished normally, so a validation failure here would
                    # only replace one negative outcome with another that says
                    # nothing truer about what the model actually produced.
                    text = result.final_response or ""
                    break

                text = result.final_response or ""
                if schema is None:
                    break
                try:
                    structured_output = self._structured_validator.validate(text, schema)
                    structured_ok = True
                    break
                except StructuredOutputError as e:
                    if retries >= max_schema_retries:
                        # Exhausted: commits nothing, same as a native failure
                        # above — an unvalidated reply is not the answer `schema=`
                        # promised, so it is not turn history either.
                        raise
                    retries += 1
                    codex_input = (
                        f"Your previous response failed schema validation: {e}. "
                        "Reply ONLY with JSON matching the schema."
                    )

            output = structured_output if structured_ok else text
            usage = result.usage.last.model_dump() if result.usage is not None else None

            if self._chat_store is not None:
                commit = await commit_external_turn(
                    self._chat_store,
                    chat_id,
                    turn_id=turn_id,
                    user_content=content,
                    response=text,
                    runtime=self._config.runtime,
                    native_session_id=thread.id,
                    native_turn_id=result.id,
                    usage=usage,
                )
                if commit_observer is not None:
                    observed = commit_observer(commit)
                    if inspect.isawaitable(observed):
                        await observed

            return external_agent_result(
                output=output,
                structured=structured_ok,
                turn_id=turn_id,
                usage=usage,
                finish_reason=result.status.value,
            )
        finally:
            self._in_flight.pop(chat_id, None)

    async def native_messages(self, chat_id: str) -> list[Message]:
        """Codex's own transcript for *chat_id*, projected into BOS ``Message``s
        (BEP 19 §3.7) — what ``BosApp.get_messages(source="native")`` delegates
        to, and the only read here of state BOS does not own.

        **It promises nothing, by design.** BOS commits two messages per turn
        and guarantees those; this is a live read of the vendor's store. Codex
        compacts and prunes a thread on its own schedule, a turn's items can
        come back unloaded (see below), and a thread can be archived or
        deleted — so this can return *less* than it did last time, or raise,
        and neither is a bug in BOS. ``source="bos"`` is the read BOS stands
        behind.

        Four behaviours worth stating outright, because each is a place the
        honest answer and the convenient one differ:

        - **No native session means no transcript, not a missing one.**
          ``read_native_session_id`` returning ``None`` says this chat never
          ran a native turn (or has no chat store at all). That is an empty
          transcript: ``[]``, logged at DEBUG. A session id that exists and
          cannot be read is the opposite case, and raises.
        - **A turn Codex did not fully load becomes a visible gap.**
          ``Turn.items_view`` is ``notLoaded``/``summary``/``full``, and on
          anything but full the ``items`` are absent or summarized. Projecting
          them anyway would silently publish a shorter conversation than the
          one that happened — the exact lie §3.7 exists to prevent. Each such
          turn emits one marker ``Message`` carrying ``metadata["items_view"]``
          and logs at WARNING. It does not raise: a partly-unloaded old thread
          is ordinary, and raising would break the read for exactly the
          long-lived sessions someone wants to read. See
          ``_LOADED_ITEMS_VIEWS`` for why "full" is a membership test.
        - **Messages only — and the tool activity is not available from BOS at
          all, not merely from here.** ``ThreadItem`` is a nineteen-member
          union and only ``UserMessageThreadItem`` and
          ``AgentMessageThreadItem`` are messages. The other seventeen —
          reasoning, command execution, file change, MCP tool call, web
          search, plan, the inline ``ContextCompactionThreadItem`` marking
          where Codex compacted, … — are the turn's internal work, and they
          are skipped because a BOS ``Message`` cannot carry them without
          forging one. BOS tool activity is a *structural pair*: an assistant
          message advertising ``tool_calls``, then a ``role="tool"`` message
          whose ``tool_call_id`` matches it. Codex's transcript has no such
          pairing, so rendering a ``CommandExecutionThreadItem`` as one would
          mean inventing a call id and an assistant message Codex never
          produced — which a host displays as authoritative, not as a
          reconstruction. And there is no second place to look: ``event_sink``
          (§3.9) is a live stream emitted while *BOS itself* runs a turn,
          nothing under ``bos/core/`` persists it, and for the case this
          method exists for — a session BOS did not author — none was ever
          emitted.

          Among agent messages, ``commentary`` is skipped too and
          ``final_answer``/no-phase kept — the same rule
          ``_final_assistant_response_from_items`` applies, and for the same
          reason: commentary is not the answer. That helper is not reused,
          because it returns the one final answer for a single turn and every
          answer in the thread is wanted here.
        - **A read can still spawn the child and fail on auth.** It goes
          through :meth:`_ensure_client` like every other call here, so on a
          cold agent the read is what starts ``codex app-server``, and with
          the default ``auth="subscription"`` a missing login fails it in
          :meth:`_preflight_auth` — an error about the login, not about the
          thread. What it does not touch is the *session*: no
          ``thread_start``, no ``thread_resume`` (see the comment in the body).

        ``Message.turn_id`` is left ``None``: BOS turn ids are minted by
        :meth:`run`, and a thread BOS did not author has none. The native ids
        go in ``metadata`` instead, where they cannot be mistaken for one.
        ``created_at`` is the turn's ``started_at`` when Codex reports one —
        per turn is the finest granularity it offers — rather than letting
        every message in a year-old thread default to "now".
        """
        runtime = self._config.runtime
        native_session_id = (
            await read_native_session_id(self._chat_store, chat_id, runtime=runtime)
            if self._chat_store is not None
            else None
        )
        if native_session_id is None:
            logger.debug(
                "%s runtime %r: chat %r is bound to no native session, so its native transcript is empty",
                runtime,
                self._kind,
                chat_id,
            )
            return []

        # Constructed directly, NOT via _thread_for: that would re-establish
        # this thread with `thread_resume`, carrying this agent's sandbox,
        # approval mode and cwd — a write to a live session on what the caller
        # asked to be a read — and would wrap any failure in a
        # session-continuity message that is misleading here. AsyncThread is a
        # plain `@dataclass(slots=True)` of (_codex, id) (api.py:679-685), so
        # building one costs no RPC; `read()` is the only call made on it, and
        # `AsyncCodex` exposes no top-level `thread_read` to use instead.
        thread = AsyncThread(await self._ensure_client(), native_session_id)
        try:
            response = await asyncio.wait_for(thread.read(include_turns=True), self._config.timeout_seconds)
        except TimeoutError as exc:
            # Ahead of the wrap below for the same reason as in _thread_for: a
            # child that never answers is not a thread that cannot be read.
            raise TimeoutError(
                f"{runtime} runtime {self._kind!r}: reading the native transcript of thread "
                f"{native_session_id!r} for chat {chat_id!r} exceeded "
                f"timeout_seconds={self._config.timeout_seconds!r}"
            ) from exc
        except Exception as exc:
            # A missing or deleted thread has no representation in a
            # `ThreadReadResponse` — its `thread` field is required and not
            # nullable — so the vendor raises (a `CodexError` subclass, via
            # `map_jsonrpc_error`) rather than handing back an empty one.
            # Re-wrapped so the answer names the chat and the agent, which a
            # bare "JSON-RPC error -32602" does not — and still an error, never
            # an empty list, because "BOS cannot read it" and "there is nothing
            # to read" are different answers to the caller's question.
            raise RuntimeError(
                f"{runtime} runtime {self._kind!r}: the native transcript of thread "
                f"{native_session_id!r} for chat {chat_id!r} could not be read: {exc}"
            ) from exc

        messages: list[Message] = []
        for turn in response.thread.turns:
            created_at = datetime.fromtimestamp(turn.started_at) if turn.started_at is not None else datetime.now()
            if turn.items_view not in _LOADED_ITEMS_VIEWS:
                view = turn.items_view
                items_view = view.value if isinstance(view, TurnItemsView) else view
                logger.warning(
                    "%s runtime %r: chat %r, native turn %r came back with items_view=%r, so its "
                    "messages are absent or summarized; the transcript has a gap there",
                    runtime,
                    self._kind,
                    chat_id,
                    turn.id,
                    items_view,
                )
                messages.append(
                    Message(
                        # "system", not "assistant": this is a note from the
                        # reader about what is missing, not something either
                        # party said, and the role is what keeps a host from
                        # attributing it to one of them.
                        llm_message={
                            "role": "system",
                            "content": (
                                f"[{runtime}: this turn's messages were not loaded by the runtime "
                                f"(items_view={items_view!r}); the transcript is incomplete here.]"
                            ),
                        },
                        created_at=created_at,
                        metadata={"source": runtime, "native_turn_id": turn.id, "items_view": items_view},
                    )
                )
                continue
            for wrapped in turn.items:
                item = _unwrap_thread_item(wrapped)
                llm_message: dict[str, Any]
                if isinstance(item, UserMessageThreadItem):
                    llm_message = {"role": "user", "content": _codex_input_to_content(item.content)}
                elif isinstance(item, AgentMessageThreadItem) and item.phase is not MessagePhase.commentary:
                    llm_message = {"role": "assistant", "content": item.text}
                else:
                    continue
                messages.append(
                    Message(
                        llm_message=llm_message,
                        created_at=created_at,
                        metadata={"source": runtime, "native_turn_id": turn.id, "native_item_id": item.id},
                    )
                )
        return messages

    async def aclose(self) -> None:
        """Interrupt every in-flight turn, then close the client regardless of
        whether they wound down in time (BEP 19 §3.10.2).

        Setting ``_stop_requested`` is enough to make each in-flight
        :meth:`run` call interrupt its own turn and race it, exactly as
        ``request_stop()`` does; this only adds waiting for them and the
        client close. That wait is bounded by ``_ACLOSE_GRACE_SECONDS``
        because :meth:`_settle_interrupted`'s own inner bound does **not**
        guarantee the task is finished — it *abandons* a task it cannot kill,
        which then outlives the turn that owned it. A turn still winding down
        after the bound is reported and left behind; the client is closed
        anyway, since that is the only thing that reaps the child process.
        So this is a bounded drain, not a clean one: a wedged turn is given a
        window, not waited out.

        Also closes Task 4's hole: an ``aclose()`` racing an ``_ensure_client()``
        still in flight no longer leaks a client. Both now take
        ``_client_lock``, so this either finds no client built yet (nothing to
        close), or waits for the one being built and closes that.
        """
        self._stop_requested.set()
        tasks = [task for task in self._in_flight.values() if task is not None]
        if tasks:
            # Bounded because _settle_interrupted ABANDONS a task it cannot
            # kill (it cancels, waits out _INTERRUPT_GRACE_SECONDS, then
            # raises) — so a task can outlive the turn that owned it, and an
            # unbounded wait here would never reach the client.close() below,
            # which is the only thing that reaps the child process.
            _, pending = await asyncio.wait(tasks, timeout=_ACLOSE_GRACE_SECONDS)
            if pending:
                logger.warning(
                    "%s runtime %r: %d turn(s) still running after %ss; closing the client anyway",
                    self._config.runtime,
                    self._kind,
                    len(pending),
                    _ACLOSE_GRACE_SECONDS,
                )
        async with self._client_lock:
            if self._client is not None:
                await self._client.close()
