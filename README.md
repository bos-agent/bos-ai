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
export OPENAI_API_KEY=<replace with real api key>
export BOS_MODEL=openai/gpt-4o
boscli ask "how are you"
```

Using a different provider? See LiteLLM's [provider docs](https://docs.litellm.ai/docs/providers) for the right `BOS_MODEL` prefix and required environment variables.

Or use `uvx` to start:

```bash
export OPENAI_API_KEY=<replace with real api key>
export BOS_MODEL=openai/gpt-4o
uvx boscli ask "how are you"
```
Install [uv](https://docs.astral.sh/uv/) to get the `uvx`.

## Project Setup

```bash
mkdir my-agent && cd my-agent
boscli init          # guided setup: purpose, topology, model — writes a runnable baseline
boscli gateway start # start the agent runtime
boscli tui           # connect the terminal UI
```

## Docs

See the [documentation site](https://bos-agent.github.io/bos-ai/) for architecture, extension points, and configuration reference.

## License

See [LICENSE](LICENSE).
