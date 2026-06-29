# Tutorial 1 — Your first agent

In this tutorial you will install BOS, set an API key, and run your first
agent query — all without creating any project files.

---

## Install

=== "pip"

    ```bash
    pip install bos-ai
    ```

=== "uv (no install)"

    If you have [uv](https://docs.astral.sh/uv/) you can run BOS without
    installing it into your environment:

    ```bash
    uvx boscli ask "how are you" --model openai/gpt-4o
    ```

    `uvx` creates a throwaway venv on the fly. Everything else on this page
    works the same way — just replace `boscli` with `uvx boscli`.

---

## Set an API key

BOS passes model requests to [LiteLLM](https://docs.litellm.ai/docs/providers),
which reads credentials from environment variables. Set the one that matches
your provider:

=== "OpenAI"

    ```bash
    export OPENAI_API_KEY=sk-...
    ```

=== "Anthropic"

    ```bash
    export ANTHROPIC_API_KEY=sk-ant-...
    ```

=== "Gemini"

    ```bash
    export GEMINI_API_KEY=...
    ```

=== "DeepSeek"

    ```bash
    export DEEPSEEK_API_KEY=...
    ```

You can also put these in a `.env` file inside a project (covered in
[Tutorial 2](create-a-project.md)).

---

## Run a one-shot query

```bash
boscli ask "Summarise the CAP theorem in two sentences." --model openai/gpt-4o
```

You should see the agent's reply printed to your terminal within a few seconds.

### Understanding the `--model` string

The value is a LiteLLM-style `provider/model` string:

```
openai/gpt-4o
│      │
│      └── model name passed to the provider
└── provider prefix
```

| Prefix | Provider | Key env var |
|--------|----------|-------------|
| `openai` | OpenAI | `OPENAI_API_KEY` |
| `anthropic` | Anthropic | `ANTHROPIC_API_KEY` |
| `gemini` | Google Gemini | `GEMINI_API_KEY` |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` |

BOS checks whether the prefix matches a registered `@ep_provider`; if not, it
falls back to the built-in LiteLLM provider with the full string. This means
any provider that LiteLLM supports works here — consult the
[LiteLLM provider docs](https://docs.litellm.ai/docs/providers) for the exact
prefix and env var name.

!!! note "Setting a default model"
    Instead of passing `--model` every time you can export `BOS_MODEL`:

    ```bash
    export BOS_MODEL=openai/gpt-4o
    boscli ask "What is the CAP theorem?"
    ```

---

## Pipe input with `--stdin`

`--stdin` appends everything from standard input to your prompt, which is
useful for piping files or command output into the agent:

```bash
cat README.md | boscli ask "Summarise this document." --model openai/gpt-4o --stdin
```

```bash
git diff HEAD~1 | boscli ask "Write a conventional commit message for this diff." --model openai/gpt-4o --stdin
```

---

## Global options

`boscli ask` runs a single agent turn **in-process** (no gateway required).
Useful options:

| Option | Description |
|--------|-------------|
| `--model <provider/model>` | Override the model for this invocation. |
| `--stdin` | Append stdin to the prompt. |
| `--agent <kind>` | Choose a named agent kind (project-level; see Tutorial 2). |
| `-w / --workspace <path>` | Point at a workspace other than the current directory. |

---

## What you learned

- Install BOS with `pip install bos-ai` or run it ephemerally with `uvx boscli`.
- The `provider/model` string selects the LLM provider and routes credentials
  via environment variables.
- `boscli ask` runs a single in-process turn — no gateway, no project needed.
- `--stdin` lets you pipe file or command output into a query.

**Next:** [Create a project](create-a-project.md) — scaffold a proper workspace
with config, persistence, and a live gateway.
