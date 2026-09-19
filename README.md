```
 ███████████     ███████     █████████
░░███░░░░░███  ███░░░░░███  ███░░░░░███
 ░███    ░███ ███     ░░███░███    ░░░
 ░██████████ ░███      ░███░░█████████
 ░███░░░░░███░███      ░███ ░░░░░░░░███
 ░███    ░███░░███     ███  ███    ░███
 ███████████  ░░░███████░  ░░█████████
░░░░░░░░░░░     ░░░░░░░     ░░░░░░░░░
```

> An agent out of the box. A framework for your own agent-native applications.

<div align="center">
  <p>
    <a href="https://pypi.org/project/bos-ai/"><img src="https://img.shields.io/pypi/v/bos-ai" alt="PyPI"></a>
    <a href="https://pepy.tech/project/bos-ai"><img src="https://static.pepy.tech/badge/bos-ai" alt="Downloads"></a>
    <img src="https://img.shields.io/badge/python-≥3.13-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <a href="https://github.com/bos-agent/bos-ai"><img src="https://img.shields.io/github/stars/bos-agent/bos-ai?style=social" alt="GitHub Stars"></a>
  </p>
</div>


## Quick Start

```bash
OPENAI_API_KEY=<api-key> uvx boscli ask "how are you" --model openai/gpt-4o
```

Install [uv](https://docs.astral.sh/uv/) to get `uvx`, or install the CLI permanently with `uv tool install boscli` / `pipx install boscli`.

Using a different provider? See LiteLLM's [provider docs](https://docs.litellm.ai/docs/providers) for the right `BOS_MODEL` prefix and required environment variables. For example, using a deepseek model

```bash
DEEPSEEK_API_KEY=<api-key> uvx boscli ask "how are you" --model deepseek/deepseek-v4-pro
```

> `pip install bos-ai` installs the **library** — it does not provide the `boscli` command. The CLI ships as the [`boscli`](https://pypi.org/project/boscli/) distribution. See [Embedding](#embedding) to drive BOS from your own application.

## Project Setup

```bash
mkdir my-agent && cd my-agent
boscli init          # guided setup: purpose, topology, model — writes a runnable baseline
boscli gateway start # start the agent runtime
boscli tui           # connect the terminal UI
```

## Embedding

`pip install bos-ai` is a library install: about 14 MB, no console script, no terminal UI, no gateway. Drive an agent from your own application:

```python
from bos.config import Workspace

workspace = Workspace(workspace=".", bos_dir="/var/lib/myapp", config=my_config_dict)
workspace.bootstrap_platform()

async with workspace.harness() as harness:
    agent = await harness.create_agent(kind="assistant")
    result = await agent.run(chat_id, "hello")
```

Configuration is a plain dict — load it from a database, environment, or a control plane; nothing requires a TOML file on disk. [`examples/embed_fastapi.py`](examples/embed_fastapi.py) is a runnable version that serves turns from a FastAPI route, registers its own LLM provider, and touches no file.

### Install extras

| Install | Adds |
|---|---|
| `bos-ai` | The library: `bos.core`, `bos.config`, plugins |
| `bos-ai[litellm]` | The built-in LLM provider. Without it, register your own with `@ep_provider` |
| `bos-ai[gateway]` | The gateway process and the Telegram/Lark channels |
| `bos-ai[search]` | The built-in web-search and page-fetch tools |
| `bos-ai[lark]` | The Lark/Feishu SDK |
| `bos-ai[cli]` | The CLI's dependencies — run it with `python -m bos.cli` |
| `bos-ai[all]` | Everything |

### The supported surface

- `bos.core` — `AgentHarness`, `Agent`, `AgentResult`, the `ep_*` extension points, and the port protocols (`LLM`, `ChatStore`, `Consolidator`, `ToolSet`, `TurnInterceptor`, `PromptProvider`, `TurnEventSink`)
- `bos.config` — `Workspace`, `RootConfig`, `validate_config`

Names prefixed with `_` are re-exported for extensions and are **not stable**; they are not part of the embedding contract.

## Docs

See the [documentation site](https://bos-agent.github.io/bos-ai/) for tutorials, architecture, extension points, and the configuration reference.

Building on BOS with an AI agent? [`llm-full.md`](src/bos/llm-full.md) is a single dense, code-grounded reference covering every mechanism — configuration, extension points, plugins, channels, skills, the CLI, and the runtime — in one file. `boscli init` drops a copy into every scaffolded project.

## License

See [LICENSE](LICENSE).
