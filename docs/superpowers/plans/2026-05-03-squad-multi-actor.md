# Squad: Multi-Actor Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `src/bos/squad/` extension to run multiple actors simultaneously with @mention-based routing.

**Architecture:** `ActorRegistry` owns @mention parsing and address resolution. `SquadActor(AgentActor)` + `SquadAgent(ReactAgent)` add attribution annotation and history filtering. `start_squad()` wires N actors + channels in a TaskGroup. One core change relaxes channel topology validation.

**Tech Stack:** Python >=3.13, asyncio, existing bos core primitives (AgentActor, ReactAgent, MailBox, MailRoute, Channel)

---

### Task 1: Relax channel topology validation

**Files:**
- Modify: `src/bos/config/workspace.py:509-521`

**Why first:** This is the single core change. Without it, channels can't target `agent@researcher` etc.

- [ ] **Step 1: Relax the validation**

In `_validate_channel_topology`, change lines 517-521 from rejecting unknown `agent@*` addresses to accepting any `agent@*` address:

```python
# src/bos/config/workspace.py, in _validate_channel_topology
# Replace lines 517-521:

# Before:
if channel.target_address.startswith("agent@"):
    if channel.target_address != actor_address:
        raise ValueError(
            f"Channel {channel.bind_address!r} targets unknown actor address {channel.target_address!r}."
        )
    continue

# After:
if channel.target_address.startswith("agent@"):
    continue
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `uv run pytest -q tests/test_workspace_runtime.py tests/test_runner.py tests/test_proc.py -v`
Expected: All existing channel-related tests PASS.

- [ ] **Step 3: Verify a non-main agent@ address is now accepted**

Run: `uv run python -c "
from bos.config.workspace import Workspace, ResolvedChannelConfig
# Should not raise for agent@custom
Workspace._validate_channel_topology(
    [ResolvedChannelConfig(name='Test', bind_address='channel@test', target_address='agent@custom')],
    actor_address='agent@main'
)
print('OK')
"`
Expected: prints "OK" with no error.

- [ ] **Step 4: Commit**

```bash
git add src/bos/config/workspace.py
git commit -m "fix(config): accept any agent@* address in channel topology validation"
```

---

### Task 2: ActorRegistry

**Files:**
- Create: `src/bos/squad/__init__.py`
- Create: `src/bos/squad/registry.py`
- Create: `tests/squad/__init__.py`
- Create: `tests/squad/test_registry.py`

- [ ] **Step 1: Create package init**

```python
# src/bos/squad/__init__.py
from bos.squad.registry import ActorRecord, ActorRegistry, RouteResult
from bos.squad.actor import SquadActor, SquadAgent
from bos.squad.runner import start_squad

__all__ = [
    "ActorRecord",
    "ActorRegistry",
    "RouteResult",
    "SquadActor",
    "SquadAgent",
    "start_squad",
]
```

- [ ] **Step 2: Create test init**

```python
# tests/squad/__init__.py
```

- [ ] **Step 3: Write failing tests for ActorRegistry**

