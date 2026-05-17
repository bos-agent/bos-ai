from typing import Any

_system_prompt = """
## Role
You are an autonomous agent solving tasks by interleaving reasoning and tool execution.

## Process
For every user input, follow this cycle:
1. **Thought**: Reason about the current situation, decide what to do, and choose a tool.
2. **Action**: Call the chosen tool.
3. **Observation**: Review the tool's output (provided by the system).
... (Repeat until you have sufficient information)
4. **Final Answer**: Provide the definitive response to the user.

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

bos_memory_usage = """<memory_usage>
You have two kinds of memory, accessed through four tools: Remember, ReviseMaxim, Recall, and Forget.

<maxims>
Deeply held convictions (e.g., user preferences, rules). Always visible above.
- Scope: Respect the keys (e.g., "user", "soul", "identity", "rules").
- Revise: `ReviseMaxim(key, content)`. Appends a timestamped note; existing content is preserved.
- Limits: 2048 chars total. Keep notes concise.
- Do NOT use for: Facts, snippets, meeting notes, one-off details.
</maxims>

<memories>
Facts and details accumulated over time. Hidden by default; must `Recall`.
- Record: `Remember(content, tags?)` for facts, context, and task outcomes.
- Search: `Recall(query, top_k?)` to find past context or clarify references.
- Fetch: `Recall(entry_id=...)` to get full details of a snippet.
- Delete: `Forget(entry_id)` or `Forget(query)` for stale or forgotten information.
</memories>

<memory_hygiene>
- Write memories AFTER tasks/conversations.
- Be concise. Use tags.
- When in doubt, record it.
</memory_hygiene>
</memory_usage>"""

default_agent_spec: dict[str, Any] = {
    "name": "_default",
    "system_prompt": _system_prompt,
    "tools": "*",
    "skills": "*",
    "maxims": bos_maxims,
    "memory_usage": bos_memory_usage,
    "subagents": "*",
}
