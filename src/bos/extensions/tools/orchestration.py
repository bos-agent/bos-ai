import asyncio
import json
from pathlib import Path

from bos.core import ep_tool


@ep_tool(
    name="TodoRead",
    description="Read the current JSON task list / todo file from a specific path.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the JSON todo file."},
        },
        "required": ["path"],
    },
)
async def tool_todo_read(path: str) -> str:
    return await asyncio.to_thread(_sync_read_json, path)


@ep_tool(
    name="TodoWrite",
    description="Write an updated JSON task list / todo file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the JSON todo file."},
            "data": {"type": "object", "description": "The new JSON payload."},
        },
        "required": ["path", "data"],
    },
)
async def tool_todo_write(path: str, data: dict) -> str:
    return await asyncio.to_thread(_sync_write_json, path, data)


def _sync_read_json(path: str) -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return f"Error: JSON file '{path}' not found."
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error reading JSON: {e}"


def _sync_write_json(path: str, data: dict) -> str:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return f"Successfully saved JSON to {path}."
    except Exception as e:
        return f"Error saving JSON: {e}"