```python
# tests/squad/test_registry.py
import pytest
from bos.squad.registry import ActorRegistry, ActorRecord, RouteResult


class FakeMailBox:
    def __init__(self, address):
        self.address = address


@pytest.fixture
def registry():
    reg = ActorRegistry()
    reg.register("main", FakeMailBox("agent@main"), is_default=True)
    reg.register("researcher", FakeMailBox("agent@researcher"))
    reg.register("reviewer", FakeMailBox("agent@reviewer"))
    return reg


class TestResolveAddress:
    def test_resolve_known_actor(self, registry):
        assert registry.resolve_address("researcher") == "agent@researcher"

    def test_resolve_default_when_none(self, registry):
        assert registry.resolve_address(None) == "agent@main"

    def test_resolve_default_when_unknown(self, registry):
        assert registry.resolve_address("unknown") == "agent@main"

    def test_resolve_mailbox(self, registry):
        mb = registry.resolve_mailbox("reviewer")
        assert mb.address == "agent@reviewer"

    def test_list_actors(self, registry):
        actors = registry.list_actors()
        assert set(actors.keys()) == {"main", "researcher", "reviewer"}
        assert actors["main"].is_default is True
        assert actors["researcher"].is_default is False


class TestRoute:
    def test_mention_at_start(self, registry):
        result = registry.route("@researcher find papers on X")
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers on X"

    def test_mention_with_hyphenated_name(self, registry):
        reg2 = ActorRegistry()
        reg2.register("main", FakeMailBox("agent@main"), is_default=True)
        reg2.register("code-reviewer", FakeMailBox("agent@code-reviewer"))
        result = reg2.route("@code-reviewer review this")
        assert result.target_address == "agent@code-reviewer"
        assert result.content == "review this"

    def test_no_mention_uses_default(self, registry):
        result = registry.route("hello world")
        assert result.target_address == "agent@main"
        assert result.target_actor is None
        assert result.content == "hello world"

    def test_mention_unknown_actor_treated_as_text(self, registry):
        result = registry.route("@unknown do something")
        assert result.target_address == "agent@main"
        assert result.target_actor is None
        assert result.content == "@unknown do something"

    def test_metadata_target_actor_overrides(self, registry):
        result = registry.route("do something", metadata={"target_actor": "reviewer"})
        assert result.target_address == "agent@reviewer"
        assert result.target_actor == "reviewer"
        assert result.content == "do something"

    def test_mention_wins_over_metadata(self, registry):
        result = registry.route(
            "@researcher find papers",
            metadata={"target_actor": "reviewer"},
        )
        assert result.target_address == "agent@researcher"
        assert result.target_actor == "researcher"
        assert result.content == "find papers"

    def test_no_default_raises(self):
        reg = ActorRegistry()
        reg.register("helper", FakeMailBox("agent@helper"))
        with pytest.raises(KeyError):
            reg.route("hello")

    def test_content_without_leading_mention_preserved(self, registry):
        result = registry.route("  @researcher find papers")
        assert result.target_address == "agent@main"
        assert result.content == "  @researcher find papers"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest -q tests/squad/test_registry.py -v`
Expected: All tests FAIL (ImportError or NameError).

- [ ] **Step 5: Implement ActorRegistry**

```python
# src/bos/squad/registry.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bos.core import MailBox


@dataclass
class ActorRecord:
    name: str
    address: str
    mailbox: MailBox
    is_default: bool = False


@dataclass
class RouteResult:
    target_address: str
    content: str
    target_actor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_MENTION_RE = re.compile(r"@([\w][\w-]*)\s+")


class ActorRegistry:
    def __init__(self) -> None:
        self._actors: dict[str, ActorRecord] = {}
        self._default: str | None = None

    def register(
        self, name: str, mailbox: MailBox, *, is_default: bool = False
    ) -> None:
        self._actors[name] = ActorRecord(
            name=name,
            address=mailbox.address,
            mailbox=mailbox,
            is_default=is_default,
        )
        if is_default:
            self._default = name

    def resolve_address(self, target_actor: str | None) -> str:
        if target_actor is not None and target_actor in self._actors:
            return self._actors[target_actor].address
        if self._default is not None:
            return self._actors[self._default].address
        raise KeyError(
            f"No actor for {target_actor!r} and no default configured"
        )

    def resolve_mailbox(self, target_actor: str | None) -> MailBox:
        if target_actor is not None and target_actor in self._actors:
            return self._actors[target_actor].mailbox
        if self._default is not None:
            return self._actors[self._default].mailbox
        raise KeyError(
            f"No actor for {target_actor!r} and no default configured"
        )

    def list_actors(self) -> dict[str, ActorRecord]:
        return dict(self._actors)

    def route(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> RouteResult:
        target_actor: str | None = None
        cleaned = content
        out_metadata = dict(metadata or {})

        m = _MENTION_RE.match(content)
        if m:
            name = m.group(1)
            if name in self._actors:
                target_actor = name
                cleaned = content[m.end():]
                out_metadata["target_actor"] = name

        if target_actor is None and metadata:
            target_actor = metadata.get("target_actor")
            if isinstance(target_actor, str) and target_actor not in self._actors:
                target_actor = None

        address = self.resolve_address(target_actor)
        return RouteResult(
            target_address=address,
            content=cleaned,
            target_actor=target_actor,
            metadata=out_metadata,
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest -q tests/squad/test_registry.py -v`
Expected: All 11 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add -f src/bos/squad/__init__.py src/bos/squad/registry.py tests/squad/__init__.py tests/squad/test_registry.py
git commit -m "feat(squad): add ActorRegistry with @mention parsing and routing"
```

---

### Task 3: SquadAgent — history filtering override

**Files:**
- Create: `src/bos/squad/actor.py`
- Create: `tests/squad/test_history.py`

- [ ] **Step 1: Write failing tests for the filter helper and SquadAgent**

```python
# tests/squad/test_history.py
import pytest
from bos.squad.actor import SquadAgent, _filter_tool_noise


