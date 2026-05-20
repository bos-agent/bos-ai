import asyncio
import os
import re
from pathlib import Path

from bos.core import ep_tool

_IGNORE_DIRS = {".git", ".pycache", "__pycache__", "node_modules", "venv", ".venv", ".uv", "dist", "build"}
_READ_FILES: set[Path] = set()


@ep_tool(
    name="ReadFile",
    description="Read a text file from the workspace. Supports pagination for large files.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read."},
            "line_offset": {
                "type": "integer",
                "description": "The line offset (0-indexed) to start reading from.",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to read. Default is 500, max is 5000.",
                "default": 500,
            },
        },
        "required": ["path"],
    },
)
async def tool_read_file(path: str, line_offset: int = 0, limit: int = 500) -> str:
    return await asyncio.to_thread(_sync_tool_read_file, path, line_offset, limit)


def _sync_tool_read_file(path: str, line_offset: int = 0, limit: int = 500) -> str:
    p = Path(path)
    if not p.exists():
        return f"Error: File '{path}' does not exist."
    if not p.is_file():
        return f"Error: '{path}' is not a file."

    limit = min(limit, 5000)
    try:
        lines = []
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(line_offset):
                if not f.readline():
                    break
            for _ in range(limit):
                line = f.readline()
                if not line:
                    break
                line_number = line_offset + len(lines) + 1
                lines.append(f"{line_number}\t{line}")
        _READ_FILES.add(p.resolve())
        return "".join(lines) or "(Reached end of file or file is empty)"
    except Exception as e:
        return f"Error reading file {path}: {e}"


@ep_tool(
    name="WriteFile",
    description="Write content to a text file in the workspace. Existing files must be read first.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file."},
            "content": {"type": "string", "description": "Content to write."},
        },
        "required": ["path", "content"],
    },
)
async def tool_write_file(path: str, content: str) -> str:
    return await asyncio.to_thread(_sync_tool_write_file, path, content)


def _sync_tool_write_file(path: str, content: str) -> str:
    p = Path(path)
    try:
        resolved = p.resolve(strict=False)
        if p.exists() and p.is_file() and resolved not in _READ_FILES:
            return f"Error: Refusing to overwrite existing file '{path}' before it has been read with ReadFile."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _READ_FILES.add(resolved)
        return f"Successfully wrote to {path}."
    except Exception as e:
        return f"Error writing to file {path}: {e}"


@ep_tool(
    name="EditFile",
    description="Surgical text replacement in a file (old_string -> new_string).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file."},
            "old_string": {"type": "string", "description": "Exact text to remove/replace."},
            "new_string": {"type": "string", "description": "New text to insert."},
            "line_offset": {
                "type": "integer",
                "description": (
                    "0-indexed line number to start searching for old_string. "
                    "Helpful if multiple identical strings exist."
                ),
                "default": 0,
            },
            "replace_all": {"type": "boolean", "description": "Replace all occurrences found in the file."},
        },
        "required": ["path", "old_string", "new_string"],
    },
)
async def tool_edit_file(
    path: str, old_string: str, new_string: str, line_offset: int = 0, replace_all: bool = False
) -> str:
    return await asyncio.to_thread(_sync_tool_edit_file, path, old_string, new_string, line_offset, replace_all)


def _sync_tool_edit_file(
    path: str, old_string: str, new_string: str, line_offset: int = 0, replace_all: bool = False
) -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return f"Error: File '{path}' does not exist."
    try:
        content = p.read_text(encoding="utf-8")
        if replace_all:
            if old_string not in content:
                return "Error: old_string not found in file."
            count = content.count(old_string)
            content = content.replace(old_string, new_string)
            p.write_text(content, encoding="utf-8")
            return f"Successfully replaced all {count} occurrences in {path}."

        # Support line_offset for jumping to the right occurrence block
        if line_offset > 0:
            lines = content.splitlines(keepends=True)
            if line_offset >= len(lines):
                return f"Error: line_offset {line_offset} is beyond file length ({len(lines)} lines)."

            # Find the character offset corresponding to this line
            char_offset = sum(len(line) for line in lines[:line_offset])
        else:
            char_offset = 0

        search_space = content[char_offset:]
        count = search_space.count(old_string)
        if count == 0:
            return f"Error: old_string not found at or after line {line_offset}."
        if count > 1:
            return (
                f"Error: old_string found {count} times at or after line {line_offset}. "
                "Provide a more specific old_string or set replace_all=true."
            )

        match_idx = content.find(old_string, char_offset)
        before = content[:match_idx]
        after = content[match_idx + len(old_string) :]
        content = before + new_string + after

        p.write_text(content, encoding="utf-8")
        return f"Successfully edited {path}."
    except Exception as e:
        return f"Error editing file {path}: {e}"


@ep_tool(
    name="GlobSearch",
    description="Find files by glob pattern.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The glob pattern (e.g. '**/*.py')."},
            "cwd": {"type": "string", "description": "Optional directory to run glob from.", "default": "."},
        },
        "required": ["pattern"],
    },
)
async def tool_glob_search(pattern: str, cwd: str = ".") -> str:
    return await asyncio.to_thread(_sync_tool_glob_search, pattern, cwd)


def _sync_tool_glob_search(pattern: str, cwd: str = ".") -> str:
    try:
        matches = [
            str(p) for p in Path(cwd).glob(pattern) if p.is_file() and not any(part in _IGNORE_DIRS for part in p.parts)
        ]
        if not matches:
            return "No files matched."

        return "\n".join(matches)
    except Exception as e:
        return f"Error with glob search: {e}"


@ep_tool(
    name="GrepSearch",
    description="Search file contents with a rg/grep pattern (supports context lines). Safely wraps the output.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The string/regex pattern to search for."},
            "cwd": {"type": "string", "description": "Directory to search from.", "default": "."},
        },
        "required": ["query"],
    },
)
async def tool_grep_search(query: str, cwd: str = ".") -> str:
    # Attempt to use 'rg' first, then 'grep'.
    cmd = None
    if os.system("command -v rg > /dev/null 2>&1") == 0:
        cmd = ["rg", "-n", "--heading", query, cwd]
    elif os.system("command -v grep > /dev/null 2>&1") == 0:
        cmd = ["grep", "-rnE", query, cwd]

    if cmd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = stdout.decode("utf-8", errors="replace")
            # Truncate at 100 matches to protect context sizes
            lines = text.split("\n")
            if len(lines) > 100:
                lines = lines[:100] + [f"... truncated ({len(lines) - 100} more lines)"]
            return "\n".join(lines).strip() or "No matches found."
        except asyncio.TimeoutError:
            return "Error: Grep command timed out."
        except Exception as e:
            return f"Error executing shell grep: {e}"
    else:
        # Fallback to python re
        try:
            compiled = re.compile(query)
        except re.error as e:
            return f"Invalid regex pattern: {e}"

        def _fallback_search() -> str:
            matches = []
            for p in Path(cwd).rglob("*"):
                if not p.is_file():
                    continue
                if any(part in _IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                    for i, line in enumerate(content.splitlines(), start=1):
                        if compiled.search(line):
                            matches.append(f"{p}:{i}:{line.strip()}")
                            if len(matches) > 100:
                                matches.append("... truncated (max 100 matches).")
                                return "\n".join(matches)
                except Exception:
                    pass  # skip binary or unreadable files
            return "\n".join(matches) or "No matches found."

        return await asyncio.to_thread(_fallback_search)
