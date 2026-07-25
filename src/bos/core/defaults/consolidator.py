from __future__ import annotations

from typing import Any

from .._utils import _compact
from ..contract import Message, ep_consolidator
from ..llm import LLMClient


def _project_history(messages: list[Message]) -> list[dict[str, Any]]:
    """Flatten history to plain role/content text for the summarization prompt.

    Tool-call *linkage* (``tool_calls`` / ``tool_call_id``) is deliberately not
    carried over — a summary prompt has no use for it. But dropping it while
    keeping ``role: "tool"`` leaves an unpaired tool response, which strict
    providers reject outright ("Missing corresponding tool call for tool
    response message"), failing the whole consolidation. So tool traffic is
    rendered as text on ordinary roles instead: the content still informs the
    summary, and the prompt stays valid everywhere."""
    projected: list[dict[str, Any]] = []
    for message in messages:
        llm_message = message.llm_message
        role = llm_message.get("role")
        content = llm_message.get("content", "")
        if role == "tool":
            name = llm_message.get("name") or "tool"
            projected.append({"role": "user", "content": f"[tool result: {name}]\n{content}"})
            continue
        if role == "assistant" and (tool_calls := llm_message.get("tool_calls")):
            called = ", ".join(_tool_call_name(call) for call in tool_calls)
            content = f"{content}\n[called: {called}]".strip() if called else content
        projected.append(_compact({"role": role, "content": content}))
    return projected


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        function = call.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "tool")
        return str(call.get("name") or "tool")
    return "tool"


DEFAULT_COMPACTION_INSTRUCTION = """\
Provide a dense, concise summary of the conversation history for future turns.
Preserve user intent, decisions, unresolved tasks, tool results, and important constraints.
Keep it highly concise but retain all factual information without omitting details.
Avoid transcript style and verbose language. Keep the summary less than 2048 characters.
"""

SUMMARY_PREFIX = "Chat summary:\n"


@ep_consolidator(name="LLMConsolidator")
class LLMConsolidator:
    """LLM-backed harness-level consolidator."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        model: str | None = None,
        instruction: str | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._instruction = instruction or DEFAULT_COMPACTION_INSTRUCTION

    async def consolidate(self, messages: list[Message], instruction: str | None = None) -> str:
        existing_summaries, conversation = self._split_history(messages)
        prompt = self._build_prompt(
            instruction=instruction or self._instruction,
            existing_summaries=existing_summaries,
            conversation=conversation,
        )
        response = await self._llm.complete(prompt, model=self._model)
        return _strip_summary_prefix(response.text).strip()

    def _build_prompt(
        self,
        *,
        instruction: str,
        existing_summaries: list[str],
        conversation: list[Message],
    ) -> list[dict[str, Any]]:
        prompt: list[dict[str, Any]] = [{"role": "system", "content": instruction.strip()}]
        if existing_summaries:
            prompt.append({
                "role": "system",
                "content": "Existing summary context:\n" + "\n\n".join(existing_summaries),
            })
        prompt.extend(_project_history(conversation))
        prompt.append({
            "role": "user",
            "content": "Please provide the summary of the conversation above.",
        })
        return prompt

    @staticmethod
    def _split_history(messages: list[Message]) -> tuple[list[str], list[Message]]:
        summaries: list[str] = []
        conversation: list[Message] = []
        for message in messages:
            content = message.llm_message.get("content", "")
            if message.is_summary or _is_summary_content(message.llm_message):
                summaries.append(_strip_summary_prefix(str(content)))
            else:
                conversation.append(message)
        return summaries, conversation


def _is_summary_content(llm_message: dict[str, Any]) -> bool:
    return llm_message.get("role") == "system" and str(llm_message.get("content", "")).startswith(SUMMARY_PREFIX)


def _strip_summary_prefix(content: str) -> str:
    if content.startswith(SUMMARY_PREFIX):
        return content[len(SUMMARY_PREFIX) :].lstrip()
    return content
