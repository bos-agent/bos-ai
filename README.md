# BOS AI

**Lightweight single-file agent framework**

`bos-ai` is an extensible, actor-based Python framework for building and running autonomous AI agents. Built around a remarkably lean core, it provides everything you need to run, extend, and orchestrate LLM-driven agents with local tool execution, memory, and message passing.

---

## 🚀 Easy Start

1. **Install the package:**
   ```bash
   pip install bos-ai
   ```

2. **Initialize a Workspace:**
   Navigate into your project directory and initialize the BOS AI workspace:
   ```bash
   bos init
   ```
   This command creates a `.bos` directory and a `config.toml` file to hold your project's agent configurations.

3. **Start Chatting:**
   Use the chat command to interact with your agent:
   ```bash
   bos chat
   ```

---

## 💻 CLI Intro

BOS AI ships with a `bos` CLI with lazy-loaded commands to keep startup times incredibly fast:

- **`bos init`**: Bootstraps a new workspace. It creates the `.bos/config.toml` file and provisions necessary data directories.
- **`bos auth`**: Set up authentication for various LLM providers and utilities.
- **`bos chat`**: Drops you into an interactive chat application to talk to the agents defined in your configuration. You can also use it in "oneshot" mode.
- **Channels**: Built-in channel bridges currently include `HttpChannel` for WebSocket/REST access and `TelegramChannel` for Telegram bot delivery.

**LLM Providers via Auth:**
```bash
# Chat with the Antigravity provider (requires `bos auth antigravity`)
bos chat -M "hello" -m antigravity/gemini-3.1-pro-low -a main

# Chat with the Gemini CLI provider (requires `bos auth gemini-cli`)
bos chat -M "hello" -m gemini-cli/gemini-2.5-flash -a main

# Chat with the Codex provider (requires `bos auth codex`)
bos chat -M "hello" -m codex/gpt-5.3-codex -a main
```

**Global Options:**
- `-w`, `--workspace`: Path to the workspace directory (defaults to `.`).

---

## 🧠 Principles

`bos-ai` is built on a few core design principles:

- **Lightweight & Embeddable**: The runtime core stays compact and explicit, with clear module boundaries for contracts, defaults, agent runtime, harness lifecycle, and protocol handling.
- **Extensible at the Core**: Every significant layer—from LLM providers, to message persistence, to memory and tools—is powered by an internal Extension System.
- **Agent as an Actor**: Agents communicate via asynchronous message passing (`MailRoute` plus bound `MailBox` capabilities). This enables robust multi-agent orchestration without tightly coupled code.
- **Harness-Managed Lifecycle**: An `AgentHarness` is used to bootstrap, maintain, and gracefully tear down shared resources (like databases or API connections) across all agents in the workspace.

---

## 🔌 Extension Framework

BOS AI utilizes an `ExtensionPoint` pattern for its modular capabilities. You can seamlessly inject your own logic or override defaults decorators.

The framework provides named extension points such as:
- `@ep_provider`: Connect new LLM backends (OpenAI, Anthropic, Gemini, etc.).
- `@ep_tool`: Add new conversational tools that the LLM can invoke.
- `@ep_memory_store`: Connect alternative vector databases or key-value stores.
- `@ep_message_store`: Custom persistence logic for conversation history.
- `@ep_mail_route`: Implement distributed message-routing interfaces like Redis or RabbitMQ.
- `@ep_react_interceptor`: Hooks to orchestrate the internal ReAct loop of an agent.

Example registering a custom tool:

```python
from bos.core import ep_tool

@ep_tool(
    name="my_custom_tool",
    description="Does something awesome.",
    # ... additional structured metadata ...
)
async def my_custom_tool(arg1: str):
    return f"Processed {arg1}"
```

To load your extensions, simply add their module paths to the `platform.extensions` array in your workspace's `config.toml`.

---

## ⚙️ Configuration System

BOS AI uses a hierarchical, TOML-based configuration pattern that is specifically designed for isolation per-workspace.

When you run a command, `bos` searches upwards from the current directory to find a `.bos/config.toml` file, eventually falling back to a global `~/.bos/config.toml` if none is found.

### Config Structure (`.bos/config.toml`)
- **`[platform]`**: Define environment variables, `.env` file locations, and structural extensions loading rules.
- **`[[platform.agents]]`**: Array of dictionaries defining your agents. You can configure `system_prompt`, limits (`max_tokens`), required `tools`, `skills`, memory definitions, and even `subagents`.
- **`[harness]`**: Define the overarching services all agents in the environment share. For example, configure the active `memory_store` directory or hook up an interceptor chain.
- **`[cli]`**: Directives and default options for the command-line application (like specifying a default agent target).

### Telegram Channel
Configure a Telegram bot channel under `main.channels`:

```toml
[[main.channels]]
name = "TelegramChannel"
address = "telegram"
token = "123456:telegram-bot-token"
poll_timeout = 30
allowed_chat_ids = [123456789]
```

Each Telegram chat is mapped to a stable BOS `conversation_id` in the form `telegram:<chat_id>`, so replies return to the correct chat.
