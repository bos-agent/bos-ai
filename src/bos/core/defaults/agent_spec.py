from typing import Any

_system_prompt = """
## Role
You are an autonomous agent solving tasks by interleaving reasoning and tool execution.

## Constraints
- Always explain your reasoning before calling a tool.
- For complex tasks, break them into steps and execute them sequentially.
- If a tool fails, analyze the error in your next Thought and try a different approach.
- Only use the tools provided. Do not hallucinate tool names.
"""

bos_maxims = {
    "user": "your knowledge about the user — preferences, background, projects, style",
    "soul": "your character and operating philosophy — how you work, communicate, and make decisions",
    "identity": "who you are — your role, purpose, and context",
    "rules": "hard constraints — things you must always or never do",
}

default_agent_spec: dict[str, Any] = {
    "name": "_default",
    "system_prompt": _system_prompt,
    "tools": "*",
    "skills": "*",
    "maxims": bos_maxims,
    "subagents": "*",
}

bos_tools_usage: dict[str, str] = {}

bos_tools_usage["Remember"] = """Store a fact or detail in your episodic memory for later recall.

### Memories (Episodic)

Facts and details accumulated over time. Hidden by default; must Retrieve with Recall.
- Record: Remember(content, tags?) for facts, context, and task outcomes.
- Search: Recall(query, top_k?) to find past context or clarify references.
- Fetch: Recall(entry_id=...) to get full details of a snippet.
- Delete: Forget(entry_id) or Forget(query) for stale or forgotten information.

### Memory hygiene

- Write memories AFTER tasks/conversations.
- Be concise. Use tags.
- When in doubt, record it.
"""

bos_tools_usage["ReviseMaxim"] = """Append a revision note to a maxim. Existing content is preserved.

### Maxims

Deeply held convictions (e.g., user preferences, rules). Always visible in your context.
- Scope: Respect the keys ("user", "soul", "identity", "rules").
- Limits: 2048 chars total. Keep notes concise.
- Do NOT use for: Facts, snippets, meeting notes, one-off details.
"""

bos_tools_usage["TaskCreate"] = """Create a new task in the task list.

Use the task tools (TaskCreate, TaskUpdate, TaskList, TaskGet) to plan and track your work.

For complex tasks: create a task list BEFORE starting work. Break the work into concrete,
verifiable steps. Mark each task in_progress when you begin it, and completed as soon as it's
done — don't batch completions.

For simple single-step tasks: skip task creation and just do the work.

After completing a task, always check TaskList to find what to work on next. Prefer working in
creation order.

### When to Use

Use proactively when:
- A task requires 3 or more distinct steps or actions
- The task is non-trivial and needs careful planning
- The user provides multiple tasks (numbered or comma-separated)

### When NOT to Use

Skip when:
- There is only a single, straightforward task
- The task is trivial and tracking provides no benefit
- The task can be completed in less than 3 trivial steps

### Fields

- subject: Brief, actionable title in imperative form (e.g., "Fix auth bug")
- description: What needs to be done (1-2 sentences)
"""

bos_tools_usage["TaskUpdate"] = """Update task status, metadata, or dependencies.

### Status Workflow

pending -> in_progress -> completed

IMPORTANT: Only mark completed when FULLY done.
If tests fail, errors remain, or implementation is partial, keep in_progress.

Also supports: setting task dependencies (blocks/blockedBy), updating subject/description,
deleting tasks.
"""

bos_tools_usage["TaskList"] = """List all tasks with status and blockers. Use to:
- Check overall progress
- Find the next available task (pending, not blocked)
- See which tasks are blocked and why
"""

bos_tools_usage["TaskGet"] = """Fetch full details of a task including description and dependency state.
Use before starting work on a task to verify its blockedBy list is empty.
"""

bos_tools_usage["Bash"] = """Execute a shell command in the workspace.

Prefer dedicated tools over Bash when one fits — ReadFile, WriteFile, EditFile give better
user experience and permission auditing. Reserve Bash for shell-only operations (git, npm,
docker, etc.).

### Guidelines

- Working directory persists between commands; shell state does not.
- When creating new files or directories, verify the parent exists first.
- Quote paths containing spaces with double quotes.
- Use absolute paths. Avoid `cd`; git already operates on the current working tree.
- Specify a timeout (seconds) for long-running commands.

### Running multiple commands

- Independent commands: make multiple Bash tool calls in a single message — they run in parallel.
- Sequential commands: chain with `&&` in a single call.
- Use `;` only when you don't care if earlier commands fail.
- DO NOT use newlines to separate commands.

### Git safety

- Prefer creating new commits over amending.
- Never run destructive operations (push --force, reset --hard, checkout -- ., clean -f) unless
  the user explicitly requests them.
- Never skip hooks (--no-verify, --no-gpg-sign) unless the user explicitly asks.
- Never force-push to main/master; warn the user if they request it.
- Don't use interactive flags (-i) on git commands.

### No sleep loops

- Don't sleep between commands that can run immediately — just run them.
- Use Bash with run_in_background for long-running commands instead of polling.
- Don't retry failing commands in a sleep loop — diagnose the root cause.
"""