class FakeMessageStore:
    def __init__(self, messages=None):
        self._messages = messages or []
        self.saved: list = []

    async def get_messages(self, chat_id, original=False):
        from bos.core import Message
        return [Message(llm_message=m) for m in self._messages]

    async def save_messages(self, chat_id, messages):
        self.saved.extend(messages)

    async def save_summary(self, chat_id, summary):
        pass

    async def list_chats(self):
        return {}


class FakeLLM:
    async def complete(self, messages, **kwargs):
        from bos.core import LLMResponse
        return LLMResponse(content="test response", finish_reason="stop")


class TestFilterToolNoise:
    def test_removes_tool_role_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hi!", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "1", "name": "echo", "content": "result"},
            {"role": "assistant", "content": "Done."},
        ]
        result = _filter_tool_noise(messages)
        assert len(result) == 3
        assert result[0] == {"role": "user", "content": "hello"}
        assert result[1] == {"role": "assistant", "content": "Hi!"}
        assert result[2] == {"role": "assistant", "content": "Done."}

    def test_preserves_non_tool_messages(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        assert _filter_tool_noise(messages) == messages

    def test_handles_empty_list(self):
        assert _filter_tool_noise([]) == []

    def test_strips_tool_calls_from_assistant_with_content(self):
        messages = [
            {"role": "assistant", "content": "Let me check.", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
        ]
        result = _filter_tool_noise(messages)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check."
        assert "tool_calls" not in result[0]

    def test_drops_assistant_with_only_tool_calls(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
        ]
        assert _filter_tool_noise(messages) == []


class TestSquadAgentHistory:
    @pytest.mark.asyncio
    async def test_filters_tool_noise_from_history(self):
        store = FakeMessageStore([
            {"role": "user", "content": "search for X"},
            {"role": "assistant", "content": "Looking...", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "1", "name": "search", "content": "found X"},
            {"role": "assistant", "content": "Found X."},
        ])
        agent = SquadAgent(
            message_store=store,
            memory=None,
            consolidator=None,
            skills_loader=None,
            llm=FakeLLM(),
            tools=[],
        )
        history = await agent._get_chat_history("abc123")
        assert len(history) == 3
        assert history[0] == {"role": "user", "content": "search for X"}
        assert history[1] == {"role": "assistant", "content": "Looking..."}
        assert history[2] == {"role": "assistant", "content": "Found X."}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/squad/test_history.py -v`
Expected: All tests FAIL (ImportError).

- [ ] **Step 3: Implement _filter_tool_noise and SquadAgent in actor.py**

```python
# src/bos/squad/actor.py
from __future__ import annotations

from typing import Any

from bos.core import ReactAgent


def _filter_tool_noise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            content = msg.get("content", "")
            if not content or not str(content).strip():
                continue
            cleaned.append({"role": "assistant", "content": str(content)})
        else:
            cleaned.append(msg)
    return cleaned


class SquadAgent(ReactAgent):
    """ReactAgent that filters tool-call noise from shared chat history."""

    async def _get_chat_history(self, chat_id: str) -> list[dict[str, Any]]:
        history = await super()._get_chat_history(chat_id)
        return _filter_tool_noise(history)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/squad/test_history.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add -f src/bos/squad/actor.py tests/squad/test_history.py
git commit -m "feat(squad): add SquadAgent with tool-noise filtering"
```

---

### Task 4: SquadActor — attribution in merged messages

**Files:**
- Modify: `src/bos/squad/actor.py` (append SquadActor class)
- Modify: `tests/squad/test_actor.py` (create)

This is the point where SquadActor inherits from AgentActor and uses SquadAgent when creating the agent. But since SquadAgent(ReactAgent) needs to wire into the harness's `create_agent`, we'll handle the SquadActor <-> SquadAgent connection in the runner (Task 5). For now, SquadActor adds the attribution annotation to incoming messages.

- [ ] **Step 1: Write failing test for SquadActor._merge_pending_messages**

```python
# tests/squad/test_actor.py
from datetime import datetime
from bos.protocol import Envelope, MessageType
from bos.squad.actor import SquadActor


class FakeAgent:
    async def ask(self, chat_id, message, **kwargs):
        return f"echo: {message}"


class FakeMailBox:
    def __init__(self):
        self.address = "agent@test"
        self.sent: list = []

    async def send(self, recipient, content, **kwargs):
        self.sent.append((recipient, content, kwargs))

    async def receive_nowait(self):
        return None


class TestMergePendingMessages:
    def test_annotates_with_target_actor(self):
        actor = SquadActor(
            FakeAgent(), FakeMailBox(), actor_name="researcher"
        )
        env1 = Envelope(
            sender="channel@http",
            recipient="agent@researcher",
            content="find papers",
            content_type=MessageType.MESSAGE,
            chat_id="abc123",
            timestamp=datetime.now(),
            metadata={"target_actor": "researcher"},
        )
        result = actor._merge_pending_messages([env1])
        parts = result if isinstance(result, list) else [{"type": "text", "text": str(result)}]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        assert "[user → @researcher]" in text
        assert "find papers" in text

    def test_no_target_actor_no_annotation(self):
        actor = SquadActor(
            FakeAgent(), FakeMailBox(), actor_name="main"
        )
        env1 = Envelope(
            sender="channel@http",
            recipient="agent@main",
            content="hello",
            content_type=MessageType.MESSAGE,
            chat_id="abc123",
            timestamp=datetime.now(),
        )
        result = actor._merge_pending_messages([env1])
        text = str(result)
        assert "[user →" not in text
        assert "hello" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/squad/test_actor.py -v`
Expected: FAIL (SquadActor not defined).

- [ ] **Step 3: Add SquadActor to actor.py**

Append to `src/bos/squad/actor.py`:

```python
from bos.core import AgentActor
from bos.protocol import Envelope, MessageContent, MessageType
from bos.protocol.content import content_as_parts


class SquadActor(AgentActor):
    def __init__(self, agent, mailbox, chat_state=None, *, actor_name=None):
        super().__init__(agent, mailbox, chat_state)
        self.actor_name = actor_name

    def _merge_pending_messages(
        self, messages: list[Envelope]
    ) -> MessageContent:
        merged = super()._merge_pending_messages(messages)
        if self.actor_name is None:
            return merged

        attribution = f"[user → @{self.actor_name}]: "
        if isinstance(merged, str):
            return attribution + merged

        if isinstance(merged, list):
            return [{"type": "text", "text": attribution}] + merged

        return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/squad/test_actor.py tests/squad/test_history.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add -f src/bos/squad/actor.py tests/squad/test_actor.py
git commit -m "feat(squad): add SquadActor with target_actor attribution"
```

---

### Task 5: start_squad runner

**Files:**
- Create: `src/bos/squad/runner.py`
- Create: `tests/squad/test_runner.py`

- [ ] **Step 1: Write failing integration-style test for start_squad**

```python
# tests/squad/test_runner.py
import pytest
from bos.squad.runner import _parse_actors_config


class TestParseActorsConfig:
    def test_parses_actors_section(self):
        config = {
            "main": {
                "actors": {
                    "researcher": {"agent": "researcher"},
                    "reviewer": {"agent": "reviewer"},
                    "main": {"agent": "main"},
                }
            }
        }
        actors = _parse_actors_config(config)
        assert len(actors) == 3
        assert actors["researcher"] == {"agent": "researcher"}
        assert actors["reviewer"] == {"agent": "reviewer"}
        assert actors["main"] == {"agent": "main"}

    def test_no_actors_section_returns_empty(self):
        actors = _parse_actors_config({"main": {}})
        assert actors == {}

    def test_default_agent_name(self):
        actors = _parse_actors_config({
            "main": {
                "agent": "orchestrator",
                "actors": {
                    "helper": {"agent": "helper"},
                },
            }
        })
        assert len(actors) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/squad/test_runner.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement config parsing and start_squad**

```python
# src/bos/squad/runner.py
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bos.config import Workspace

logger = logging.getLogger(__name__)


def _parse_actors_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    main = config.get("main", {})
    if not isinstance(main, dict):
        return {}
    actors = main.get("actors", {})
    if not isinstance(actors, dict):
        return {}
    return {str(k): dict(v) for k, v in actors.items() if isinstance(v, dict)}


async def start_squad(workspace: Workspace) -> None:
    from bos.core import AgentActor, Channel, _create_extension_instance, ep_channel
    from bos.core.chat_state import ChatState
    from bos.squad.actor import SquadActor
    from bos.squad.registry import ActorRegistry

    actors_cfg = _parse_actors_config(workspace.config)
    channels_cfg = workspace.resolve_channels(
        runtime_kind=os.environ.get("BOS_RUNTIME", "process")
    )

    if not actors_cfg:
        # Backwards compat: single main actor, no registry
        agent_name = workspace.get_main_agent_name()
        actor_address = workspace.get_main_agent_address()
        logger.info(
            "No [main.actors] configured; starting single actor agent=%r",
            agent_name,
        )
        async with workspace.harness() as harness:
            chat_state = ChatState(workspace.bos_dir)
            agent = harness.create_agent(agent_name)
            actor = AgentActor(
                agent, harness.mail_route.bind(actor_address), chat_state=chat_state
            )
            channels = _create_channels(channels_cfg, ep_channel, Channel)
            await _run_actor_and_channels(actor, channels, harness)
        return

    actor_names = list(actors_cfg.keys())
    logger.info(
        "Starting squad with %d actor(s): %s",
        len(actor_names),
        ", ".join(actor_names),
    )

    async with workspace.harness() as harness:
        chat_state = ChatState(workspace.bos_dir)
        registry = ActorRegistry()
        actors: list[SquadActor] = []

        for routing_name, cfg in actors_cfg.items():
            agent_name = cfg.get("agent", routing_name)
            address = f"agent@{routing_name}"
            mailbox = harness.mail_route.bind(address)
            is_default = routing_name == "main"

            agent = _build_squad_agent(harness, agent_name, workspace.config)
            actor = SquadActor(
                agent, mailbox, chat_state=chat_state, actor_name=routing_name
            )
            actors.append(actor)
            registry.register(routing_name, mailbox, is_default=is_default)

        channels = _create_channels(channels_cfg, ep_channel, Channel, registry=registry)

        async with asyncio.TaskGroup() as tg:
            for actor in actors:
                tg.create_task(actor.run(), name=f"actor:{actor.actor_name}")
            for ch, address in channels:
                tg.create_task(
                    ch.run(harness.mail_route.bind(address)),
                    name=f"channel:{address}",
                )


def _create_channels(
    channels_cfg, ep_channel, Channel, registry=None
) -> list[tuple[Channel, str]]:
    from bos.core import _create_extension_instance

    channels: list[tuple[Channel, str]] = []
    for cfg in channels_cfg:
        ext_cfg = cfg.extension_config()
        if registry is not None:
            ext_cfg["actor_registry"] = registry
        ch = _create_extension_instance(ep_channel, Channel, ext_cfg)
        if ch is None:
            logger.warning("Could not create channel from config: %r", cfg)
            continue
        channels.append((ch, cfg.bind_address))
    return channels


async def _run_actor_and_channels(
    actor, channels, harness
) -> None:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(actor.run(), name="actor")
        for ch, address in channels:
            tg.create_task(
                ch.run(harness.mail_route.bind(address)),
                name=f"channel:{address}",
            )


def _build_squad_agent(harness, agent_name: str, config: dict[str, Any]):
    """Build a SquadAgent with config from platform.agents + harness services."""
    from bos.squad.actor import SquadAgent

    agents = config.get("platform", {}).get("agents", [])
    agent_spec: dict[str, Any] = {}
    for a in agents:
        if isinstance(a, dict) and a.get("name") == agent_name:
            agent_spec = {k: v for k, v in a.items() if k != "name"}
            break

    defaults = config.get("platform", {}).get("agent_defaults", {})
    if isinstance(defaults, dict):
        for k, v in defaults.items():
            agent_spec.setdefault(k, v)

    return SquadAgent(
        name=agent_name,
        llm=harness.llm,
        message_store=harness.message_store,
        memory=harness.memory,
        consolidator=harness.consolidator,
        skills_loader=harness.skills_loader,
        interceptor=harness.interceptor,
        tool_configs=harness._tools_cfg,
        **agent_spec,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/squad/test_runner.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add -f src/bos/squad/runner.py tests/squad/test_runner.py
git commit -m "feat(squad): add start_squad runner with multi-actor wiring"
```

---

### Task 6: HttpChannel — use ActorRegistry for routing

**Files:**
- Modify: `src/bos/extensions/channels/http.py`
- Create: `tests/squad/test_http_routing.py`

- [ ] **Step 1: Write failing test for channel routing**

```python
# tests/squad/test_http_routing.py
import asyncio
import json
import pytest
from aiohttp import web
from bos.extensions.channels.http import HttpChannel


class FakeRegistry:
    def __init__(self):
        self.routes = []
    def route(self, content, metadata=None):
        self.routes.append((content, metadata))
        from bos.squad.registry import RouteResult
        target = (metadata or {}).get("target_actor", "main")
        return RouteResult(
            target_address=f"agent@{target}",
            content=content,
            target_actor=target if target != "main" else None,
            metadata=dict(metadata or {}),
        )


class FakeMailBox:
    def __init__(self):
        self.address = "channel@test"
        self.sent: list = []

    async def send(self, recipient, content, **kwargs):
        self.sent.append((recipient, content, kwargs))

    async def receive(self):
        await asyncio.sleep(10)
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_channel_uses_registry_for_routing(aiohttp_client):
    registry = FakeRegistry()
    mailbox = FakeMailBox()
    channel = HttpChannel(
        target_address="agent@main",
        actor_registry=registry,
        port=0,
    )
    app = channel._build_app(mailbox)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/send",
        json={
            "content": "@researcher find papers",
            "chat_id": "abc123",
            "content_type": "message",
        },
    )
    assert resp.status == 202
    data = await resp.json()
    assert data["ok"] is True

    # Verify registry was consulted
    assert len(registry.routes) == 1
    called_content, _ = registry.routes[0]
    assert called_content == "@researcher find papers"

    # Verify message was sent to resolved address
    assert len(mailbox.sent) == 1
    recipient, _, _ = mailbox.sent[0]
    assert recipient == "agent@researcher"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/squad/test_http_routing.py -v`
Expected: FAIL (actor_registry not accepted by HttpChannel).

- [ ] **Step 3: Add actor_registry support to HttpChannel**

In `src/bos/extensions/channels/http.py`:

Add parameter to `__init__`:

```python
# In HttpChannel.__init__, add parameter:
def __init__(
    self,
    target_address: str,
    host: str = "127.0.0.1",
    port: int = 5920,
    upload_dir: str | Path | None = None,
    max_upload_bytes: int = 20 * 1024 * 1024,
    bos_dir: str | Path | None = None,
    chat_state_path: str | Path | None = None,
    actor_registry: Any = None,  # ADD THIS
) -> None:
    # ... existing code ...
    self._actor_registry = actor_registry  # ADD THIS
```

In `_build_app`, store the registry:

```python
# In _build_app, add:
app[APP_ACTOR_REGISTRY] = self._actor_registry  # ADD THIS
```

Add new AppKey at module level:

```python
APP_ACTOR_REGISTRY = web.AppKey("actor_registry", object)  # ADD THIS
```

In `_send_handler`, use registry for routing. Replace the send call:

```python
# In _send_handler, before mailbox.send, add:
registry = request.app.get(APP_ACTOR_REGISTRY)
if registry is not None:
    from bos.squad.registry import ActorRegistry
    result = registry.route(
        str(env.content) if isinstance(env.content, str) else "",
        metadata=env.metadata,
    )
    recipient = result.target_address
    content = result.content
else:
    recipient = env.recipient
    content = env.content

await mailbox.send(
    recipient,
    content,
    content_type=env.content_type,
    chat_id=chat_id,
    metadata=metadata,
)
```

In `_ws_handler`, inside the `async for msg in ws:` loop, add registry routing before `mailbox.send`. Replace the existing `mailbox.send` call (lines 223-229) with:

```python
# In _ws_handler, replace the mailbox.send call:
registry = request.app.get(APP_ACTOR_REGISTRY)
if registry is not None:
    route_result = registry.route(
        str(env.content) if isinstance(env.content, str) else "",
        metadata=metadata,
    )
    recipient = route_result.target_address
    content = route_result.content
else:
    recipient = env.recipient
    content = env.content

await mailbox.send(
    recipient,
    content,
    content_type=env.content_type,
    chat_id=conn.chat_id,
    metadata=metadata,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/squad/test_http_routing.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add -f src/bos/extensions/channels/http.py tests/squad/test_http_routing.py
git commit -m "feat(squad): wire ActorRegistry into HttpChannel for @mention routing"
```

---

### Task 7: Backwards compatibility — no [main.actors] path

**Files:**
- Modify: `tests/squad/test_runner.py` (add backwards compat test)
- No production code changes needed (handled in start_squad)

- [ ] **Step 1: Add backwards compat test**

Append to `tests/squad/test_runner.py`:

```python
class TestBackwardsCompat:
    def test_no_actors_config_returns_empty(self):
        actors = _parse_actors_config({})
        assert actors == {}

    def test_empty_main_section(self):
        actors = _parse_actors_config({"main": {}})
        assert actors == {}

    def test_actors_not_a_dict(self):
        actors = _parse_actors_config({"main": {"actors": "not-a-dict"}})
        assert actors == {}
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest -q tests/squad/test_runner.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -q`
Expected: All existing tests still PASS.

- [ ] **Step 4: Commit**

```bash
git add -f tests/squad/test_runner.py
git commit -m "test(squad): add backwards compatibility tests for no-actors config"
```

---

### Task 8: Final integration — lint and full suite

- [ ] **Step 1: Run linter**

Run: `uv run ruff check src/squad tests/squad`
Expected: No new lint findings.

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -q`
Expected: All tests PASS.

- [ ] **Step 3: Final commit**

```bash
git add -f src/squad/ tests/squad/
git commit -m "chore(squad): finalize squad multi-actor extension"
```
