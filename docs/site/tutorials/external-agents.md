# Tutorial 7 — Run Claude Code or Codex as an agent

BOS can hand an agent's turns to **Claude Code** or **Codex** — the vendors' own agent
harnesses, with their own tools and their own subscription login — and still treat it as
a BOS agent: it has a name, it runs under `boscli ask` and the gateway, it can call your
tools, and its conversation is persisted. In this tutorial you declare one, run it,
let it write, give it a BOS tool, and read its transcript back.

This tutorial assumes you have the `my-agent/` project from
[Tutorial 2](create-a-project.md), and the `WordCount` tool from
[Tutorial 3](custom-tool.md). Pick one runtime and follow its tab wherever the steps
differ. The reference for everything here is
[External agents](../concepts/external-agents.md).

---

## Install the runtime

Each runtime is an extra of `bos-ai`, installed into the same environment as `boscli`:

=== "Claude Code"

    ```bash
    uv tool install boscli --with 'bos-ai[claude-code]'
    ```

=== "Codex"

    ```bash
    uv tool install boscli --with 'bos-ai[codex]'
    ```

Re-running `uv tool install` with `--with` is fine if `boscli` is already installed.
With `uvx`, pass the same `--with` on each call (`uvx --with 'bos-ai[codex]' boscli …`).
Neither extra is part of `bos-ai[all]`: each ships a vendor binary.

---

## Log in

BOS uses the runtime's own login, as the OS user that runs BOS, and stores no token. If
you already use the runtime on this machine as that user, you are logged in.

=== "Claude Code"

    ```bash
    claude        # then /login
    ```

    No `claude` on your `PATH`? The SDK wheel bundles the CLI BOS runs:

    ```bash
    PY="$(uv tool dir)/boscli/bin/python"
    "$("$PY" -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')"
    ```

    On a headless host, `claude setup-token` prints a subscription token to export as
    `CLAUDE_CODE_OAUTH_TOKEN`.

    !!! warning "Check your project's `.env`"
        If you chose Anthropic in Tutorial 2, `.bos/.env` sets `ANTHROPIC_API_KEY`, and
        BOS loads it into the environment Claude Code inherits. A Claude Code agent on the
        subscription login then refuses to build, because that key would be billed
        instead. Move the key out of the environment BOS runs in, or add
        `auth: api_key` to the agent below to bill it deliberately.

=== "Codex"

    ```bash
    codex login
    ```

    No `codex` on your `PATH`? The extra ships the binary BOS runs:

    ```bash
    PY="$(uv tool dir)/boscli/bin/python"
    "$("$PY" -c 'import codex_cli_bin; print(codex_cli_bin.bundled_codex_path())')" login
    ```

---

## Declare the agent

The runtime's name, `claude-code` or `codex`, is a reserved agent kind. Inherit from it
with `_parent` and you get a named agent of your own. Create `.bos/agents/coder.md`:

=== "Claude Code"

    ```markdown
    ---
    description: Works on this project with Claude Code.
    _parent: claude-code
    permission: read-only
    ---
    Your replies are read by this project's other agents and scripts, not by a person
    at a terminal. Answer briefly. If a task needs a change you are not allowed to make,
    say what you would change and why.
    ```

=== "Codex"

    ```markdown
    ---
    description: Works on this project with Codex.
    _parent: codex
    permission: read-only
    ---
    Your replies are read by this project's other agents and scripts, not by a person
    at a terminal. Answer briefly. If a task needs a change you are not allowed to make,
    say what you would change and why.
    ```

`permission` is required — BOS never guesses what an external runtime may do to your
files. The body is appended to the runtime's own system prompt rather than replacing it,
so its tool guidance still works. `[agent.defaults]` does not apply: the `model` and
plugins there are BOS-agent settings.

Check what BOS built, without running a turn:

```bash
boscli inspect agent coder
```

The report shows the runtime, the absolute `cwd` (the project root, since `coder` sets
none), and `permission: read-only`.

---

## Run a turn

```bash
boscli ask --agent coder "What does this project do? Answer in three sentences."
```

The runtime reads the project with its own tools — the progress lines on stderr show
each tool call — and `boscli` prints its answer. With Claude Code, BOS also appended the
project's root `CLAUDE.md` to the prompt, if there is one; Codex reads `AGENTS.md`
itself.

Two things differ from a BOS agent:

- **`--model` is a native model name**, passed to the runtime as is; `BOS_MODEL` does
  not reach it. Leave both out to use the runtime's default.
- **Each `boscli ask` is a new conversation**, and so a new native session. The last step
  keeps one going.

---

## Let it write

Change `permission: read-only` to `permission: workspace-write` in `coder.md`. What
confines the agent now depends on the runtime:

