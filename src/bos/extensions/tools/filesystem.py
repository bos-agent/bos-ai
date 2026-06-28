import asyncio
import fnmatch
import os
import re
from collections.abc import Sequence
from pathlib import Path

from bos.core import ep_tool

# Directories skipped by GlobSearch/GrepSearch by default. Includes the
# workspace's `.bos/` control directory. Both tools resolve their effective
# ignore set via _resolve_ignore_dirs, configurable per tool through
# [exts.ep_tool.<Tool>] replace_ignore / extend_ignore / remove_ignore.
_IGNORE_DIRS = {".git", ".pycache", "__pycache__", "node_modules", "venv", ".venv", ".uv", "dist", "build", ".bos"}
_READ_FILES: set[Path] = set()


def _resolve_ignore_dirs(
    replace_ignore: Sequence[str] | None,
    extend_ignore: Sequence[str] | None,
    remove_ignore: Sequence[str] | None,
) -> set[str]:
    """Resolve the effective ignore set for a search tool.

    ``replace_ignore`` replaces the default ``_IGNORE_DIRS`` outright;
    ``extend_ignore`` unions onto it (``replace_ignore`` wins if both are given).
    ``remove_ignore`` then subtracts from the resulting set — e.g.
    ``remove_ignore=[".bos"]`` to search the control directory.
    """
    if replace_ignore is not None:
        base = set(replace_ignore)
    elif extend_ignore is not None:
        base = _IGNORE_DIRS | set(extend_ignore)
    else:
        base = set(_IGNORE_DIRS)
    if remove_ignore is not None:
        base -= set(remove_ignore)
    return base


# GrepSearch output limits — prevent context-window poisoning from
# minified bundles, giant JSON tokenizer files, or broad pattern matches.
_MAX_GREP_LINES = 100
_MAX_GREP_BYTES = 100_000
_MAX_LINE_BYTES = 4_000


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
    usage="""Read a text file from the workspace.

Use when you know the file path or need a focused line range. Before editing an existing file,
read the relevant current content so edits match the actual code. For unknown files or symbols,
search first with GlobSearch or GrepSearch.

Guidelines:
- Use line_offset and limit for large files or focused inspection.
- Results include 1-based line numbers; use them when referencing code.
- Read enough surrounding context to understand the existing pattern.
- Do not reread immediately after a successful EditFile/WriteFile unless semantic verification requires it.
""",
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
    usage="""Write full file contents.

Use mainly for new files or deliberate complete rewrites. Prefer EditFile for localized changes
to existing files. Before overwriting an existing file, inspect its current content with ReadFile.

Guidelines:
- WriteFile refuses to overwrite an existing file until that file has been read with ReadFile.
- Avoid creating documentation, plans, or broad new files unless the task requires them.
- Preserve existing style and formatting when rewriting.
- After writing meaningful code, verify with an appropriate test, import, or focused inspection.
""",
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
    usage="""Edit an existing file by exact text replacement.

Use for precise, localized changes. Choose old_string with enough surrounding context to target
the intended location. Use replace_all only when every occurrence should change.

Guidelines:
- Read the relevant file content before editing.
- Preserve indentation and nearby style.
- EditFile fails when old_string is ambiguous; include more context or use replace_all deliberately.
- If the edit fails, search or reread the file and adjust the replacement; do not guess.
""",
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
    usage="""Find files by glob pattern.

Use for path discovery when you know filename shapes or extensions. Prefer focused patterns over
broad repository-wide scans.

Examples:
- Find Python tests: pattern="tests/**/*.py"
- Find source modules: pattern="src/**/*.py"
""",
)
async def tool_glob_search(
    pattern: str,
    cwd: str = ".",
    replace_ignore: Sequence[str] | None = None,
    extend_ignore: Sequence[str] | None = None,
    remove_ignore: Sequence[str] | None = None,
) -> str:
    # replace_ignore / extend_ignore / remove_ignore are config-only knobs (see
    # _resolve_ignore_dirs), set via [exts.ep_tool.GlobSearch]. Ignore entries match
    # directory names in the path; unlike GrepSearch, GlobSearch does not interpret
    # them as file globs.
    ignore_dirs = _resolve_ignore_dirs(replace_ignore, extend_ignore, remove_ignore)
    return await asyncio.to_thread(_sync_tool_glob_search, pattern, cwd, ignore_dirs)


