"""AgentStepInterceptor — relays agent execution stages to the caller via mailbox.

Sends ``Envelope(content_type="agent_step")`` envelopes so that remote clients
(e.g. the TUI connected via WebSocket) can display real-time progress.

Configuration (in ``config.toml``)::

    harness.interceptors = ["AgentStepRelay"]
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from bos.core import (
    CURRENT_HARNESS,
    CURRENT_MAILBOX,
    ReactContext,
    ep_react_interceptor,
)
from bos.protocol import MessageType

logger = logging.getLogger(__name__)


@ep_react_interceptor(name="AgentStepRelay")
class AgentStepInterceptor:
    """Relays execution-stage events to the original sender via the mailbox.

    Reads ``context.metadata["sender"]`` and ``context.metadata["actor_address"]``
    (set by ``AgentActor._run_ask``) to route step envelopes back to the correct
    client.
    """

    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
        ],
        context: ReactContext,
    ) -> None:
        sender = context.metadata.get("sender")
        actor_address = context.metadata.get("actor_address")
        if not sender:
            return

        harness = CURRENT_HARNESS.get(None)
        if not harness:
            return

        info: dict[str, Any] = {
            "stage": stage,
            "turn_id": context.turn_id,
            "conversation_id": context.conversation_id,
        }

        if stage == "before_llm":
            info["detail"] = "thinking"

        elif stage == "after_llm":
            resp = context.current_llm_response
            if resp and resp.tool_calls:
                info["detail"] = "tool_calls"
                info["tool_calls"] = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in resp.tool_calls
                ]
            else:
                info["detail"] = "response_ready"

        elif stage == "after_tool":
            last = context.current[-1].llm_message if context.current else {}
            info["detail"] = "tool_result"
            info["tool_name"] = last.get("name", "unknown")
            result_text = str(last.get("content", ""))
            info["tool_result"] = result_text[:200] + ("…" if len(result_text) > 200 else "")

        elif stage == "final_response":
            info["detail"] = "final"
            info["content"] = context.final_content or ""

        elif stage == "max_iteration":
            info["detail"] = "max_iteration"

        else:
            # prepare, etc. — skip to avoid noise
            return

        try:
            mailbox = CURRENT_MAILBOX.get(None)
            if mailbox is None:
                if not actor_address or not harness.mail_route:
                    return
                mailbox = harness.mail_route.bind(actor_address)

            await mailbox.send(
                sender,
                json.dumps(info, default=str),
                content_type=MessageType.AGENT_STEP,
                conversation_id=context.conversation_id,
            )
        except Exception:
            logger.debug("AgentStepInterceptor send error", exc_info=True)
