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

> From zero to agent in a single command.

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
pip install bos-ai
boscli ask "how are you"
```

Or if you have uv installed:

```bash
uvx boscli ask "how are you"
```

## Project Setup

```bash
mkdir my-agent && cd my-agent
boscli init          # guided setup: purpose, topology, model — writes a runnable baseline
boscli gateway start # start the agent runtime
boscli tui           # connect the terminal UI
```

Grow the project as you go:

```bash
boscli gen agent <name>      # add a specialist agent
boscli gen tool <Name>       # add a custom tool stub
boscli gen channel telegram  # wire a Telegram bot
boscli doctor                # check config, paths, env, credentials
```

## Docs

See [`docs/`](docs/) for architecture, extension points, and configuration reference.

## License

See [LICENSE](LICENSE).