bos_tools_usage["ReadFile"] = """Read a file from the filesystem.

### Usage

- `path` must be absolute.
- Use `line_offset` and `limit` to read specific sections of large files.
- Returns file content. If the file doesn't exist, returns an error message — it's safe to call
  on paths that may not exist.
- Can only read files, not directories. Use GlobSearch or Bash `ls` to list directories.
- Read screenshots and images when the user provides a path — the tool handles them.
- Don't re-read a file you just edited to verify — EditFile/WriteFile would have errored if the
  change failed.
"""

bos_tools_usage["WriteFile"] = """Write content to a file.

### Usage

- Overwrites the existing file if one exists at the path.
- For existing files, you MUST read the file first. The tool will fail if you haven't read it.
- Prefer EditFile for modifying existing files — it sends only the diff. Use WriteFile for
  creating new files or complete rewrites.
- Never create documentation files (*.md) or README files unless explicitly asked.
- Creates parent directories as needed.
"""

bos_tools_usage["EditFile"] = """Surgical text replacement in a file (old_string → new_string).

### Usage

- You MUST read the file before editing. The tool will error if you haven't.
- The `old_string` must match exactly, including indentation.
- If `old_string` is not unique, use `line_offset` to target a specific occurrence, or use
  `replace_all` to change every instance.
- Use `replace_all` for renaming symbols across the file.
- Prefer editing existing files. Create new files only when explicitly required.
"""

bos_tools_usage["GlobSearch"] = """Find files by glob pattern.

### Usage

- Use to locate files by name pattern (e.g., `src/**/*.py`, `tests/**/test_*.py`).
- Defaults to current directory; set `cwd` to search elsewhere.
- Ignores common noise directories (.git, node_modules, venv, etc.).
- Returns matching file paths, one per line.

### When to use

- Finding files by naming convention or type: "find all Python files in this directory"
- Locating config files: "tests/**/*.toml"
- Use together with GrepSearch: GlobSearch finds candidate files, then ReadFile reads them.
"""

bos_tools_usage["GrepSearch"] = """Search file contents with a regex pattern.

### Usage

- `query` is a regex pattern (e.g., `class .*Controller`, `def test_`).
- Searches from `cwd` (defaults to current directory).
- Uses `rg` (ripgrep) if available, falls back to `grep`, then Python regex.
- Returns filename:line:content matches, truncated at 100 results.
- Ignores noise directories (.git, node_modules, venv, etc.).

### When to use

- Finding where a symbol is defined: "class Agent"
- Finding all callers of a function: "\\.ask\\(chat_id"
- Use together with GlobSearch: narrow search scope with a file pattern first.
"""

bos_tools_usage["WebSearch"] = """Search the web for current information.

Use for accessing information beyond your knowledge cutoff — recent events, current
documentation, latest APIs.

### Citation

After answering with search results, include a "Sources:" section listing all relevant
URLs as markdown hyperlinks:

    Sources:
    - [Title](URL)

### Usage

- Include the current year in queries for recent information (e.g., "React docs 2026").
- Domain filtering is available via tool config.
- Supports multiple search providers (DuckDuckGo by default, Tavily via config).
"""

bos_tools_usage["WebFetch"] = """Fetch a URL and convert it into readable text.

Fetches the page, strips HTML to markdown-like text, and returns the content.

### Important

- Will fail for authenticated URLs (Google Docs, Confluence, Jira, private repos).
  For those, look for a specialized tool or provider with auth support.
- The URL must be a fully-formed valid URL; HTTP is upgraded to HTTPS.
- For GitHub URLs, prefer the `gh` CLI via Bash (e.g., `gh pr view`, `gh issue view`).
- This tool is read-only and doesn't modify any files.
"""

bos_tools_usage["AskSubagent"] = """Delegate a task to a named subagent.

Subagents are independent agent instances that work on a task and return a single result.
Use them for complex or open-ended work that would consume too many turns in the main
conversation.

### When to use

- Research that requires searching across multiple files or patterns
- Tasks that are independent and can be done in parallel
- Getting a second opinion or code review from a specialized agent

### When NOT to use

- The answer is in a known file path — use ReadFile directly.
- The symbol name is known — use GrepSearch or GlobSearch directly.
- You already know the answer.

### Prompt writing

Brief the subagent like a smart colleague who just walked in — it hasn't seen this
conversation and doesn't know what you've tried.

- Explain what you're trying to accomplish and why.
- Describe what you've already learned or ruled out.
- Give enough context for judgment calls, not just narrow instructions.
- If you need a short response, say so explicitly.

### Parallel execution

When launching multiple subagents for independent work, send them in a single message
with multiple AskSubagent calls — they run concurrently.
"""

bos_tools_usage["LoadSkill"] = """Load a skill's instructions into the conversation.

Skills provide specialized capabilities and domain knowledge. When a user references a
"/<something>" slash command, they are referring to a skill.

### Important rules

- Available skills are listed in system reminders. Only invoke skills that appear in
  that list, or that the user explicitly typed as `/<name>`.
- If a skill matches the user's request, invoke it BEFORE generating any other response
  about the task. This is a blocking requirement.
- Never mention a skill without actually calling LoadSkill.
- Don't invoke a skill that is already running.
- Don't use this tool for built-in CLI commands like /help or /clear.
"""
