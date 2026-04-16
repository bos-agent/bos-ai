from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ..protocol import Envelope, MessageType
from .agent import AbortTurn
from .contract import ep_actor_command
from .harness import CURRENT_HARNESS

logger = logging.getLogger(__name__)


class AgentActor:
    """Actor that drives an Agent via a Mailbox."""

    def __init__(self, address: str, agent: Any, mailbox: Any):
        self._address = address
        self._agent = agent
        self._mailbox = mailbox
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending: dict[str, list[Envelope]] = {}
        self._interrupts: dict[str, list[Envelope]] = {}

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def run(self) -> None:
        try:
            while True:
                for s in list(self._tasks.keys()):
                    if self._tasks[s].done():
                        if exc := self._tasks[s].exception():
                            import traceback

                            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                            logger.error("Ask task failed for sender=%s:\n%s", s, tb)
                            error_content = f"(error: {exc})\n\n```\n{tb}```"
                            await self._mailbox.send(Envelope(sender=self._address, recipient=s, content=error_content))
                        del self._tasks[s]

                        if s in self._pending and any(e.content_type == MessageType.MESSAGE for e in self._pending[s]):
                            self._fire_pending(s)

                env = await self._mailbox.receive_nowait(self._address)
                if env is None:
                    await asyncio.sleep(0.1)
                    continue

                sender = env.sender

                if env.content_type == MessageType.COMMAND:
                    if env.content.strip().startswith("/"):
                        asyncio.create_task(self._handle_command(env))
                        continue
                    env.content_type = MessageType.MESSAGE

                if sender not in self._tasks:
                    if env.content_type == MessageType.MESSAGE:
                        self._pending.setdefault(sender, []).append(env)
                        self._fire_pending(sender)
                    continue

                if env.content_type in (MessageType.INTERRUPT_MESSAGE, MessageType.INTERRUPT_ABORT):
                    self._interrupts.setdefault(sender, []).append(env)
                else:
                    self._pending.setdefault(sender, []).append(env)

        except asyncio.CancelledError:
            await self.aclose()
            raise

    def _fire_pending(self, sender: str) -> None:
        messages = [e for e in self._pending.pop(sender, []) if e.content_type == MessageType.MESSAGE]
        if not messages:
            return
        content = "\n\n".join(
            f"[from {e.sender} {e.timestamp.isoformat()}]: {e.content}" if len(messages) > 1 else e.content
            for e in messages
        )
        conversation_id = messages[-1].conversation_id or uuid.uuid4().hex
        self._interrupts[sender] = []
        self._tasks[sender] = asyncio.create_task(self._run_ask(sender, conversation_id, content))

    async def _run_ask(self, sender: str, conversation_id: str, content: str) -> None:
        while True:
            response = await self._agent.ask(
                conversation_id,
                content,
                interrupt=self._make_interrupt(sender),
                ctx_metadata={"sender": sender, "actor_address": self._address},
            )
            await self._mailbox.send(
                Envelope(sender=self._address, recipient=sender, content=response, conversation_id=conversation_id)
            )

            messages = [e for e in self._pending.pop(sender, []) if e.content_type == MessageType.MESSAGE]
            if not messages:
                break

            conversation_id = messages[-1].conversation_id or uuid.uuid4().hex
            content = "\n\n".join(
                f"[from {e.sender} {e.timestamp.isoformat()}]: {e.content}" if len(messages) > 1 else e.content
                for e in messages
            )

    async def _handle_command(self, env: Envelope) -> None:
        parts = env.content.split(None, 1)
        cmd_name, input = parts[0].lstrip("/"), "" if len(parts) == 1 else parts[1]

        if not ep_actor_command.has(cmd_name):
            result = f"Invalid command `{cmd_name}`"
        else:
            try:
                result = await ep_actor_command.invoke_async(
                    cmd_name, {"input": input, "env": env, "actor": self, "harness": CURRENT_HARNESS.get(None)}
                )
            except Exception as e:
                result = str(e)

        if result is None:
            result = "(done)"

        if not isinstance(result, (Envelope, str)):
            result = json.dumps(result, default=str)

        if isinstance(result, str):
            result = Envelope(
                sender=self._address,
                recipient=env.sender,
                content=result,
                content_type=MessageType.COMMAND_RESULT,
                conversation_id=env.conversation_id,
            )

        await self._mailbox.send(result)

    def _make_interrupt(self, sender: str):
        def _interrupt() -> dict[str, Any] | None:
            buf = self._interrupts.get(sender, [])
            parts: list[str] = []
            remaining: list[Envelope] = []
            for env in buf:
                if env.content_type == MessageType.INTERRUPT_ABORT:
                    self._interrupts[sender] = remaining
                    raise AbortTurn()
                if env.content_type == MessageType.INTERRUPT_MESSAGE:
                    parts.append(f"[from {env.sender}]: {env.content}")
                else:
                    remaining.append(env)
            self._interrupts[sender] = remaining
            if parts:
                return {"role": "user", "content": "\n\n".join(parts)}
            return None

        return _interrupt
