from bos.protocol.content import content_preview

from ..contract import ep_consolidator


@ep_consolidator(name="_default")
class NaiveConsolidator:
    """Naive content consolidator that take the last 10 messages and concatenate them."""

    async def consolidate(self, messages: list[dict], instruction: str | None = None) -> str:
        summary = None
        for role, content in ((m.get("role"), m.get("content", "")) for m in messages if not m.get("tool_calls")):
            if summary is None and role not in ["user", "system"]:
                continue
            preview = content_preview(content, limit=200)
            summary = (summary or "") + (preview if role == "system" else f"{role}: {preview.strip()}") + "\n"
        return summary.strip()