=== "Claude Code"

    BOS offers the file tools and `Bash`, checks every file-tool path against `cwd`, and
    runs `Bash` inside Claude Code's OS sandbox, which confines its writes. On Linux the
    sandbox needs `bwrap` and `socat`:

    ```bash
    sudo apt install bubblewrap socat
    ```

    Without them — and always on Windows — BOS refuses to build the agent rather than
    let bash run unsandboxed, and the error names what to install. macOS needs
    `/usr/bin/sandbox-exec`, which it ships.

=== "Codex"

    Codex's own OS sandbox confines writes to `cwd` — plus `/tmp` and `$TMPDIR`, which
    Codex allows by default. Every escalation past it is refused by Codex itself; nobody
    is asked.

Ask for one write inside the project and one outside it:

```bash
boscli ask --agent coder "Create NOTES.md containing a one-line summary of this project."
boscli ask --agent coder "Write the word hello to ../outside.txt."
```

`NOTES.md` appears; `../outside.txt` does not — the write is refused, and the agent
is told why. Reads are another matter: neither runtime stops a shell command from reading
a file outside `cwd`. The reference lists
[what each level does and does not bound](../concepts/external-agents.md#permission).

---

## Give it a BOS tool

The runtime has its own tools and runs in another process, so your `@ep_tool`s reach it
over MCP — only the ones you list. Add `WordCount` from Tutorial 3 to the frontmatter:

```markdown
---
description: Works on this project with Claude Code.
_parent: claude-code
permission: workspace-write
mcp_tools: [WordCount]
---
```

(With Codex, keep `_parent: codex`.) `boscli inspect agent coder` now lists
`mcp_tools: WordCount`; a name with no registered tool shows up there as unavailable.

```bash
boscli ask --agent coder "Use the WordCount tool to count the words in 'the quick brown fox'."
```

The progress lines show the call — Claude Code names it `mcp__bos-tools__WordCount` —
and the tool itself runs in the BOS process, not in the runtime's. `permission` does not
gate these tools: even a `read-only` agent can call every tool in `mcp_tools`, so list
only the ones it should have.

---

## Keep a conversation, and read its transcript

A native session belongs to a `chat_id`: every turn on the same `chat_id` resumes it,
even after a restart. Save this as `transcript.py` in `my-agent/`:

```python
import asyncio

from bos.sdk import BosApp, Workspace


async def main() -> None:
    async with BosApp(Workspace.from_discovery(".")) as app:
        coder = app.agent("coder")
        first = await coder.run("tutorial-7", "Which file in this project is the largest?")
        print(first.output, f"[{first.finish_reason}]")
        again = await coder.run("tutorial-7", "How many lines does it have?")
        print(again.output)

        print("\n-- what BOS stored --")
        for m in await app.get_messages("tutorial-7", source="bos"):
            print(m.llm_message["role"], m.metadata.get("native_session_id", ""))

        print("\n-- the runtime's own transcript --")
        for m in await app.get_messages("tutorial-7", source="native"):
            print(m.llm_message["role"], str(m.llm_message["content"])[:70])


asyncio.run(main())
```

Run it with the Python of the environment `boscli` and the extra live in:

```bash
"$(uv tool dir)/boscli/bin/python" transcript.py
```

The second question makes sense only because the second turn resumed the first one's
session. BOS's own record is two messages per turn — your message and the final answer,
whose metadata names the runtime and the native session. The native transcript is the
runtime's own: it can hold messages BOS never stored, and it is a live read BOS does not
guarantee. Called without `source`, `get_messages` returns the native transcript for a
chat an external agent served.

---

## Put it behind an actor (optional)

`coder` is an agent kind like any other. Give it an actor to talk to it from the TUI:

```toml
[runtime.actors.coder]
agent = "coder"
```

Or let your main agent delegate to it by adding it to the list from
[Tutorial 4](subagents.md):

```toml
[agents.main.plugin-bindings.SubagentPlugin]
enabled = ["researcher", "writer", "coder"]
```

Restart the gateway, and `@coder` — or `AskSubagent(role="coder", …)` from `main` —
runs a Claude Code or Codex turn.

---

## What you learned

- `_parent: claude-code` or `_parent: codex` makes a named agent backed by that runtime;
  `permission` is required and `[agent.defaults]` does not apply.
- The extra must be installed next to `boscli`, and BOS uses the runtime's own login.
- `permission` bounds the filesystem — confined by BOS's checks and Claude Code's bash
  sandbox, or by Codex's own sandbox — but not reads from the shell, and not the BOS
  tools you list in `mcp_tools`.
- A `chat_id` maps to one native session, resumed on every turn; `BosApp.get_messages`
  reads BOS's two-message record (`source="bos"`) or the runtime's own transcript
  (`source="native"`).

**Next:** [External agents](../concepts/external-agents.md) — every key, each
permission level in detail, auth, streaming, and troubleshooting.
