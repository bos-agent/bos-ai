from __future__ import annotations

from typing import Any

from ..contract import Message, ep_consolidator
from ..history import project_message_history
from ..llm import LLMClient

DEFAULT_COMPACTION_INSTRUCTION = """\
Summarize the conversation history for future turns.
Preserve user intent, decisions, unresolved tasks, tool results, and important constraints.
Avoid transcript style.
"""

SUMMARY_PREFIX = "Chat summary:"


@ep_consolidator(name="_default")
class LLMConsolidator:
    """LLM-backed harness-level consolidator."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        model: str,
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
            prompt.append(
                {
                    "role": "system",
                    "content": "Existing summary context:\n" + "\n\n".join(existing_summaries),
                }
            )
        prompt.extend(project_message_history(conversation))
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