def _sync_tool_glob_search(pattern: str, cwd: str, ignore_dirs: set[str]) -> str:
    try:
        matches = [
            str(p) for p in Path(cwd).glob(pattern) if p.is_file() and not any(part in ignore_dirs for part in p.parts)
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
    usage="""Search file contents by string or regex.

Use for symbols, call sites, configuration keys, error messages, and behavior discovery. Search
before assuming names or locations, then use ReadFile on the most relevant hits.

Guidelines:
- Prefer specific queries over broad terms.
- Follow imports, tests, and references when a change may cross file boundaries.
- Treat no matches as evidence to refine the query, not proof that the concept does not exist.
""",
)
async def tool_grep_search(
    query: str,
    cwd: str = ".",
    replace_ignore: Sequence[str] | None = None,
    extend_ignore: Sequence[str] | None = None,
    remove_ignore: Sequence[str] | None = None,
) -> str:
    # replace_ignore / extend_ignore / remove_ignore are config-only knobs (see
    # _resolve_ignore_dirs), set via [exts.ep_tool.GrepSearch]. Entries may be directory
    # names (".venv") or file globs ("*.min.js") — _is_file_glob routes each to the
    # right rg/grep flag.
    exclude_patterns: list[str] = sorted(_resolve_ignore_dirs(replace_ignore, extend_ignore, remove_ignore))

    # Attempt to use 'rg' first, then 'grep'.
    cmd = None
    if os.system("command -v rg > /dev/null 2>&1") == 0:
        # rg -g handles both directory and file glob patterns.
        ignore_globs: list[str] = []
        for pat in exclude_patterns:
            ignore_globs.extend(["-g", f"!{pat}" if not pat.startswith("!") else pat])
        # -M caps per-match output bytes so a single giant line (minified JS,
        # tokenizer JSON) cannot poison the context window.
        cmd = ["rg", "-n", "--heading", "-M", str(_MAX_LINE_BYTES), *ignore_globs, query, cwd]
    elif os.system("command -v grep > /dev/null 2>&1") == 0:
        cmd = ["grep", "-rnE"]
        for pat in exclude_patterns:
            if _is_file_glob(pat):
                cmd.extend(["--exclude", pat])
            else:
                cmd.extend(["--exclude-dir", pat])
        cmd.extend([query, cwd])

    if cmd:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            text = stdout.decode("utf-8", errors="replace")
            return _truncate_grep_output(text)
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
            # Separate exclude list into dir names and file globs.
            exclude_dirs: set[str] = set()
            exclude_globs: list[str] = []
            for pat in exclude_patterns:
                if _is_file_glob(pat):
                    exclude_globs.append(pat)
                else:
                    exclude_dirs.add(pat)

            for p in Path(cwd).rglob("*"):
                if not p.is_file():
                    continue
                if any(part in exclude_dirs for part in p.parts):
                    continue
                if any(fnmatch.fnmatch(p.name, g) for g in exclude_globs):
                    continue
                try:
                    content = p.read_text(encoding="utf-8")
                    for i, line in enumerate(content.splitlines(), start=1):
                        if compiled.search(line):
                            stripped = line.strip()
                            matches.append(f"{p}:{i}:{stripped}")
                            if len(matches) > _MAX_GREP_LINES:
                                matches.append("... truncated (max 100 matches).")
                                return "\n".join(matches)
                except Exception:
                    pass  # skip binary or unreadable files
            return _truncate_grep_output("\n".join(matches)) or "No matches found."

        return await asyncio.to_thread(_fallback_search)


def _is_file_glob(pattern: str) -> bool:
    """Return True when *pattern* looks like a file-name glob, not a dir name.

    Heuristic: the pattern contains a glob meta-character (``*``, ``?``, ``[``)
    or a ``.`` extension separator (``*.js``, ``*.py``).  Plain names like
    ``.venv`` or ``node_modules`` are assumed to be directory names.
    """
    return any(c in pattern for c in "*?[") or (pattern.startswith("*.") or ".?" in pattern)


def _truncate_grep_output(text: str) -> str:
    """Truncate GrepSearch output to protect context windows.

    Guards against two failure modes:
    1. Too many lines (truncated at _MAX_GREP_LINES).
    2. Too many total bytes (truncated at _MAX_GREP_BYTES), with
       individual lines capped at _MAX_LINE_BYTES to handle minified
       JS / single-line JSON dumps that the shell tool cannot split.
    """
    lines = text.split("\n")
    truncated = False

    # Per-line cap (handles shell rg/grep paths where -M may be unavailable).
    capped_lines: list[str] = []
    for line in lines:
        if len(line) > _MAX_LINE_BYTES:
            capped_lines.append(line[:_MAX_LINE_BYTES] + "...")
        else:
            capped_lines.append(line)

    # Line-count cap.
    if len(capped_lines) > _MAX_GREP_LINES:
        capped_lines = capped_lines[:_MAX_GREP_LINES]
        capped_lines.append(f"... truncated ({len(lines) - _MAX_GREP_LINES} more lines)")
        truncated = True

    # Byte-count cap.  Walk backward to keep the truncation guard visible.
    output = "\n".join(capped_lines)
    if len(output) > _MAX_GREP_BYTES:
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_GREP_BYTES:
            # Chop at a valid UTF-8 boundary.
            encoded = encoded[:_MAX_GREP_BYTES]
            output = encoded.decode("utf-8", errors="replace")
            if not truncated:
                output += "\n... truncated (output exceeded size limit)"
        else:
            output = encoded.decode("utf-8", errors="replace")

    return output.strip()
