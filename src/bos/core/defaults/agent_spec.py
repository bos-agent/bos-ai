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
